"""Organize by moving: no room needed for a second copy, previewed first, undoable."""

import os
from pathlib import Path

import pytest
from conftest import register, scan
from samplelib import picture, save_jpeg

from imagelib import organize
from imagelib.volumes import QUARANTINE_DIR


def listing(root: Path) -> dict[str, bytes]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != QUARANTINE_DIR]
        for name in filenames:
            if not name.startswith(".imagelib"):
                full = os.path.join(dirpath, name)
                out[os.path.relpath(full, root).replace(os.sep, "/")] = open(full, "rb").read()
    return out


@pytest.fixture
def drives(tmp_path, catalog):
    """Drive A holds the library-to-be; B another writable drive; C a read-only one."""
    a, b, c = tmp_path / "A", tmp_path / "B", tmp_path / "C"
    save_jpeg(picture(1), a / "Pictures/2009/IMG_1.jpg", date="2009:05:01 10:00:00")
    save_jpeg(picture(2), a / "Pictures/2009/IMG_2.jpg", date="2009:05:02 10:00:00")
    (a / "Backup").mkdir(parents=True)
    (a / "Backup/IMG_1.jpg").write_bytes((a / "Pictures/2009/IMG_1.jpg").read_bytes())    # an extra copy, same drive
    (a / "Pictures/2009/notes.txt").write_text("not a picture")
    save_jpeg(picture(3), b / "Phone/IMG_3.jpg", date="2010:01:03 09:00:00")         # only on B
    (b / "Phone/IMG_2 copy.jpg").write_bytes((a / "Pictures/2009/IMG_2.jpg").read_bytes())  # an extra copy, other drive
    save_jpeg(picture(4), c / "Camera/IMG_4.jpg", date="2011:02:04 08:00:00")        # only on read-only C
    ids = {}
    for name, root in (("A", a), ("B", b), ("C", c)):
        ids[name] = register(catalog, root, name)
        assert scan(catalog, root, ids[name]).state == "done"
    catalog.conn.execute("UPDATE volumes SET read_only = 1 WHERE id = ?", (ids["C"],))
    online = {ids["A"]: a, ids["B"]: b, ids["C"]: c}
    return a, b, c, ids, online


def plan(catalog, a, ids):
    return organize.create(catalog, a / "Library", organize.Options(mode=organize.MOVE),
                           target_volume=ids["A"], target_rel="Library")


def test_preview_shows_the_library_before_anything_moves(drives, catalog):
    a, b, c, ids, online = drives
    before = (listing(a), listing(b), listing(c))
    p = plan(catalog, a, ids)
    top = organize.browse(catalog, p["id"])
    assert [f["name"] for f in top["folders"]] == ["2009", "2010", "2011"] and top["files"] == []
    day = organize.browse(catalog, p["id"], "2009/2009-05-01")
    assert [(f["name"], f["method"], f["source_label"]) for f in day["files"]] == [("IMG_1.jpg", "rename", "A")]
    assert organize.browse(catalog, p["id"], "2010/2010-01-03")["files"][0]["method"] == "transfer"
    assert organize.browse(catalog, p["id"], "2011/2011-02-04")["files"][0]["method"] == "wait"
    s = organize.summary(catalog, p["id"], online)
    assert s["needed_bytes"] == (b / "Phone/IMG_3.jpg").stat().st_size  # only the transfer needs room
    assert s["state"] == "planned" and (listing(a), listing(b), listing(c)) == before
    organize.discard(catalog, p["id"])
    assert organize.detail(catalog, p["id"]) is None


def test_moving_gathers_one_of_each_and_undo_puts_everything_back(drives, catalog):
    a, b, c, ids, online = drives
    before = (listing(a), listing(b), listing(c))
    p = plan(catalog, a, ids)
    prog = organize.run(catalog, p["id"], online)
    assert prog.state == "done" and prog.failed == 0 and prog.copied == 3 and prog.waiting == 1
    lib = listing(a / "Library")
    assert set(lib) == {"2009/2009-05-01/IMG_1.jpg", "2009/2009-05-02/IMG_2.jpg", "2010/2010-01-03/IMG_3.jpg"}
    assert lib["2010/2010-01-03/IMG_3.jpg"] == before[1]["Phone/IMG_3.jpg"]
    # Old folders keep only what was not organized; the extra copies and B's original are in quarantine.
    assert set(listing(a)) - {f"Library/{k}" for k in lib} == {"Pictures/2009/notes.txt"}
    assert listing(b) == {} and (b / QUARANTINE_DIR / f"organize-{p['id']}" / "Phone/IMG_3.jpg").exists()
    assert (a / QUARANTINE_DIR / f"organize-{p['id']}" / "Backup/IMG_1.jpg").exists()
    assert listing(c) == before[2]  # read-only: never written
    assert not (b / "Phone").exists() and (a / "Pictures/2009").exists()  # emptied folders go; others stay
    assert prog.extras == 2
    # The catalog follows the moves: nothing new or changed when the drives are scanned again.
    for name, root in (("A", a), ("B", b)):
        assert scan(catalog, root, ids[name]).files_total == 0

    assert organize.undo(catalog, p["id"], online) == {"problems": []}
    assert (listing(a), listing(b), listing(c)) == before
    assert not (a / "Library").exists() or not any((a / "Library").rglob("*"))
    for name, root in (("A", a), ("B", b)):
        assert scan(catalog, root, ids[name]).files_total == 0


