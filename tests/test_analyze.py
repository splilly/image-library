"""Analyzer edge cases seen in real libraries."""

import io

from samplelib import picture

from imagelib import analyze


def jpeg(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def run(data: bytes, ext: str = "jpg") -> analyze.Result:
    return analyze.analyze(analyze.Job(kind="image", ext=ext, size=len(data), data=data, make_medium=False))


def test_jpeg_with_zero_padding_after_the_end_marker_is_fine():
    # Seen on a real drive: ~250 KB of zeros after a complete JPEG (camera / copy tool padding).
    r = run(jpeg(picture(7, (800, 600))) + b"\0" * 300_000)
    assert r.status == analyze.OK, r.problems


def test_jpeg_with_trailing_vendor_data_is_fine():
    r = run(jpeg(picture(8, (800, 600))) + b"VENDOR" * 1000)
    assert r.status == analyze.OK, r.problems


def test_jpeg_missing_its_end_is_flagged():
    data = jpeg(picture(9, (800, 600)))
    r = run(data[:-2] + b"\x12\x34" * 40_000)  # end marker replaced by unrelated bytes
    assert r.status != analyze.OK


def test_damaged_jpeg_still_gets_a_perceptual_hash_and_preview():
    data = jpeg(picture(10, (800, 600)))
    mid = len(data) // 2
    r = run(data[:mid] + b"\0" * 4096 + data[mid + 4096:])
    assert r.status == analyze.CORRUPT
    assert r.phash is not None and "s" in r.previews
