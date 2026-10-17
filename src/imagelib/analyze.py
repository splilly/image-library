"""Content analysis, run inside worker processes.

For each unique file content this fully decodes the media (which is the integrity
check), extracts metadata, computes perceptual hashes and renders previews. The
input is either the file's bytes (already read once by the volume reader) or, for
very large files, a path the worker opens itself.
"""

from __future__ import annotations

import io
import json
import multiprocessing
import os
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageFile

from . import formats, phash

# Bump when analysis changes in a way that makes re-analysis worthwhile.
# 2: strict JPEG checking (libjpeg-turbo warnings), RAW sensor data actually unpacked.
ANALYZER_VERSION = 2

SMALL_EDGE = 320
MEDIUM_EDGE = 1024
SMALL_QUALITY = 70
MEDIUM_QUALITY = 72

OK = "ok"
WARNING = "warning"
CORRUPT = "corrupt"
UNSUPPORTED = "unsupported"


@dataclass
class Job:
    kind: str
    ext: str
    size: int
    data: bytes | None = None   # whole file, when small enough to read in one go
    path: str | None = None     # otherwise the worker reads from here
    make_medium: bool = True


@dataclass
class Result:
    status: str = OK
    problems: list[str] = field(default_factory=list)
    format: str | None = None
    width: int | None = None
    height: int | None = None
    taken_at: str | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    software: str | None = None
    orientation: int | None = None
    has_exif: bool = False
    duration_s: float | None = None
    phash: int | None = None
    dhash: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    previews: dict[str, bytes] = field(default_factory=dict)

    def flag(self, status: str, problem: str) -> None:
        rank = {OK: 0, WARNING: 1, UNSUPPORTED: 2, CORRUPT: 3}
        if rank[status] > rank[self.status]:
            self.status = status
        self.problems.append(problem)

    def metadata_json(self) -> str | None:
        return json.dumps(self.metadata, ensure_ascii=False, sort_keys=True) if self.metadata else None


_initialized = False
_gate: DecodeGate | None = None
_TEST_HOOKS = os.environ.get("IMAGELIB_TEST_HOOKS") == "1"


def _test_hook(data: bytes) -> None:
    """Simulate decoders that crash or hang (tests only; needs IMAGELIB_TEST_HOOKS=1)."""
    if data.startswith(b"IMAGELIB-TEST-CRASH"):
        os._exit(1)
    if data.startswith(b"IMAGELIB-TEST-HANG"):
        import time
        time.sleep(3600)