def test_purge_deletes_the_quarantined_originals_and_copies(drives, catalog):
    a, b, c, ids, online = drives
    p = plan(catalog, a, ids)
    organize.run(catalog, p["id"], online)
    freed = organize.purge(catalog, p["id"], online)
    assert freed["bytes"] > 0 and freed["waiting"] == []
    assert not (a / QUARANTINE_DIR / f"organize-{p['id']}").exists()
    assert not (b / QUARANTINE_DIR / f"organize-{p['id']}").exists()
    assert set(listing(a / "Library")) == {"2009/2009-05-01/IMG_1.jpg", "2009/2009-05-02/IMG_2.jpg",
                                           "2010/2010-01-03/IMG_3.jpg"}


def test_a_changed_file_is_not_moved_and_a_missing_drive_waits(drives, catalog):
    a, b, c, ids, online = drives
    p = plan(catalog, a, ids)
    (a / "Pictures/2009/IMG_2.jpg").write_bytes(b"edited since the scan")
    prog = organize.run(catalog, p["id"], {ids["A"]: a})  # B not connected
    assert prog.failed == 1 and prog.waiting >= 1 and "B" in prog.waiting_volumes
    assert (a / "Pictures/2009/IMG_2.jpg").read_bytes() == b"edited since the scan"
    assert (a / "Library/2009/2009-05-01/IMG_1.jpg").exists()


def test_moving_needs_the_library_on_a_catalogued_drive(drives, catalog, tmp_path):
    with pytest.raises(organize.Refused):
        organize.create(catalog, tmp_path / "elsewhere", organize.Options(mode=organize.MOVE))


def test_a_crash_between_copying_and_quarantining_is_finished_next_time(drives, catalog, monkeypatch):
    a, b, c, ids, online = drives
    p = plan(catalog, a, ids)
    real = organize._quarantine_original

    def crash(*args):
        raise KeyboardInterrupt  # the app dies right after the copy was recorded

    monkeypatch.setattr(organize, "_quarantine_original", crash)
    with pytest.raises(KeyboardInterrupt):
        organize.run(catalog, p["id"], online)
    assert (a / "Library/2010/2010-01-03/IMG_3.jpg").exists() and (b / "Phone/IMG_3.jpg").exists()
    monkeypatch.setattr(organize, "_quarantine_original", real)
    prog = organize.run(catalog, p["id"], online)
    assert prog.failed == 0 and not (b / "Phone/IMG_3.jpg").exists()
    assert catalog.conn.execute("SELECT count(*) FROM files WHERE rel_path = 'Library/2010/2010-01-03/IMG_3.jpg'"
                                ).fetchone()[0] == 1
    assert scan(catalog, a, ids["A"]).files_total == 0


def test_preview_and_move_through_the_api(tmp_path):
    import time

    from fastapi.testclient import TestClient

    from imagelib.server import create_app

    root = tmp_path / "drive"
    save_jpeg(picture(1), root / "Pictures/IMG_1.jpg", date="2009:05:01 10:00:00")
    (root / "Copy").mkdir()
    (root / "Copy/IMG_1.jpg").write_bytes((root / "Pictures/IMG_1.jpg").read_bytes())
    app = create_app(tmp_path / "catalog", workers=1, auto_relate=False)
    with TestClient(app, headers={"x-imagelib": "1"}) as api:
        vid = api.post("/api/volumes", json={"path": str(root), "label": "Drive"}).json()["id"]
        api.post("/api/scan", json={"volume_ids": [vid]}).raise_for_status()
        while api.get("/api/scan").json()["running"]:
            time.sleep(0.1)
        p = api.post("/api/organize", json={"target": str(root / "Library"), "mode": "move"}).json()
        s = api.get(f"/api/organize/{p['id']}").json()
        assert s["state"] == "planned" and s["mode"] == "move" and s["needed_bytes"] == 0
        day = api.get(f"/api/organize/{p['id']}/browse", params={"folder": "2009/2009-05-01"}).json()
        assert day["files"][0]["method"] == "rename" and day["files"][0]["source_label"] == "Drive"
        api.post(f"/api/organize/{p['id']}/run").raise_for_status()
        while api.get("/api/scan").json()["export"]["running"]:
            time.sleep(0.05)
        assert (root / "Library/2009/2009-05-01/IMG_1.jpg").exists() and not (root / "Pictures/IMG_1.jpg").exists()
        assert api.post(f"/api/organize/{p['id']}/undo").json() == {"problems": []}
        assert (root / "Pictures/IMG_1.jpg").exists() and (root / "Copy/IMG_1.jpg").exists()
