"""macOS metadata files ("._name", AppleDouble) are not pictures, whatever they are named."""

import io
import sqlite3

from conftest import file_row, register, scan
from PIL import Image

from imagelib import queries
from imagelib.catalog import Catalog

APPLE_DOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + b"\x00" * 4000


def jpeg_bytes(color=(120, 80, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (900, 600), color).save(out, "JPEG")
    return out.getvalue()


def make_drive(root):
    (root / "photos").mkdir(parents=True)
    (root / "photos" / "IMG_0001.jpg").write_bytes(jpeg_bytes())
    (root / "photos" / "._IMG_0001.jpg").write_bytes(APPLE_DOUBLE)       # the usual sidecar
    (root / "photos" / "._IMG_0002.CR2").write_bytes(APPLE_DOUBLE + b"x")  # next to a RAW
    (root / "photos" / "renamed.jpg").write_bytes(APPLE_DOUBLE + b"y")     # recognised by content
    (root / "photos" / "._real.jpg").write_bytes(jpeg_bytes((10, 90, 200)))  # a real picture, odd name
    return {"photo": "photos/IMG_0001.jpg", "sidecar": "photos/._IMG_0001.jpg",
            "raw_sidecar": "photos/._IMG_0002.CR2", "renamed": "photos/renamed.jpg", "real": "photos/._real.jpg"}


def test_sidecars_are_recognised_by_content(tmp_path, catalog):
    rel = make_drive(tmp_path / "drive")
    vid = register(catalog, tmp_path / "drive")
    prog = scan(catalog, tmp_path / "drive", vid)
    assert prog.state == "done" and prog.corrupt == 0
    for key in ("sidecar", "raw_sidecar", "renamed"):
        assert file_row(catalog, vid, rel[key])["status"] == "sidecar", key
    assert file_row(catalog, vid, rel["real"])["status"] == "ok"
    assert file_row(catalog, vid, rel["photo"])["status"] == "ok"


def test_sidecars_are_left_out_unless_asked_for(tmp_path, catalog):
    rel = make_drive(tmp_path / "drive")
    vid = register(catalog, tmp_path / "drive")
    scan(catalog, tmp_path / "drive", vid)
    shown = {i["path"] for i in queries.list_files(catalog, queries.FileFilter())["items"]}
    assert shown == {rel["photo"], rel["real"]}
    sidecars = queries.FileFilter(status="sidecar")
    assert {i["path"] for i in queries.list_files(catalog, sidecars)["items"]} == \
        {rel["sidecar"], rel["raw_sidecar"], rel["renamed"]}
    assert queries.list_files(catalog, queries.FileFilter(status="problems"))["items"] == []
    s = catalog.summary()
    assert s["files"] == 2 and s["sidecar_files"] == 3 and s["corrupt"] == 0 and s["unique_contents"] == 2
    facets = queries.FacetIndex().facets(catalog)
    assert sum(k["count"] for k in facets["kinds"]) == 2


def test_catalogs_from_older_versions_are_cleaned_up_once(tmp_path):
    """Earlier versions reported sidecars as corrupt; opening the catalog fixes that without the drive."""
    cat = Catalog(tmp_path / "catalog")
    vid = register(cat, tmp_path)  # any folder: the drive is not read
    rows = [("photos/._IMG_0001.jpg", b"a", "corrupt"), ("photos/._IMG_0002.mov", b"b", "corrupt"),
            ("photos/IMG_0003.jpg", b"c", "corrupt"), ("photos/._fine.png", b"d", "ok")]
    for i, (path, h, status) in enumerate(rows):
        cat.conn.execute("INSERT INTO contents(hash, size, kind, status, analyzer_version, analyzed_at) "
                         "VALUES(?, 10, 'image', ?, 5, 0)", (h * 32, status))
        cat.conn.execute("INSERT INTO files(volume_id, rel_path, path_key, kind, size, mtime_ns, state, "
                         "content_hash, first_seen_at, last_seen_scan) VALUES(?,?,?,'image',10,0,'done',?,0,0)",
                         (vid, path, path.lower(), h * 32))
    cat.conn.execute("PRAGMA user_version = 9")
    cat.close()

    reopened = Catalog(tmp_path / "catalog")
    status = dict(reopened.conn.execute("SELECT substr(hash, 1, 1), status FROM contents").fetchall())
    assert status == {b"a": "sidecar", b"b": "sidecar", b"c": "corrupt", b"d": "ok"}
    assert sqlite3.connect(reopened.db_path).execute("PRAGMA user_version").fetchone()[0] >= 10
    reopened.close()
