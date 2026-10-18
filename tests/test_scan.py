import os
import shutil
import subprocess
import sys
import time

import pytest

from conftest import file_row, register, scan

from imagelib import volumes
from imagelib.catalog import to_unsigned64
from imagelib.discover import mtime_equivalent
from imagelib.phash import hamming
from imagelib.previews import PreviewStore
from imagelib.search import where


def test_full_scan_classifies_everything(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    prog = scan(catalog, root, vid)

    assert prog.state == "done"
    assert prog.files_total == 22  # trash folder and non-media ignored
    assert prog.files_done == 22

    status = {name: file_row(catalog, vid, path)["status"] for name, path in rel.items()}
    assert status["truncated"] == "corrupt"
    assert status["zeroed"] == "corrupt"  # Pillow alone silently "repairs" this one
    assert file_row(catalog, vid, rel["zeroed"])["has_preview"] == 1  # still reviewable by eye
    assert status["empty"] == "corrupt"
    assert status["garbage_raw"] == "corrupt"
    assert status["grey_band"] == "warning"
    for name in ("photo1", "heic", "tiff16", "psd", "png", "video", "thumb1", "orphan_thumb"):
        assert status[name] == "ok", name
        assert file_row(catalog, vid, rel[name])["has_preview"] == 1, name

    previews = PreviewStore(catalog.previews_path)
    h = file_row(catalog, vid, rel["photo1"])["content_hash"]
    assert previews.get(h, "s") and previews.get(h, "m")


def test_exact_duplicates_share_content_and_are_analyzed_once(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    prog = scan(catalog, root, vid)
    a, b = file_row(catalog, vid, rel["photo1"]), file_row(catalog, vid, rel["dup1"])
    assert a["content_hash"] == b["content_hash"]
    assert prog.reused == 3
    assert catalog.summary()["redundant_copies"] == 3


def test_thumbnail_perceptually_matches_its_original(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    scan(catalog, root, vid)
    ph = {n: to_unsigned64(file_row(catalog, vid, rel[n])["phash"]) for n in ("photo1", "photo2", "thumb1")}
    assert hamming(ph["thumb1"], ph["photo1"]) <= 4
    assert hamming(ph["thumb1"], ph["photo2"]) > 16


def test_rescan_detects_changes_and_missing_files(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    scan(catalog, root, vid)
    (root / rel["photo6"]).unlink()
    shutil.copy2(root / rel["photo5"], root / rel["photo4"])  # changes size & content
    (root / "photos/2004/IMG_0005.jpg").rename(root / "photos/2004/renamed.jpg")

    prog = scan(catalog, root, vid)
    assert prog.files_total == 2  # only the changed and the renamed file are read
    assert file_row(catalog, vid, rel["photo6"])["state"] == "missing"
    assert file_row(catalog, vid, rel["photo5"])["state"] == "missing"
    assert file_row(catalog, vid, rel["photo4"])["content_hash"] == \
        file_row(catalog, vid, "photos/2004/renamed.jpg")["content_hash"]


def test_mtime_equivalence_tolerates_fat_timezone_shifts():
    s = 1_000_000_000
    assert mtime_equivalent(0, 2 * s)
    assert mtime_equivalent(0, 5 * 3600 * s)            # UTC vs UTC-5
    assert mtime_equivalent(0, (5 * 3600 + 1800) * s)   # half-hour zones
    assert not mtime_equivalent(0, 61 * s)
    assert not mtime_equivalent(0, 20 * 3600 * s)


def test_scan_of_subfolder_only_touches_that_folder(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    prog = scan(catalog, root, vid, subpath="photos/2004")
    assert prog.files_total == 8
    assert catalog.conn.execute("SELECT count(*) FROM files").fetchone()[0] == 8


def test_drive_unplugged_during_discovery_does_not_mark_files_missing(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    scan(catalog, root, vid)
    (root / volumes.MARKER_NAME).unlink()  # looks like the drive vanished mid-walk
    prog = scan(catalog, root, vid)
    assert prog.state == "offline"
    assert catalog.conn.execute("SELECT count(*) FROM files WHERE state='missing'").fetchone()[0] == 0


def test_drive_unplugged_during_analysis_pauses_and_resumes(drive, catalog, tmp_path):
    from imagelib.scanner import Scanner

    root, rel = drive
    vid = register(catalog, root)
    scanner = Scanner(catalog, PreviewStore(catalog.previews_path), workers=2)
    target = scanner.prepare(vid, "Sample", root)
    # Complete discovery, then unplug before analysis reads anything.
    import imagelib.discover as discover

    conn = catalog.connect()
    assert discover.discover(conn, vid, root, "", target.scan_id, lambda: False, lambda s: None,
                             target.present)
    conn.execute("UPDATE scans SET discovered=1 WHERE id=?", (target.scan_id,))
    unplugged = tmp_path / "elsewhere"
    root.rename(unplugged)

    prog = scanner.run([target])[target.scan_id]
    assert prog.state == "offline"
    states = {r[0] for r in catalog.conn.execute("SELECT DISTINCT state FROM files")}
    assert states == {"pending"}

    # "Plug it back in" at a different mount point: found by its marker and resumed.
    catalog.mark_volume_seen(vid, str(unplugged))
    found = volumes.locate_online(catalog)
    assert found[vid] == unplugged
    prog = scan(catalog, unplugged, vid)
    assert prog.state == "done" and prog.files_done == 22
    assert catalog.conn.execute("SELECT count(*) FROM scans").fetchone()[0] == 1  # same scan resumed


def test_crash_mid_scan_resumes_without_losing_or_repeating_work(drive, tmp_path):
    root, rel = drive
    env = {**os.environ, "IMAGELIB_HOME": str(tmp_path / "cat")}
    cli = [sys.executable, "-m", "imagelib"]
    subprocess.run(cli + ["volume", "add", str(root), "--label", "Sample"], env=env, check=True,
                   capture_output=True)
    crashed = subprocess.run(cli + ["scan", str(root), "--workers", "2"], capture_output=True,
                             env={**env, "IMAGELIB_TEST_CRASH_AFTER": "5"})
    assert crashed.returncode == 17  # hard exit, like pulling the plug

    from imagelib.catalog import Catalog

    cat = Catalog(tmp_path / "cat")
    done_before = cat.conn.execute("SELECT count(*) FROM files WHERE state='done'").fetchone()[0]
    assert 5 <= done_before < 22
    cat.close()

    resumed = subprocess.run(cli + ["scan"], env=env, capture_output=True, text=True)  # no args = resume
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    cat = Catalog(tmp_path / "cat")
    assert cat.conn.execute("SELECT count(*) FROM files WHERE state='done'").fetchone()[0] == 22
    assert cat.conn.execute("SELECT count(*) FROM scans WHERE state='done'").fetchone()[0] == 1
    cat.close()


def test_decoder_crash_is_isolated_to_the_guilty_file(drive, catalog):
    root, rel = drive
    (root / "photos/2004/IMG_crash.jpg").write_bytes(b"IMAGELIB-TEST-CRASH" + b"\0" * 1000)
    vid = register(catalog, root)
    prog = scan(catalog, root, vid)
    assert prog.state == "done"
    row = file_row(catalog, vid, "photos/2004/IMG_crash.jpg")
    assert row["status"] == "corrupt" and "crashed" in row["problems"]
    # Neighbours that shared the broken pool were retried, not blamed.
    assert catalog.conn.execute(
        "SELECT count(*) FROM contents WHERE status='corrupt'").fetchone()[0] == 5


def test_decoder_hang_is_killed_and_reported(drive, catalog):
    root, rel = drive
    (root / "photos/2004/IMG_hang.jpg").write_bytes(b"IMAGELIB-TEST-HANG" + b"\0" * 1000)
    vid = register(catalog, root)
    started = time.monotonic()
    prog = scan(catalog, root, vid, analysis_timeout=5)
    assert prog.state == "done"
    assert time.monotonic() - started < 60
    row = file_row(catalog, vid, "photos/2004/IMG_hang.jpg")
    assert row["status"] == "corrupt" and "hung" in row["problems"]
    assert catalog.conn.execute("SELECT count(*) FROM files WHERE state='done'").fetchone()[0] == 23


def test_where_finds_exact_copies_and_lookalikes(drive, catalog, tmp_path):
    root, rel = drive
    vid = register(catalog, root)
    scan(catalog, root, vid)
    exact = where(catalog, root / rel["photo1"])
    assert {loc.rel_path for loc in exact} == {rel["photo1"], rel["dup1"]}

    from PIL import Image

    resized = tmp_path / "emailed.jpg"
    with Image.open(root / rel["photo2"]) as im:
        im.resize((640, 480)).save(resized, quality=60)
    similar = where(catalog, resized)
    assert rel["photo2"] in {loc.rel_path for loc in similar}
    assert not any(loc.exact for loc in similar)


def test_content_from_an_older_analyzer_is_rechecked_on_the_next_scan(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    scan(catalog, root, vid)
    zeroed = file_row(catalog, vid, rel["zeroed"])["content_hash"]
    # Pretend an older analyzer once passed this file.
    catalog.conn.execute("UPDATE contents SET analyzer_version=1, status='ok' WHERE hash=?", (zeroed,))
    # Everything else is up to date, so only that file is re-read.
    from imagelib.analyze import ANALYZER_VERSION
    catalog.conn.execute("UPDATE contents SET analyzer_version=? WHERE hash!=?", (ANALYZER_VERSION, zeroed))
    prog = scan(catalog, root, vid)
    assert prog.files_total == 1
    assert file_row(catalog, vid, rel["zeroed"])["status"] == "corrupt"


def test_analyzer_upgrades_reread_only_the_results_they_affect(drive, catalog):
    from imagelib.analyze import STALE_CONTENT

    root, rel = drive
    vid = register(catalog, root)
    scan(catalog, root, vid)
    # As if analyzed by v4: v5 (pixel hashes) affects intact stills only, not damaged files or videos.
    catalog.conn.execute("UPDATE contents SET analyzer_version=4")
    stale = {r[0] for r in catalog.conn.execute(f"SELECT kind || ':' || status FROM contents WHERE {STALE_CONTENT}")}
    assert stale == {"image:ok", "psd:ok"}


def test_writes_wait_out_a_busy_catalog(tmp_path):
    """A lock held longer than SQLite's own wait delays a writer instead of failing it."""
    import sqlite3
    import threading

    from imagelib.catalog import write_transaction

    db = tmp_path / "t.db"
    setup = sqlite3.connect(db, isolation_level=None)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE t(x)")
    writer = sqlite3.connect(db, isolation_level=None, timeout=0.2)  # gives up after 0.2 s on its own
    holder = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    threading.Timer(1.5, lambda: holder.execute("COMMIT")).start()

    started = time.monotonic()
    write_transaction(writer, lambda: writer.execute("INSERT INTO t VALUES (1)"), patience=10)
    assert time.monotonic() - started >= 1.4
    assert setup.execute("SELECT count(*) FROM t").fetchone()[0] == 1

    holder.execute("BEGIN IMMEDIATE")  # and never released: the writer eventually gives up
    with pytest.raises(sqlite3.OperationalError):
        write_transaction(writer, lambda: writer.execute("INSERT INTO t VALUES (2)"), patience=1)
    holder.execute("ROLLBACK")



def test_a_failed_write_never_leaves_the_catalog_locked(tmp_path):
    """An error inside a transaction rolls it back, so other writers (and later writes) carry on."""
    import sqlite3

    from imagelib.catalog import transaction, write_transaction

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x UNIQUE)")
    other = sqlite3.connect(db, isolation_level=None, timeout=0.2)

    def clash():
        conn.execute("INSERT INTO t VALUES (1)")
        conn.execute("INSERT INTO t VALUES (1)")  # fails part-way through

    with pytest.raises(sqlite3.IntegrityError):
        write_transaction(conn, clash)
    assert not conn.in_transaction
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            clash()
    assert not conn.in_transaction
    other.execute("INSERT INTO t VALUES (3)")  # nobody is left holding the lock

    conn.execute("BEGIN IMMEDIATE")  # abandoned by older code: discarded, not built on
    conn.execute("INSERT INTO t VALUES (4)")
    with transaction(conn):
        conn.execute("INSERT INTO t VALUES (5)")
    assert [r[0] for r in other.execute("SELECT x FROM t ORDER BY x")] == [3, 5]


def test_text_that_is_not_valid_unicode_is_stored_not_fatal(catalog):
    """Broken names or metadata (lone surrogates) are stored with a placeholder instead of stopping the scan."""
    from imagelib.catalog import write_transaction

    conn = catalog.conn
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", ("a", "IMG_\udcc0.jpg"))
    conn.executemany("INSERT INTO settings(key, value) VALUES(:k, :v)", [{"k": "b", "v": "Canon \ud83d"}])
    write_transaction(conn, lambda: conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", ("c", "ok \U0001f600")))
    got = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('a', 'b', 'c')").fetchall())
    assert got == {"a": "IMG_�.jpg", "b": "Canon �", "c": "ok \U0001f600"}
