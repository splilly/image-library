"""Find where an image lives: exact copies by hash, look-alikes by perceptual hash."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import blake3
import numpy as np

from . import analyze, formats
from .catalog import Catalog, to_unsigned64


@dataclass
class Location:
    volume_id: str
    volume_label: str
    rel_path: str
    state: str
    content_hash: bytes
    exact: bool     # identical bytes
    distance: int   # pHash bit difference (0 can still mean "a resized copy")


def hash_file(path: Path) -> bytes:
    hasher = blake3.blake3(max_threads=blake3.blake3.AUTO)
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            hasher.update(chunk)
    return hasher.digest()


def locations_for(catalog: Catalog, content_hash: bytes, exact: bool = True, distance: int = 0) -> list[Location]:
    rows = catalog.conn.execute(
        "SELECT f.volume_id, v.label, f.rel_path, f.state FROM files f JOIN volumes v ON v.id=f.volume_id "
        "WHERE f.content_hash=? ORDER BY v.label, f.rel_path", (content_hash,)).fetchall()
    return [Location(r[0], r[1], r[2], r[3], content_hash, exact, distance) for r in rows]


def similar_contents(catalog: Catalog, phash_value: int, max_distance: int = 8,
                     limit: int = 50) -> list[tuple[bytes, int]]:
    """(hash, distance) of catalogued contents whose pHash is within ``max_distance`` bits.

    A brute-force XOR + popcount over every hash takes well under a second even for
    millions of images, which is plenty for interactive look-ups.
    """
    hashes: list[bytes] = []
    values: list[int] = []
    for h, p in catalog.conn.execute("SELECT hash, phash FROM contents WHERE phash IS NOT NULL"):
        hashes.append(h)
        values.append(to_unsigned64(p))
    if not values:
        return []
    arr = np.array(values, dtype=np.uint64)
    dist = np.bitwise_count(arr ^ np.uint64(phash_value))
    idx = np.nonzero(dist <= max_distance)[0]
    idx = idx[np.argsort(dist[idx], kind="stable")][:limit]
    return [(hashes[i], int(dist[i])) for i in idx]


def where(catalog: Catalog, path: Path, max_distance: int = 8) -> list[Location]:
    """Exact copies of ``path`` in the catalog, or failing that, visually similar images."""
    h = hash_file(path)
    exact = locations_for(catalog, h)
    if exact:
        return exact
    kind = formats.kind_for(path.name)
    if kind is None:
        return []
    job = analyze.Job(kind=kind, ext=formats.extension(path.name), size=os.path.getsize(path),
                      path=str(path), make_medium=False)
    result = analyze.analyze(job)
    if result.phash is None:
        return []
    found: list[Location] = []
    for content_hash, distance in similar_contents(catalog, result.phash, max_distance):
        found += locations_for(catalog, content_hash, exact=False, distance=distance)
    return found
