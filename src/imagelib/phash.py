"""64-bit perceptual hashes (pHash and dHash) implemented with numpy only.

Both are computed on the displayed orientation so that a thumbnail whose pixels
were rotated by old software still matches an original that relies on an EXIF
orientation tag.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

_N = 32
# Orthonormal DCT-II basis, so the 2D DCT is D @ X @ D.T
_k = np.arange(_N)
_DCT = np.sqrt(2.0 / _N) * np.cos(np.pi * (2 * _k[None, :] + 1) * _k[:, None] / (2 * _N))
_DCT[0, :] /= np.sqrt(2.0)


def _bits_to_int(bits: np.ndarray) -> int:
    value = 0
    for bit in bits.ravel():
        value = (value << 1) | int(bit)
    return value


def phash(img: Image.Image) -> int:
    small = img.convert("L").resize((_N, _N), Image.Resampling.LANCZOS, reducing_gap=4.0)
    gray = np.asarray(small, dtype=np.float64)
    low = (_DCT @ gray @ _DCT.T)[:8, :8]
    return _bits_to_int(low > np.median(low))


def dhash(img: Image.Image) -> int:
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS, reducing_gap=4.0)
    gray = np.asarray(small, dtype=np.int16)
    return _bits_to_int(gray[:, 1:] > gray[:, :-1])


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()