class DecodeGate:
    """Memory reservations shared by all worker processes.

    Ordinary photos decode without touching the gate. A job whose decode needs more
    than one unit (a panorama, a huge TIFF/PSD, a big RAW) first reserves enough
    units, so only as many giants run at once as memory allows. Reservations are
    taken under a lock, so two large jobs can never deadlock holding half each.
    """

    UNIT = 256 * 1024 * 1024

    def __init__(self, ctx, budget_bytes: int):
        self.units = max(1, budget_bytes // self.UNIT)
        self._lock = ctx.Lock()
        self._sem = ctx.Semaphore(self.units)

    def reserve(self, estimate: int) -> int:
        n = 0 if estimate <= self.UNIT else min(self.units, -(-estimate // self.UNIT))
        if n:
            with self._lock:
                for _ in range(n):
                    self._sem.acquire()
        return n

    def release(self, n: int) -> None:
        for _ in range(n):
            self._sem.release()


def init_worker(gate: DecodeGate | None = None, log_path: str | None = None) -> None:
    """Per-process setup; safe to call more than once."""
    global _initialized, _gate
    if gate is not None:
        _gate = gate
    if _initialized:
        return
    if log_path:
        # C libraries (LibRaw, libjpeg) print diagnostics straight to stderr, which would
        # scramble the progress display. Keep them, and any worker traceback, in a log.
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 2)
        os.close(fd)
    import signal

    import exiv2
    import pillow_heif

    # Ctrl+C means "pause" to the main process, which lets workers finish their file.
    parent = multiprocessing.parent_process()
    if parent is not None:
        if _TEST_HOOKS and os.environ.get("IMAGELIB_TEST_BROKEN_WORKERS") == "1":
            os._exit(1)  # tests: a worker environment that cannot start at all
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        # If the main process dies (crash, power-button kill), don't linger as an orphan.
        import threading

        def exit_with_parent() -> None:
            parent.join()
            os._exit(0)

        threading.Thread(target=exit_with_parent, daemon=True).start()
    pillow_heif.register_heif_opener()
    exiv2.LogMsg.setLevel(exiv2.LogMsg.Level.mute)  # HEIC/CR3 (BMFF) support is built into the wheel
    Image.MAX_IMAGE_PIXELS = 1_000_000_000
    _initialized = True


def analyze(job: Job) -> Result:
    init_worker()
    if _TEST_HOOKS and job.data is not None:
        _test_hook(job.data)
    result = Result()
    reserved = _gate.reserve(_estimate_decode_bytes(job)) if _gate else 0
    try:
        if job.kind == formats.VIDEO:
            _analyze_video(job, result)
        else:
            if job.size == 0:
                result.flag(CORRUPT, "file is empty (0 bytes)")
                return result
            _read_metadata(job, result)
            if job.kind == formats.RAW:
                _analyze_raw(job, result)
            elif job.kind == formats.PSD:
                _analyze_psd(job, result)
            else:
                _analyze_image(job, result)
    except MemoryError:
        result.flag(WARNING, "too large to verify in available memory")
    except Exception as e:  # never let one bad file kill the worker
        result.flag(CORRUPT, f"analysis failed: {type(e).__name__}: {e}")
    finally:
        if reserved:
            _gate.release(reserved)
    return result


def ping() -> bool:
    """Health check: proves a worker process can start and run a job."""
    init_worker()
    return True


def _estimate_decode_bytes(job: Job) -> int:
    """Rough peak memory to analyze this job, from the header only (nothing is decoded)."""
    if job.kind == formats.VIDEO or job.size == 0:
        return 0
    if job.kind == formats.RAW:
        return job.size * 5  # unpacked 16-bit sensor data plus a half-size render
    try:
        with Image.open(_source(job)) as im:
            w, h = im.size
        # decoded image + 8-bit/RGBA working copy; layered PSD rendering needs more
        return w * h * (16 if job.kind == formats.PSD else 8)
    except Exception:
        return job.size * 4


def _source(job: Job):
    return io.BytesIO(job.data) if job.data is not None else job.path


def _whole_bytes(job: Job) -> bytes:
    if job.data is not None:
        return job.data
    with open(job.path, "rb") as f:
        return f.read()


# -- still images ------------------------------------------------------------

_JPEG_EXTS = frozenset({"jpg", "jpeg", "jpe", "jfif"})


def _analyze_image(job: Job, result: Result) -> None:
    if job.ext in _JPEG_EXTS and _analyze_jpeg(job, result):
        return
    try:
        im = Image.open(_source(job))
    except Image.DecompressionBombError:
        result.flag(WARNING, "image too large to verify")
        return
    except Exception as e:
        result.flag(CORRUPT, f"cannot be opened as an image: {e}")
        return
    with im:
        result.format = result.format or im.format
        try:
            im.load()
        except Exception as e:
            result.flag(CORRUPT, f"decode failed: {e}")
            _render_damaged(job, result)
            return
        if im.format == "JPEG":
            _check_jpeg_end(job, result)
        _finish_still(im, result, job, _orientation(im))


def _analyze_jpeg(job: Job, result: Result) -> bool:
    """Strict JPEG check with libjpeg-turbo. Returns False to fall back to Pillow.

    Pillow silently repairs damaged entropy-coded data (zeroed or overwritten
    blocks); libjpeg only reports it as a warning, which strict mode turns into an
    error. Decoding at a reduced DCT scale still reads every byte of image data, so
    the check stays complete while the decode is several times cheaper.
    """
    import simplejpeg

    data = _whole_bytes(job)
    try:
        height, width, colorspace, _ = simplejpeg.decode_jpeg_header(data)
    except ValueError:
        return False  # not a baseline/progressive JPEG libjpeg-turbo understands; let Pillow judge
    if colorspace not in ("YCbCr", "Gray", "RGB"):
        return False  # CMYK / YCCK (old Photoshop files): Pillow handles the conversion
    result.format = "JPEG"
    orientation = result.orientation
    result.width, result.height = (height, width) if orientation in (5, 6, 7, 8) else (width, height)
    wide = width >= height
    try:
        pixels = simplejpeg.decode_jpeg(data, colorspace="RGB", strict=True,
                                        min_width=MEDIUM_EDGE if wide else 0,
                                        min_height=0 if wide else MEDIUM_EDGE)
    except ValueError as e:
        result.flag(CORRUPT, f"JPEG data damaged: {e}")
        _render_damaged(job, result)
        return True
    _check_jpeg_end(job, result)
    _finish_still(Image.fromarray(pixels), result, job, orientation)
    return True


def _check_jpeg_end(job: Job, result: Result) -> None:
    # Some cameras append data after the end marker, so look near the end rather than at it.
    if job.data is not None and b"\xff\xd9" not in job.data[-65536:]:
        result.flag(WARNING, "JPEG end-of-image marker missing; the file may be cut short")


def _render_damaged(job: Job, result: Result) -> None:
    """Best-effort preview of a damaged image so it can be reviewed by eye."""
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(_source(job)) as im:
            im.load()
            result.width, result.height = result.width or im.width, result.height or im.height
            _make_previews(_prepare(im, _orientation(im)), result, job)
    except Exception:
        pass
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = False


def _finish_still(img: Image.Image, result: Result, job: Job, orientation: int | None = None) -> None:
    """Shared tail for every still: size, quality heuristics, hashes, previews."""
    if result.width is None:
        w, h = img.size
        result.width, result.height = (h, w) if orientation in (5, 6, 7, 8) else (w, h)
    display = _prepare(img, orientation)
    _check_grey_band(display, result)
    result.phash = phash.phash(display)
    result.dhash = phash.dhash(display)
    _make_previews(display, result, job)


# EXIF orientation -> transforms that show the image upright (same table as ImageOps.exif_transpose).
_ORIENT = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _orientation(im: Image.Image) -> int | None:
    try:
        return im.getexif().get(0x0112)
    except Exception:
        return None


def _prepare(img: Image.Image, orientation: int | None) -> Image.Image:
    """Shrink to medium-preview size, convert to 8-bit and orient.

    Hashes, the grey-band check and both previews all use this one small image.
    A cheap integer box reduction first gets within 2x of the target, so the
    high-quality resample only touches a few megapixels instead of 24+.
    """
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = _to_display(img)
    factor = max(img.size) // MEDIUM_EDGE
    if factor >= 2:
        img = img.reduce(factor)
    if max(img.size) > MEDIUM_EDGE:
        img = _fit(img, MEDIUM_EDGE)
    img = _to_display(img)
    if orientation in _ORIENT:
        img = img.transpose(_ORIENT[orientation])
    return img


def _to_display(img: Image.Image) -> Image.Image:
    """Convert any mode (16-bit, CMYK, palette...) to 8-bit RGB/RGBA."""
    if img.mode in ("RGB", "RGBA"):
        return img
    if img.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
        arr = np.asarray(img, dtype=np.float64)
        hi = arr.max() or 1.0
        return Image.fromarray((arr * (255.0 / hi)).astype(np.uint8), "L").convert("RGB")
    if img.mode == "F":
        arr = np.nan_to_num(np.asarray(img, dtype=np.float64))
        lo, hi = arr.min(), arr.max()
        return Image.fromarray(((arr - lo) * (255.0 / ((hi - lo) or 1.0))).astype(np.uint8), "L").convert("RGB")
    if img.mode in ("LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        return img.convert("RGBA")
    return img.convert("RGB")


def _check_grey_band(img: Image.Image, result: Result) -> None:
    """Detect the flat grey area decoders paint below the point where data ran out."""
    small = img if img.mode == "RGB" else img.convert("RGB")
    if small.height > 512:
        small = small.reduce(max(1, small.height // 512))
    arr = np.asarray(small, dtype=np.int16)
    h = arr.shape[0]
    if h < 16:
        return
    rows = 0
    for y in range(h - 1, -1, -1):
        row = arr[y]
        if np.ptp(row) <= 1 and np.all(np.abs(row - 128) <= 2):
            rows += 1
        else:
            break
    if rows >= max(2, h * 0.02):
        result.flag(WARNING, f"bottom {rows * 100 // h}% is flat grey; image data probably missing")


def _fit(img: Image.Image, edge: int) -> Image.Image:
    scale = edge / max(img.size)
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)


def _make_previews(img: Image.Image, result: Result, job: Job) -> None:
    sizes = [("s", SMALL_EDGE, SMALL_QUALITY)]
    if job.make_medium:
        sizes.insert(0, ("m", MEDIUM_EDGE, MEDIUM_QUALITY))
    work = img
    for name, edge, quality in sizes:  # largest first so each step resizes less
        if max(work.size) > edge:
            work = _fit(work, edge)
        buf = io.BytesIO()
        work.save(buf, "WEBP", quality=quality, method=3)
        result.previews[name] = buf.getvalue()


# -- RAW -------------------------------------------------------------------

_FLIP_TO_TRANSPOSE = {
    3: Image.Transpose.ROTATE_180,
    5: Image.Transpose.ROTATE_90,
    6: Image.Transpose.ROTATE_270,
}


def _analyze_raw(job: Job, result: Result) -> None:
    import rawpy

    result.format = job.ext.upper()
    try:
        raw = rawpy.imread(_source(job))  # reads the header only
    except rawpy.LibRawFileUnsupportedError:
        if _looks_like_raw(job):
            result.flag(UNSUPPORTED, "RAW variant not supported by LibRaw")
        else:
            result.flag(CORRUPT, "not a readable RAW file (header is unrecognisable)")
        return
    except Exception as e:
        result.flag(CORRUPT, f"RAW data could not be decoded: {e}")
        return
    with raw:
        # rawpy opens lazily; unpacking decompresses all sensor data, which is the integrity check.
        try:
            raw.unpack()
        except rawpy.LibRawIOError:
            result.flag(CORRUPT, "RAW sensor data ends early (file is cut short)")
            _raw_preview_only(result, job)
            return
        except rawpy.LibRawError as e:
            result.flag(CORRUPT, f"RAW sensor data is damaged: {e}")
            _raw_preview_only(result, job)
            return
        flip = raw.sizes.flip
        w, h = raw.sizes.width, raw.sizes.height
        result.width, result.height = (h, w) if flip in (5, 6) else (w, h)
        img = None
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(thumb.data))
                img.load()
            else:
                img = Image.fromarray(thumb.data)
            if max(img.size) < 600:
                img = None  # too small to judge; render the real thing instead
            elif flip in _FLIP_TO_TRANSPOSE:
                img = img.transpose(_FLIP_TO_TRANSPOSE[flip])
        except Exception:
            img = None
        if img is None:
            try:
                rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
                img = Image.fromarray(rgb)
            except Exception as e:
                result.flag(CORRUPT, f"RAW could not be rendered: {e}")
                return
        _finish_still(img, result, job)


def _raw_preview_only(result: Result, job: Job) -> None:
    """For damaged RAWs, show the embedded preview (if intact) so the file can be judged by eye.

    A fresh LibRaw instance is needed: one whose unpack failed is left unusable.
    """
    import rawpy

    try:
        with rawpy.imread(_source(job)) as raw:
            w, h = raw.sizes.width, raw.sizes.height
            flip = raw.sizes.flip
            thumb = raw.extract_thumb()
        result.width, result.height = (h, w) if flip in (5, 6) else (w, h)
        img = Image.open(io.BytesIO(thumb.data)) if thumb.format == rawpy.ThumbFormat.JPEG \
            else Image.fromarray(thumb.data)
        img.load()
        if flip in _FLIP_TO_TRANSPOSE:
            img = img.transpose(_FLIP_TO_TRANSPOSE[flip])
        _make_previews(_prepare(img, None), result, job)
    except Exception:
        pass


# Leading bytes of the containers camera RAW formats are built on.
_RAW_SIGNATURES = (b"II*\x00", b"MM\x00*", b"IIRO", b"IIRS", b"IIU\x00", b"FUJIFILM", b"\x00MRM",
                   b"FOVb", b"ARRI")


def _looks_like_raw(job: Job) -> bool:
    if job.data is not None:
        head = job.data[:16]
    else:
        with open(job.path, "rb") as f:
            head = f.read(16)
    return head.startswith(_RAW_SIGNATURES) or head[6:14] == b"HEAPCCDR" or head[4:8] == b"ftyp"


# -- PSD -------------------------------------------------------------------

def _analyze_psd(job: Job, result: Result) -> None:
    result.format = "PSB" if job.ext == "psb" else "PSD"
    try:
        with Image.open(_source(job)) as im:
            im.load()
            result.width, result.height = im.size
            _finish_still(im, result, job)
            return
    except Exception:
        pass  # PSB, or no flattened composite stored: fall back to rendering layers
    try:
        from psd_tools import PSDImage

        psd = PSDImage.open(_source(job))
        result.width, result.height = psd.width, psd.height
        composite = psd.composite()
        if composite is None:
            result.flag(WARNING, "PSD has no visible content to preview")
            return
        _finish_still(composite, result, job)
    except Exception as e:
        result.flag(CORRUPT, f"PSD could not be read: {e}")


# -- metadata --------------------------------------------------------------

_EXIF_DATE_KEYS = ("Exif.Photo.DateTimeOriginal", "Exif.Image.DateTimeOriginal",
                   "Exif.Photo.DateTimeDigitized", "Exif.Image.DateTime")
_XMP_DATE_KEYS = ("Xmp.exif.DateTimeOriginal", "Xmp.photoshop.DateCreated", "Xmp.xmp.CreateDate")
_KEEP_PREFIXES = ("Exif.Image.", "Exif.Photo.", "Exif.GPSInfo.", "Xmp.xmp.", "Xmp.photoshop.",
                  "Xmp.exif.", "Xmp.tiff.", "Iptc.Application2.")
_SKIP_KEYS = ("MakerNote", "PrintImageMatching", "XMLPacket", "InterColorProfile", "ImageResources",
              "JPEGTables", "StripOffsets", "StripByteCounts", "TileOffsets", "TileByteCounts")


def _read_metadata(job: Job, result: Result) -> None:
    import exiv2

    try:
        image = exiv2.ImageFactory.open(_whole_bytes(job)) if job.data is not None \
            else exiv2.ImageFactory.open(job.path)
        image.readMetadata()
    except Exception:
        return  # metadata is optional; decoding decides corruption
    values: dict[str, str] = {}
    for data in (image.exifData(), image.xmpData(), image.iptcData()):
        try:
            for datum in data:
                key = datum.key()
                if not key.startswith(_KEEP_PREFIXES) or any(s in key for s in _SKIP_KEYS):
                    continue
                try:
                    text = datum.toString()
                except Exception:
                    continue
                if text and len(text) <= 256 and key not in values:
                    values[key] = text
        except Exception:
            continue
    result.metadata = values
    result.has_exif = any(k.startswith("Exif.") for k in values)
    for key in _EXIF_DATE_KEYS + _XMP_DATE_KEYS:
        if values.get(key, "").strip("0: "):
            result.taken_at = _normalize_date(values[key])
            break
    result.camera_make = values.get("Exif.Image.Make") or None
    result.camera_model = values.get("Exif.Image.Model") or None
    result.software = values.get("Exif.Image.Software") or values.get("Xmp.xmp.CreatorTool") or None
    try:
        result.orientation = int(values.get("Exif.Image.Orientation", ""))
    except ValueError:
        pass


def _normalize_date(text: str) -> str:
    """'2004:07:21 13:05:09' -> '2004-07-21T13:05:09'; other forms are kept as-is."""
    text = text.strip()
    if len(text) >= 19 and text[4] == ":" and text[7] == ":":
        return f"{text[0:4]}-{text[5:7]}-{text[8:10]}T{text[11:19]}"
    return text


# -- video -----------------------------------------------------------------

def _analyze_video(job: Job, result: Result) -> None:
    import av

    try:
        container = av.open(_source(job))
    except Exception as e:
        result.flag(CORRUPT, f"video container cannot be opened: {e}")
        return
    with container:
        result.format = job.ext.upper()
        if container.duration:
            result.duration_s = container.duration / av.time_base
        created = container.metadata.get("creation_time")
        if created:
            result.taken_at = created.replace("Z", "").split(".")[0]
        if not container.streams.video:
            result.flag(WARNING, "no video stream")
            return
        stream = container.streams.video[0]
        result.width, result.height = stream.codec_context.width, stream.codec_context.height
        result.metadata = {f"Video.{k}": str(v)[:256] for k, v in container.metadata.items()}
        result.metadata["Video.codec"] = stream.codec_context.name
        frames: list[Image.Image] = []
        for fraction in (0.1, 0.5, 0.9):
            frame = _frame_at(container, stream, fraction, result.duration_s)
            if frame is None:
                result.flag(CORRUPT if not frames else WARNING,
                            f"could not decode a frame at {int(fraction * 100)}% of the video")
                if not frames:
                    return
            else:
                frames.append(frame)
        _finish_still(frames[0], result, job)


def _frame_at(container, stream, fraction: float, duration_s: float | None) -> Image.Image | None:
    import av

    try:
        if duration_s:
            container.seek(int(duration_s * fraction * av.time_base), any_frame=False)
        for frame in container.decode(stream):
            return frame.to_image()
    except Exception:
        return None
    return None
