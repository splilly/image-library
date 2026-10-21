"""Merging two folder trees: the kept tree gains what only the other had; the rest is quarantined."""

import os
import random

import pytest
from conftest import register, scan

from imagelib import merges
from imagelib.volumes import QUARANTINE_DIR


def put(root, rel, data: bytes):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def listing(root) -> dict[str, bytes]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != QUARANTINE_DIR]
        for name in filenames:
            if name.startswith(".imagelib"):
                continue
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, root).replace(os.sep, "/")] = open(full, "rb").read()
    return out


@pytest.fixture
def merged_drive(tmp_path, catalog):
    rng = random.Random(3)
    blob = {name: rng.randbytes(2000 + i) for i, name in enumerate("abcdefgh")}
    root = tmp_path / "drive"
    for name in "abcde":
        put(root, f"Keep/Pictures/{name}.jpg.bak", blob[name])
    for name in "cdefg":                                  # f, g only here
        put(root, f"Old/Pictures/{name}.jpg.bak", blob[name])
    put(root, "Old/Other place/a copy.bak", blob["a"])     # covered: same content elsewhere in Keep
    put(root, "Keep/notes.txt", b"kept version")
    put(root, "Old/notes.txt", b"the other version")       # conflict
    put(root, "Old/Deep/new/only.txt", blob["h"])          # only here, in folders Keep lacks
    vid = register(catalog, root)
    assert scan(catalog, root, vid).state == "done"
    return root, vid


def run(catalog, merge_id, root, vid):
    return merges.run(catalog, merge_id, {vid: root}, lambda: False, lambda p: None)


def test_prepare_sorts_every_file(merged_drive, catalog):
    root, vid = merged_drive
    m = merges.prepare(catalog, vid, "Keep", vid, "Old")
    c = m["counts"]
    assert (c["move"]["files"], c["conflict"]["files"], c["covered"]["files"]) == (3, 1, 4)
    assert {e["to"] for e in m["examples"]["conflict"]} == {f"Keep/notes (imagelib merge {m['id']}).txt"}
    assert {e["to"] for e in m["examples"]["move"]} == \
        {"Keep/Pictures/f.jpg.bak", "Keep/Pictures/g.jpg.bak", "Keep/Deep/new/only.txt"}
    assert listing(root)["Old/notes.txt"] == b"the other version"  # nothing moved yet


def test_merge_keeps_everything_once_and_undo_puts_it_back(merged_drive, catalog):
    root, vid = merged_drive
    before = listing(root)
    m = merges.prepare(catalog, vid, "Keep", vid, "Old")
    prog = run(catalog, m["id"], root, vid)
    assert prog.state == "merged", prog.message
    after = listing(root)
    assert not any(p.startswith("Old/") for p in after)
    assert after["Keep/notes.txt"] == b"kept version"
    assert after[f"Keep/notes (imagelib merge {m['id']}).txt"] == b"the other version"
    assert after["Keep/Deep/new/only.txt"] == before["Old/Deep/new/only.txt"]
    assert set(before.values()) == set(after.values())  # every distinct content survives in Keep
    quarantined = root / QUARANTINE_DIR / f"merge-{m['id']}" / "Old"
    assert (quarantined / "Pictures" / "c.jpg.bak").exists()

    # The catalog follows: moved files at their new place, the rest of Old gone; a rescan finds no change.
    row = catalog.conn.execute("SELECT state FROM files WHERE rel_path = 'Keep/Deep/new/only.txt'").fetchone()
    assert row["state"] == "done"
    assert catalog.conn.execute("SELECT count(*) FROM files WHERE rel_path LIKE 'Old/%' AND state != 'missing'"
                                ).fetchone()[0] == 0
    assert scan(catalog, root, vid).files_total == 0

    assert merges.undo(catalog, m["id"], {vid: root}) == {"problems": []}
    assert listing(root) == before
    assert not (root / "Keep/Deep").exists()  # folders the merge created are gone again
    assert merges.detail(catalog, m["id"])["status"] == "restored"
    assert scan(catalog, root, vid).files_total == 0  # catalog matches the disk again


