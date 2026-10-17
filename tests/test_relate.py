import shutil

import pytest
from conftest import file_row, register, scan
from PIL import Image, ImageDraw
from samplelib import picture, save_jpeg

from imagelib import relate, review


def strip_exif(data: bytes) -> bytes:
    """Remove the APP1 (EXIF) segment: same compressed picture, different bytes."""
    assert data[:2] == b"\xff\xd8"
    pos = 2
    while data[pos] == 0xFF and data[pos + 1] != 0xDA:
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if data[pos + 1] == 0xE1:
            return data[:pos] + data[pos + 2 + length:]
        pos += 2 + length
    raise AssertionError("no EXIF segment")


@pytest.fixture
def lib(drive, catalog):
    root, rel = drive
    extra = {}

    def add(name: str, path: str) -> None:
        extra[name] = path

    # Same picture, metadata stripped by some organiser.
    (root / "organised").mkdir()
    (root / "organised/IMG_0005.jpg").write_bytes(strip_exif((root / rel["photo5"]).read_bytes()))
    add("stripped5", "organised/IMG_0005.jpg")
    # Re-encoded at lower quality, same moment.
    save_jpeg(picture(6), root / "recompressed/IMG_0006.jpg", date="2004:07:06 10:00:00", quality=60)
    add("recompressed6", "recompressed/IMG_0006.jpg")
    # The next frame of a burst: nearly the same scene, a second later. Must NOT be related.
    burst = picture(6)
    ImageDraw.Draw(burst).ellipse((700, 500, 760, 560), fill=(250, 250, 250))
    save_jpeg(burst, root / "photos/2004/IMG_0006_burst.jpg", date="2004:07:06 10:00:01")
    add("burst", "photos/2004/IMG_0006_burst.jpg")
    # A burst frame within the same EXIF second (cameras shoot several per second): only the
    # pixel check can tell it from a copy.
    quick = picture(1)
    ImageDraw.Draw(quick).ellipse((300, 300, 420, 420), fill=(20, 20, 20))
    save_jpeg(quick, root / "photos/2004/IMG_0001_burst.jpg", date="2004:07:01 10:00:00")
    add("burst_same_second", "photos/2004/IMG_0001_burst.jpg")
    # Web export of photo 3, 800 px: a reduced copy, not thumbnail-sized.
    web = picture(3).resize((800, 600), Image.Resampling.LANCZOS)
    save_jpeg(web, root / "web/export/IMG_0003.jpg", date="2004:07:03 10:00:00", quality=80)
    add("web3", "web/export/IMG_0003.jpg")
    # Thumbnail of an image that is itself only "suspicious" (grey band).
    with Image.open(root / rel["grey_band"]) as g:
        thumb = g.copy()
    thumb.thumbnail((160, 160))
    save_jpeg(thumb, root / "broken/.thumbnails/IMG_greyband.jpg", camera=False)
    add("grey_thumb", "broken/.thumbnails/IMG_greyband.jpg")

    vid = register(catalog, root)
    scan(catalog, root, vid)
    assert relate.run(catalog) is True
    rel = {**rel, **extra}
    h = {name: file_row(catalog, vid, path)["content_hash"] for name, path in rel.items()}
    ids = {name: file_row(catalog, vid, path)["id"] for name, path in rel.items()}
    return catalog, vid, root, rel, h, ids


def pairs(cat):
    return {(a, b): kind for a, b, kind in cat.conn.execute("SELECT a, b, kind FROM similar_pairs")}


def related(cat, x, y):
    p = pairs(cat)
    return p.get((x, y)) or p.get((y, x))


def test_relations(lib):
    cat, vid, root, rel, h, ids = lib
    assert related(cat, h["photo5"], h["stripped5"]) == "same_pixels"
    assert related(cat, h["photo6"], h["recompressed6"]) == "near"
    assert pairs(cat).get((h["thumb1"], h["photo1"])) == "derived"   # small first
    assert pairs(cat).get((h["thumb2"], h["photo2"])) == "derived"
    assert pairs(cat).get((h["web3"], h["photo3"])) == "derived"
    assert related(cat, h["photo6"], h["burst"]) is None           # different second: a different shot
    assert related(cat, h["photo1"], h["burst_same_second"]) is None  # same second, but something moved
    # The thumbnail of photo 1 is verified against photo 1, and not against its burst neighbour.
    assert pairs(cat).get((h["thumb1"], h["burst_same_second"])) in (None, "derived_uncertain")
    assert related(cat, h["photo1"], h["photo2"]) is None


