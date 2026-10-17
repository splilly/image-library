from pathlib import Path

from conftest import file_row, register, scan
from samplelib import picture, save_jpeg

from imagelib import organize, relate, review, volumes


def setup(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    scan(catalog, root, vid)
    relate.run(catalog)
    return root, rel, vid


def copied(target: Path) -> dict[str, bytes]:
    return {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}


def test_one_copy_of_each_picture_sorted_by_date(drive, catalog, tmp_path):
    root, rel, vid = setup(drive, catalog)
    target = tmp_path / "Library"
    plan = organize.create(catalog, target, organize.Options())
    prog = organize.run(catalog, plan["id"], volumes.locate_online(catalog))
    assert prog.state == "done" and prog.failed == 0
    out = copied(target)
    # Six photos once each (not their backup copies), in date folders, with their own names.
    for i in range(1, 7):
        assert out[f"2004/2004-07-0{i}/IMG_000{i}.jpg"] == (root / rel[f"photo{i}"]).read_bytes()
    names = [Path(p).name for p in out]
    assert names.count("IMG_0001.jpg") == 1
    # Thumbnails of kept originals are left out; the orphan thumbnail (only copy) is kept.
    assert "IMG_0001_thumb.jpg" not in names and "tn_beach.jpg" in names
    # Damaged files are left out by default.
    assert "IMG_trunc.jpg" not in names and "empty.jpg" not in names
    assert not any(n.endswith(organize.TMP_SUFFIX) for n in names)


def test_discarded_pictures_and_excluded_kinds_are_skipped(drive, catalog, tmp_path):
    root, rel, vid = setup(drive, catalog)
    # A damaged file marked to discard stays out even when damaged files are included.
    review.decide(catalog, [file_row(catalog, vid, rel["truncated"])["id"]], review.DISCARD, "problem")
    plan = organize.create(catalog, tmp_path / "L", organize.Options(include_videos=False, include_damaged=True))
    organize.run(catalog, plan["id"], volumes.locate_online(catalog))
    names = [Path(p).name for p in copied(tmp_path / "L")]
    assert "IMG_trunc.jpg" not in names
    assert "IMG_zeroed.jpg" in names          # damaged, not discarded, and asked for
    assert "clip.mp4" not in names


def test_resumes_after_a_crash_and_waits_for_disconnected_drives(drive, catalog, tmp_path):
    root, rel, vid = setup(drive, catalog)
    target = tmp_path / "L"
    plan = organize.create(catalog, target, organize.Options())
    # As if a crash happened mid-copy: one item 'copying' with a partial file left behind.
    item = catalog.conn.execute("SELECT * FROM export_items WHERE export_id = ? LIMIT 1", (plan["id"],)).fetchone()
    partial = target / (item["dest"] + organize.TMP_SUFFIX)
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"half")
    catalog.conn.execute("UPDATE export_items SET status = 'copying' WHERE id = ?", (item["id"],))
    # And the drive is not connected yet.
    prog = organize.run(catalog, plan["id"], {})
    assert prog.copied == 0 and prog.waiting == plan["items"] and prog.waiting_volumes == ["Sample"]
    prog = organize.run(catalog, plan["id"], volumes.locate_online(catalog))
    assert prog.copied == plan["items"] and not partial.exists()
    assert organize.run(catalog, plan["id"], volumes.locate_online(catalog)).total == 0  # nothing left


def test_name_clashes_get_numbered(drive, catalog, tmp_path):
    root, _ = drive
    # A different picture with the same name, taken the same day.
    save_jpeg(picture(50), root / "a/IMG_0001.jpg", date="2004:07:01 12:00:00")
    setup(drive, catalog)
    plan = organize.create(catalog, tmp_path / "L", organize.Options())
    organize.run(catalog, plan["id"], volumes.locate_online(catalog))
    out = copied(tmp_path / "L")
    assert "2004/2004-07-01/IMG_0001.jpg" in out and "2004/2004-07-01/IMG_0001 (2).jpg" in out


def test_a_changed_source_is_not_copied(drive, catalog, tmp_path):
    root, rel, vid = setup(drive, catalog)
    plan = organize.create(catalog, tmp_path / "L", organize.Options())
    for name in ("photo1", "dup1"):
        with open(root / rel[name], "ab") as f:
            f.write(b"x")
    prog = organize.run(catalog, plan["id"], volumes.locate_online(catalog))
    assert prog.failed == 1
    assert "IMG_0001.jpg" not in [Path(p).name for p in copied(tmp_path / "L")]