def test_purge_deletes_the_quarantined_tree(merged_drive, catalog):
    root, vid = merged_drive
    m = merges.prepare(catalog, vid, "Keep", vid, "Old")
    run(catalog, m["id"], root, vid)
    freed = merges.purge(catalog, m["id"], {vid: root})["bytes"]
    assert freed > 0 and not (root / QUARANTINE_DIR / f"merge-{m['id']}").exists()
    with pytest.raises(merges.MergeRefused):
        merges.undo(catalog, m["id"], {vid: root})


def test_a_tree_that_changed_since_scanning_is_not_merged(merged_drive, catalog):
    root, vid = merged_drive
    m = merges.prepare(catalog, vid, "Keep", vid, "Old")
    put(root, "Old/Pictures/new since scan.bak", b"surprise")
    (root / "Keep/Pictures/a.jpg.bak").write_bytes(b"edited")  # the only copy of a "covered" file
    prog = run(catalog, m["id"], root, vid)
    assert prog.state == "blocked" and prog.done == 0
    assert any("new since the last scan" in p for p in prog.problems)
    assert any("a copy.bak" in p and "kept tree" in p for p in prog.problems)
    assert (root / "Old/notes.txt").exists()


def test_merges_that_cannot_be_done_safely_are_refused(merged_drive, catalog, tmp_path):
    root, vid = merged_drive
    for keep, drop in (("Keep", "Keep/Pictures"), ("", "Old"), ("Keep", "")):
        with pytest.raises(merges.MergeRefused):
            merges.prepare(catalog, vid, keep, vid, drop)
    catalog.conn.execute("UPDATE volumes SET read_only = 1 WHERE id = ?", (vid,))
    with pytest.raises(merges.MergeRefused, match="read-only"):
        merges.prepare(catalog, vid, "Keep", vid, "Old")
    catalog.conn.execute("UPDATE volumes SET read_only = 0 WHERE id = ?", (vid,))
    catalog.conn.execute("UPDATE files SET state = 'pending', content_hash = NULL WHERE rel_path = 'Old/notes.txt'")
    with pytest.raises(merges.MergeRefused, match="not scanned"):
        merges.prepare(catalog, vid, "Keep", vid, "Old")


def test_a_move_interrupted_by_a_crash_is_settled_from_the_disk(merged_drive, catalog):
    root, vid = merged_drive
    m = merges.prepare(catalog, vid, "Keep", vid, "Old")
    item = catalog.conn.execute("SELECT * FROM merge_items WHERE merge_id = ? AND action = 'move' "
                                "ORDER BY src_path LIMIT 1", (m["id"],)).fetchone()
    # As if the app died right after the rename, before recording it.
    os.makedirs(os.path.dirname(root / item["dest_path"]), exist_ok=True)
    os.rename(root / item["src_path"], root / item["dest_path"])
    catalog.conn.execute("UPDATE merge_items SET status = 'moving' WHERE id = ?", (item["id"],))
    prog = run(catalog, m["id"], root, vid)
    assert prog.state == "merged", prog.problems
    assert (root / item["dest_path"]).exists()