def test_relate_is_resumable_and_skips_when_up_to_date(lib, monkeypatch):
    cat, vid, root, rel, h, ids = lib
    expected = pairs(cat)
    assert relate.run(cat) is True and pairs(cat) == expected  # up to date: nothing redone

    monkeypatch.setattr(relate, "BLOCK", 4)
    cat.conn.execute("UPDATE meta SET value = '-1' WHERE key IN ('relate_version', 'relate_target')")
    calls = {"n": 0}

    def stop_after_two_blocks():
        calls["n"] += 1
        return calls["n"] > 1

    assert relate.run(cat, should_stop=stop_after_two_blocks, threads=1) is False
    done_blocks = int(cat.conn.execute("SELECT value FROM meta WHERE key='relate_block'").fetchone()[0])
    assert 0 < done_blocks < -(-len(relate._Stills(cat)) // 4)
    assert relate.run(cat, threads=1) is True
    assert pairs(cat) == expected


def test_thumbnail_classes(lib):
    cat, vid, root, rel, h, ids = lib
    items = {bytes.fromhex(i["hash"]): i for i in review.thumbnails(cat, limit=500)["items"]}
    assert items[h["thumb1"]]["class"] == "original_ok" and items[h["thumb1"]]["kind"] == "thumbnail"
    assert items[h["thumb1"]]["original"]["hash"] == h["photo1"].hex()
    assert items[h["orphan_thumb"]]["class"] == "orphan"
    assert items[h["web3"]]["class"] == "original_ok" and items[h["web3"]]["kind"] == "reduced"
    assert items[h["grey_thumb"]]["class"] == "original_damaged"
    assert h["photo1"] not in items  # originals are not thumbnails
    # Small, but not generated thumbnails: a screenshot PNG, a TIFF scan, a PSD.
    assert not {h["png"], h["tiff16"], h["psd"]} & set(items)


def test_verified_thumbnails_can_be_discarded_but_never_their_originals(lib):
    cat, vid, root, rel, h, ids = lib
    result = review.discard_verified_thumbnails(cat, "thumbnail")
    assert result["images"] == 2  # thumb1, thumb2 (orphan and damaged-original ones are left alone)
    assert review.file_decisions(cat, [ids["thumb1"]])[ids["thumb1"]] == review.DISCARD
    assert ids["orphan_thumb"] not in review.file_decisions(cat, [ids["orphan_thumb"]])
    # A thumbnail surviving does not make its original discardable.
    with pytest.raises(review.Refused):
        review.decide(cat, [ids["photo4"]], review.DISCARD)
    # Nor may the only copy of an orphan thumbnail go.
    with pytest.raises(review.Refused):
        review.decide(cat, [ids["orphan_thumb"]], review.DISCARD)


def test_similar_clusters_suggest_and_keep(lib):
    cat, vid, root, rel, h, ids = lib
    clusters = review.similar_clusters(cat)["items"]
    assert {c["kind"] for c in clusters} == {"same_pixels", "near"}
    by_member = {}
    for c in clusters:
        detail = review.similar_cluster(cat, c["cluster"], online={vid})
        for m in detail["members"]:
            by_member[bytes.fromhex(m["hash"])] = detail
    same = by_member[h["photo5"]]
    assert same["suggested_keep"] == h["photo5"].hex()  # the copy that kept its EXIF
    assert "metadata" in same["suggestion_reason"]
    near = by_member[h["photo6"]]
    assert near["suggested_keep"] == h["photo6"].hex()  # quality 90 beats the quality 60 re-encode

    result = review.keep_cluster_member(cat, same["cluster"], h["photo5"])
    assert result == {"kept": 1, "discarded": 1}
    assert review.file_decisions(cat, [ids["stripped5"]])[ids["stripped5"]] == review.DISCARD
    assert review.similar_clusters(cat, undecided=True)["total"] == 1


def test_new_images_make_relations_stale(lib):
    cat, vid, root, rel, h, ids = lib
    assert relate.is_current(cat)
    shutil.copy2(root / rel["photo1"], root / "photos/2004/copy_of_1.jpg")  # same content: no new analysis
    scan(cat, root, vid)
    assert relate.is_current(cat)
    save_jpeg(picture(77), root / "photos/2004/new.jpg")
    scan(cat, root, vid)
    assert not relate.is_current(cat)
