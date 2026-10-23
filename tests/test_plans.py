import os
import shutil
from pathlib import Path

import pytest
from conftest import file_row, register, scan

from imagelib import plans, review, volumes
from imagelib.volumes import QUARANTINE_DIR


@pytest.fixture
def lib(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    scan(catalog, root, vid)
    ids = {name: file_row(catalog, vid, path)["id"] for name, path in rel.items()}
    return catalog, vid, root, rel, ids


def ctx(cat):
    rows = cat.list_volumes()
    online = volumes.locate_online(cat)
    refs = {r["id"]: volumes.volume_ref(cat, r) for r in rows}
    return online, refs


def run(cat, plan_id, **opts):
    online, refs = ctx(cat)
    return plans.run(cat, plan_id, online, refs, plans.RunOptions(**opts))


def status_of(cat, plan_id, file_id):
    return cat.conn.execute("SELECT status, error FROM plan_items WHERE plan_id=? AND file_id=?",
                            (plan_id, file_id)).fetchone()


def test_quarantine_restore_and_purge(lib):
    cat, vid, root, rel, ids = lib
    original = (root / rel["dup1"]).read_bytes()
    review.decide(cat, [ids["dup1"]], review.DISCARD, "duplicate")
    plan = plans.create(cat)
    assert plan["items"] == 1

    prog = run(cat, plan["id"])
    assert (prog.state, prog.moved) == ("done", 1)
    assert not (root / rel["dup1"]).exists()
    moved = root / QUARANTINE_DIR / f"plan-{plan['id']}" / rel["dup1"]
    assert moved.read_bytes() == original
    assert (root / QUARANTINE_DIR / "README.txt").exists()
    assert file_row(cat, vid, rel["dup1"])["state"] == "missing"
    assert cat.summary()["redundant_copies"] == 2  # no longer counted as a duplicate

    restored = plans.restore(cat, plan["id"], ctx(cat)[0])
    assert restored == {"restored": 1, "relayed": 0, "problems": []}
    assert (root / rel["dup1"]).read_bytes() == original
    assert file_row(cat, vid, rel["dup1"])["state"] == "done"
    assert review.file_decisions(cat, [ids["dup1"]]) == {}  # decision withdrawn with the restore

    # Discard again, new plan, then purge for good.
    review.decide(cat, [ids["dup1"]], review.DISCARD, "duplicate")
    plan2 = plans.create(cat)
    run(cat, plan2["id"])
    result = plans.purge(cat, plan2["id"], ctx(cat)[0])
    assert result["purged"] == 1 and result["bytes"] == len(original)
    assert not (root / QUARANTINE_DIR / f"plan-{plan2['id']}").exists()  # empty folders removed
    assert status_of(cat, plan2["id"], ids["dup1"])["status"] == "purged"
    assert (root / rel["photo1"]).exists()  # the kept copy, untouched throughout


def test_files_changed_after_planning_are_skipped(lib):
    cat, vid, root, rel, ids = lib
    review.decide(cat, [ids["dup1"], ids["dup2"]], review.DISCARD, "duplicate")
    plan = plans.create(cat)
    with open(root / rel["dup1"], "ab") as f:
        f.write(b"edited")                       # size changes
    review.decide(cat, [ids["dup2"]], None)       # decision withdrawn
    prog = run(cat, plan["id"])
    assert prog.skipped == 2 and prog.moved == 0
    assert "changed" in status_of(cat, plan["id"], ids["dup1"])["error"]
    assert "withdrawn" in status_of(cat, plan["id"], ids["dup2"])["error"]
    assert (root / rel["dup1"]).exists() and (root / rel["dup2"]).exists()


def test_same_size_content_change_is_caught_by_the_hash_check(lib):
    cat, vid, root, rel, ids = lib
    review.decide(cat, [ids["dup1"]], review.DISCARD, "duplicate")
    plan = plans.create(cat)
    data = bytearray((root / rel["dup1"]).read_bytes())
    data[-100] ^= 0xFF
    stat = os.stat(root / rel["dup1"])
    (root / rel["dup1"]).write_bytes(bytes(data))
    os.utime(root / rel["dup1"], ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same size and date
    prog = run(cat, plan["id"])
    assert prog.skipped == 1 and (root / rel["dup1"]).exists()


def test_blocked_when_the_kept_copy_vanished_since_the_scan(lib):
    cat, vid, root, rel, ids = lib
    review.decide(cat, [ids["dup1"]], review.DISCARD, "duplicate")
    plan = plans.create(cat)
    (root / rel["photo1"]).unlink()               # the copy we meant to keep is gone
    prog = run(cat, plan["id"])
    assert prog.blocked == 1 and (root / rel["dup1"]).exists()
    assert "not connected" in status_of(cat, plan["id"], ids["dup1"])["error"] or \
        "no other copy" in status_of(cat, plan["id"], ids["dup1"])["error"]


def test_kept_copy_on_a_disconnected_drive(lib, tmp_path):
    cat, vid, root, rel, ids = lib
    # A second drive holding another copy of photo 2, then unplugged.
    other = tmp_path / "other"
    other.mkdir()
    shutil.copy2(root / rel["photo2"], other / "IMG_0002.jpg")
    vid2 = register(cat, other, "Other")
    scan(cat, other, vid2)
    other_id = file_row(cat, vid2, "IMG_0002.jpg")["id"]
    review.decide(cat, [ids["photo2"], ids["dup2"]], review.DISCARD, "duplicate")  # keep only the other drive's
    plan = plans.create(cat)
    other.rename(tmp_path / "unplugged")
    cat.mark_volume_seen(vid2, str(tmp_path / "gone"))

    prog = run(cat, plan["id"])
    assert prog.blocked == 2 and (root / rel["photo2"]).exists()
    prog = run(cat, plan["id"], require_connected_copy=False)   # "trust the catalog"
    assert prog.moved == 2 and not (root / rel["photo2"]).exists()
    assert review.file_decisions(cat, [other_id]) == {}


def test_corrupt_files_need_no_surviving_copy(lib):
    cat, vid, root, rel, ids = lib
    review.decide(cat, [ids["truncated"], ids["empty"]], review.DISCARD, "problem")
    prog = run(cat, plans.create(cat)["id"])
    assert prog.moved == 2


def test_read_only_drives_are_never_written(drive, catalog, monkeypatch):
    from imagelib import fsid

    root, rel = drive
    ident = fsid.FsIdentity({(fsid.SERIAL32, "AAAA-0001")}, label="RO", total_bytes=10 ** 12)
    monkeypatch.setattr(fsid, "identify", lambda r: ident if Path(r).resolve() == root.resolve() else fsid.FsIdentity())
    monkeypatch.setattr(fsid, "mount_root", lambda p: root.resolve())
    ref = volumes.register_read_only(catalog, root, None)
    scan(catalog, root.resolve(), ref.id)
    dup = file_row(catalog, ref.id, rel["dup1"])["id"]
    review.decide(catalog, [dup], review.DISCARD, "duplicate")
    plan = plans.create(catalog)
    before = sorted(str(p) for p in root.rglob("*"))
    online, refs = ctx(catalog)
    prog = plans.run(catalog, plan["id"], online, refs, plans.RunOptions())
    assert prog.total == 0 and prog.moved == 0
    assert sorted(str(p) for p in root.rglob("*")) == before
    listing = plans.manual_list(catalog, plan["id"], ref.id, str(root.resolve()))
    assert str(root.resolve() / rel["dup1"]) in listing


def test_crash_mid_move_is_reconciled(lib):
    cat, vid, root, rel, ids = lib
    review.decide(cat, [ids["dup1"], ids["dup2"]], review.DISCARD, "duplicate")
    plan = plans.create(cat)
    q1 = f"{QUARANTINE_DIR}/plan-{plan['id']}/{rel['dup1']}"
    # dup1: crashed after the rename; dup2: crashed before it.
    (root / q1).parent.mkdir(parents=True)
    (root / rel["dup1"]).rename(root / q1)
    cat.conn.execute("UPDATE plan_items SET status='moving', quarantine_path=? WHERE file_id=?", (q1, ids["dup1"]))
    cat.conn.execute("UPDATE plan_items SET status='moving', quarantine_path=? WHERE file_id=?",
                     (f"{QUARANTINE_DIR}/plan-{plan['id']}/{rel['dup2']}", ids["dup2"]))
    prog = run(cat, plan["id"])
    assert status_of(cat, plan["id"], ids["dup1"])["status"] == "quarantined"
    assert status_of(cat, plan["id"], ids["dup2"])["status"] == "quarantined"  # retried and moved
    assert prog.moved == 1


def test_drive_unplugged_mid_run_pauses_without_skipping(lib, tmp_path):
    cat, vid, root, rel, ids = lib
    review.decide(cat, [ids["dup1"], ids["dup2"]], review.DISCARD, "duplicate")
    plan = plans.create(cat)
    online, refs = ctx(cat)
    root.rename(tmp_path / "unplugged")           # gone after the run decided it was online
    prog = plans.run(cat, plan["id"], online, refs, plans.RunOptions())
    assert prog.state == "paused" and "disconnected" in prog.message
    statuses = {r[0] for r in cat.conn.execute("SELECT status FROM plan_items WHERE plan_id=?", (plan["id"],))}
    assert statuses == {"pending"}


def test_a_file_is_only_ever_in_one_active_plan(lib):
    cat, vid, root, rel, ids = lib
    review.decide(cat, [ids["dup1"]], review.DISCARD, "duplicate")
    assert plans.create(cat)["items"] == 1
    assert plans.create(cat) == {"id": None, "items": 0}