def test_merging_through_the_api(tmp_path):
    import time

    from fastapi.testclient import TestClient

    from imagelib.server import create_app

    root = tmp_path / "drive"
    put(root, "Keep/a.bin", b"a" * 100)
    put(root, "Old/a.bin", b"a" * 100)
    put(root, "Old/b.bin", b"b" * 100)
    app = create_app(tmp_path / "catalog", workers=1, auto_relate=False)
    with TestClient(app, headers={"x-imagelib": "1"}) as api:
        vid = api.post("/api/volumes", json={"path": str(root), "label": "Drive"}).json()["id"]
        api.post("/api/scan", json={"volume_ids": [vid]}).raise_for_status()
        while api.get("/api/scan").json()["running"]:
            time.sleep(0.1)
        bad = api.post("/api/merges", json={"keep_volume": vid, "keep_root": "Keep", "drop_volume": vid,
                                             "drop_root": "Keep"})
        assert bad.status_code == 409 and "inside" in bad.json()["detail"]
        m = api.post("/api/merges", json={"keep_volume": vid, "keep_root": "Keep", "drop_volume": vid,
                                           "drop_root": "Old"}).json()
        assert m["counts"]["move"]["files"] == 1 and m["counts"]["covered"]["files"] == 1
        api.post(f"/api/merges/{m['id']}/run").raise_for_status()
        while (d := api.get(f"/api/merges/{m['id']}").json())["status"] in ("planned", "running"):
            time.sleep(0.05)
        assert d["status"] == "merged" and (root / "Keep/b.bin").exists() and not (root / "Old").exists()
        assert api.post(f"/api/merges/{m['id']}/undo").json()["merge"]["status"] == "restored"
        assert (root / "Old/b.bin").exists() and not (root / "Keep/b.bin").exists()


def test_merging_across_drives_copies_checks_and_undoes(merged_drive, catalog, tmp_path):
    root, vid = merged_drive
    other = tmp_path / "other drive"
    put(other, "Backup/Pictures/c.jpg.bak", (root / "Old/Pictures/c.jpg.bak").read_bytes())
    put(other, "Backup/Pictures/z.jpg.bak", b"only on the other drive" * 50)
    put(other, "Backup/notes.txt", b"yet another version")
    vid2 = register(catalog, other, "Other")
    assert scan(catalog, other, vid2).state == "done"
    online = {vid: root, vid2: other}
    before = (listing(root), listing(other))

    m = merges.prepare(catalog, vid, "Keep", vid2, "Backup")
    assert not m["same_drive"]
    assert (m["counts"]["move"]["files"], m["counts"]["conflict"]["files"], m["counts"]["covered"]["files"]) == (1, 1, 1)
    prog = merges.run(catalog, m["id"], online, lambda: False, lambda p: None)
    assert prog.state == "merged", prog.problems
    kept = listing(root)
    assert kept["Keep/Pictures/z.jpg.bak"] == b"only on the other drive" * 50
    assert kept[f"Keep/notes (imagelib merge {m['id']}).txt"] == b"yet another version"
    assert os.stat(root / "Keep/Pictures/z.jpg.bak").st_mtime_ns == \
        os.stat(other / QUARANTINE_DIR / f"merge-{m['id']}" / "Backup/Pictures/z.jpg.bak").st_mtime_ns
    assert not (other / "Backup").exists() and not any(".tmp" in p for p in kept)
    assert scan(catalog, root, vid).files_total == 0  # the copies are already catalogued, unchanged

    assert merges.undo(catalog, m["id"], online) == {"problems": []}
    assert (listing(root), listing(other)) == before
    assert scan(catalog, root, vid).files_total == 0 and scan(catalog, other, vid2).files_total == 0


def test_cross_drive_merge_stops_without_room(merged_drive, catalog, tmp_path, monkeypatch):
    root, vid = merged_drive
    other = tmp_path / "other drive"
    put(other, "Backup/big.bin", b"x" * 1000)
    vid2 = register(catalog, other, "Other")
    scan(catalog, other, vid2)
    m = merges.prepare(catalog, vid, "Keep", vid2, "Backup")
    monkeypatch.setattr(merges, "FREE_SPACE_MARGIN", 1 << 62)
    prog = merges.run(catalog, m["id"], {vid: root, vid2: other}, lambda: False, lambda p: None)
    assert prog.state == "blocked" and "free space" in prog.message
    assert (other / "Backup/big.bin").exists() and not (root / "Keep/big.bin").exists()
