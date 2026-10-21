"""File type classification by extension."""

from __future__ import annotations

# Kinds drive which analyzer is used.
IMAGE = "image"
RAW = "raw"
PSD = "psd"
VIDEO = "video"
# Every other file: catalogued and hashed (to compare folder trees and merge them), never analysed,
# and left out of everything about pictures.
OTHER = "other"
MEDIA_KINDS = (IMAGE, RAW, PSD, VIDEO)

_EXTENSIONS: dict[str, str] = {}


def _register(kind: str, exts: str) -> None:
    for ext in exts.split():
        _EXTENSIONS["." + ext] = kind


_register(IMAGE, "jpg jpeg jpe jfif png gif bmp tif tiff webp heic heif avif jp2")
_register(RAW, "cr2 cr3 crw nef nrw arw srf sr2 orf rw2 raf pef dng raw rwl srw x3f 3fr erf kdc dcr mrw mos iiq")
_register(PSD, "psd psb")
_register(VIDEO, "mp4 m4v mov avi mpg mpeg mts m2ts 3gp mkv wmv")


def kind_for(name: str) -> str | None:
    """Return the media kind for a file name, or None if it is not a supported media file."""
    dot = name.rfind(".")
    if dot < 0:
        return None
    return _EXTENSIONS.get(name[dot:].lower())


def extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1:].lower() if dot >= 0 else ""
