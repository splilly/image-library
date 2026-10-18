"""Relate: find images that are the same picture in another form.

* same_pixels — identical decoded picture, different bytes (metadata rewritten).
* near        — same size and visually the same: re-encoded, lightly edited.
* derived     — a smaller version of a larger image: thumbnail, web export.

Stills are sorted by aspect ratio and each is compared by perceptual hash (XOR +
popcount in numpy, across threads) with every other still of compatible shape, so
landscape and portrait shots never meet. The cost still grows with the square of the
library: a few minutes per million images on a desktop. The work is done in fixed
blocks; each finished block is committed with its results, so an interrupted run
resumes where it stopped, and a run started on older data starts over.

Burst frames are the trap: consecutive shots look alike to perceptual hashes, and a
camera shooting several frames a second gives them the same EXIF time. So every
candidate is also checked on its previews block by block: a re-encode or a thumbnail
differs from its source by fine noise spread evenly, a burst frame differs somewhere
by a lot (something moved). Compared at 32x32 (coarse enough to ignore resampling
noise in tiny thumbnails), measured on real Canon bursts and genuine copies:
web export 0.7, re-encode 1.2, 160 px thumbnail 1.4, 100 px q70 thumbnail 1.9,
closest burst neighbour 2.1. Candidates that fail the check are dropped (near) or
kept as "derived_uncertain" so the user decides; nothing uncertain is ever treated
as a copy, so a missed match only ever keeps a file.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from .catalog import Catalog, to_unsigned64

BLOCK = 512                 # rows per committed unit of work
COLUMNS = 16384             # columns per numpy operation (bounds memory to ~64 MB per thread)
PHASH_MAX = 10              # candidate pairs
DHASH_MAX = 14
NEAR_PHASH_MAX = 6          # stricter for same-size "near duplicates"
NEAR_DHASH_MAX = 10
DERIVED_SCALE = 1.2         # long edge ratio from which the smaller one counts as derived
ASPECT_TOLERANCE = 0.03
THUMB_EDGE = 640            # small JPEGs without camera EXIF up to this long edge look like thumbnails
TINY_EDGE = 256             # any image this small looks like a thumbnail
NEAR_BLOCK_MAX = 1.6        # max mean difference (0-255) of any cell of an 8x8 grid over 32x32 previews
DERIVED_BLOCK_MAX = 2.0
COMPARE_SIZE = 32

_THUMB_PATH = re.compile(
    r"(^|/)(\.?thumbnails?|\.?thumbs?|@eaDir|\.@__thumb|_vti_cnf|\.?previews?|previews\.lrdata|"
    r"\.picasaoriginals_thumbs|tn)(/|$)"
    r"|(^|/)(tn_|th_)[^/]*$|[_\-.](thumb|thumbnail|tn|small|sm)\.[a-z0-9]+$",
    re.IGNORECASE)


@dataclass
class RelateProgress:
    state: str = "idle"       # idle | running | paused | done | failed
    phase: str = ""
    done: int = 0
    total: int = 0
    pairs: int = 0
    message: str = ""


def _meta(conn, key: str) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(row[0]) if row else -1


def _set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))


# Relations depend only on analysis results, so they track contents_version (bumped when
# the scanner writes contents), not files_version (which moves on every rescan).
VERSION_KEY = "contents_version"


def is_current(cat: Catalog) -> bool:
    return _meta(cat.conn, "relate_version") == _meta(cat.conn, VERSION_KEY)


class _Stills:
    """Every intact still with a perceptual hash, sorted by aspect ratio then hash: a
    stable order (so blocks can resume) in which compatible shapes are contiguous."""

    def __init__(self, cat: Catalog):
        rows = cat.conn.execute(
            "SELECT hash, phash, dhash, width, height, taken_at, pixel_hash FROM contents "
            "WHERE kind != 'video' AND status IN ('ok', 'warning') AND phash IS NOT NULL "
            "AND width > 0 AND height > 0 ORDER BY CAST(width AS REAL) / height, hash").fetchall()
        self.hashes = [r[0] for r in rows]
        self.phash = np.array([to_unsigned64(r[1]) for r in rows], dtype=np.uint64)
        self.dhash = np.array([to_unsigned64(r[2]) or 0 for r in rows], dtype=np.uint64)
        w = np.array([r[3] for r in rows], dtype=np.float64)
        h = np.array([r[4] for r in rows], dtype=np.float64)
        self.edge = np.maximum(w, h)
        self.aspect = w / h
        self.taken = [(r[5] or "")[:19] for r in rows]
        self.pixel = [r[6] for r in rows]

    def __len__(self) -> int:
        return len(self.hashes)


def _compare_block(s: _Stills, start: int) -> list[tuple]:
    """Candidate pairs (i < j) for rows in [start, start + BLOCK) against every later row.

    Perceptual hashes only; verify_pairs() then confirms them against the previews.
    """
    n = len(s)
    rows = np.arange(start, min(start + BLOCK, n))
    # Only later stills whose shape is within tolerance of this block's widest one.
    end = int(np.searchsorted(s.aspect, s.aspect[rows[-1]] * (1 + ASPECT_TOLERANCE), side="right"))
    out = []
    for c0 in range(start, end, COLUMNS):
        cols = np.arange(c0, min(c0 + COLUMNS, end))
        dist = np.bitwise_count(s.phash[rows][:, None] ^ s.phash[cols][None, :])
        ri, ci = np.nonzero(dist <= PHASH_MAX)
        if not len(ri):
            continue
        i, j = rows[ri], cols[ci]
        keep = j > i
        i, j, pd = i[keep], j[keep], dist[ri[keep], ci[keep]]
        dd = np.bitwise_count(s.dhash[i] ^ s.dhash[j])
        a_i, a_j = s.aspect[i], s.aspect[j]
        aspect_ok = np.abs(a_i - a_j) <= ASPECT_TOLERANCE * np.maximum(a_i, a_j)
        sel = (dd <= DHASH_MAX) & aspect_ok
        for ii, jj, p, d in zip(i[sel], j[sel], pd[sel], dd[sel]):
            ti, tj = s.taken[ii], s.taken[jj]
            if ti and tj and ti != tj:
                continue  # different moments: a burst or a similar scene, not the same photo
            if s.pixel[ii] is not None and s.pixel[ii] == s.pixel[jj]:
                continue  # recorded as same_pixels
            ei, ej = s.edge[ii], s.edge[jj]
            scale = max(ei, ej) / min(ei, ej)
            if scale >= DERIVED_SCALE:
                small, large = (ii, jj) if ei < ej else (jj, ii)
                out.append((s.hashes[small], s.hashes[large], "derived", int(p), float(scale)))
            elif p <= NEAR_PHASH_MAX and d <= NEAR_DHASH_MAX:
                a, b = sorted((s.hashes[ii], s.hashes[jj]))
                out.append((a, b, "near", int(p), float(scale)))
    return out


def run(cat: Catalog, should_stop=lambda: False, on_progress=lambda p: None, threads: int | None = None) -> bool:
    """Bring relations up to date. Returns True when finished, False if stopped (resumable)."""
    conn = cat.conn
    prog = RelateProgress(state="running", phase="loading")
    on_progress(prog)
    target = _meta(conn, VERSION_KEY)
    if _meta(conn, "relate_version") == target:
        prog.state, prog.phase = "done", "up to date"
        on_progress(prog)
        return True
    stills = _Stills(cat)
    n = len(stills)
    if _meta(conn, "relate_target") != target:
        # A new run: results from older data are discarded.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM similar_pairs")
        _set_meta(conn, "relate_target", target)
        _set_meta(conn, "relate_block", 0)
        _insert_same_pixels(conn, stills)
        conn.execute("COMMIT")
    starts = list(range(0, n, BLOCK))
    first = _meta(conn, "relate_block")
    prog.phase, prog.total, prog.done = "comparing", len(starts), first
    prog.pairs = conn.execute("SELECT count(*) FROM similar_pairs").fetchone()[0]
    on_progress(prog)

    threads = threads or max(1, min(8, (os.cpu_count() or 2) - 1))
    from .previews import PreviewStore

    previews = _PreviewCache(PreviewStore.shared(cat.previews_path))
    with ThreadPoolExecutor(threads) as pool:
        pending = starts[first:]
        # Submit a window at a time so a stop request takes effect quickly.
        for w in range(0, len(pending), threads * 2):
            if should_stop():
                prog.state = "paused"
                on_progress(prog)
                return False
            window = pending[w:w + threads * 2]
            for block_start, pairs in zip(window, pool.map(lambda s0: _compare_block(stills, s0), window)):
                pairs = verify_pairs(pairs, previews)
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany("INSERT OR REPLACE INTO similar_pairs(a, b, kind, phash_dist, scale) "
                                 "VALUES(?,?,?,?,?)", pairs)
                _set_meta(conn, "relate_block", block_start // BLOCK + 1)
                conn.execute("COMMIT")
                prog.done += 1
                prog.pairs += len(pairs)
                on_progress(prog)

    prog.phase = "finishing"
    on_progress(prog)
    # Work out the results with plain reads, then write them in one short transaction.
    candidates = _thumb_candidates(conn)
    clusters = _clusters(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM thumb_candidates")
    conn.executemany("INSERT INTO thumb_candidates(hash, reason) VALUES(?, ?)", candidates)
    conn.execute("DELETE FROM similar_clusters")
    conn.executemany("INSERT INTO similar_clusters(cluster, hash) VALUES(?, ?)", clusters)
    _set_meta(conn, "relate_version", target)
    conn.execute("COMMIT")
    prog.state, prog.phase = "done", "done"
    on_progress(prog)
    return True


class _PreviewCache:
    """Small grayscale versions of previews, decoded once each."""

    def __init__(self, store, size: int = 2048):
        self.store = store
        self.size = size
        self.cache: dict[bytes, np.ndarray | None] = {}

    def get(self, h: bytes) -> np.ndarray | None:
        if h not in self.cache:
            if len(self.cache) >= self.size:
                self.cache.pop(next(iter(self.cache)))
            self.cache[h] = _small_gray(self.store.get(h, "s"))
        return self.cache[h]


def _small_gray(data: bytes | None) -> np.ndarray | None:
    if not data:
        return None
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            small = im.convert("L").resize((COMPARE_SIZE, COMPARE_SIZE), Image.Resampling.BILINEAR)
            return np.asarray(small, dtype=np.float32)
    except Exception:
        return None


def block_difference(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    """Largest mean difference of any 8x8 block: small for copies, large where something moved."""
    if a is None or b is None:
        return None
    k = COMPARE_SIZE // 8
    return float(np.abs(a - b).reshape(8, k, 8, k).mean(axis=(1, 3)).max())


def verify_pairs(pairs: list[tuple], previews) -> list[tuple]:
    out = []
    for a, b, kind, dist, scale in pairs:
        diff = block_difference(previews.get(a), previews.get(b))
        if kind == "near":
            if diff is not None and diff <= NEAR_BLOCK_MAX:
                out.append((a, b, kind, dist, scale))
        elif diff is not None and diff <= DERIVED_BLOCK_MAX:
            out.append((a, b, "derived", dist, scale))
        else:
            out.append((a, b, "derived_uncertain", dist, scale))
    return out


def _insert_same_pixels(conn, stills: _Stills) -> None:
    groups: dict[bytes, list[bytes]] = {}
    for h, px in zip(stills.hashes, stills.pixel):
        if px is not None:
            groups.setdefault(px, []).append(h)
    pairs = [(first, other, "same_pixels", 0, 1.0)
             for members in groups.values() if len(members) > 1
             for first in [min(members)] for other in members if other != first]
    conn.executemany("INSERT OR REPLACE INTO similar_pairs(a, b, kind, phash_dist, scale) VALUES(?,?,?,?,?)", pairs)


def _thumb_candidates(conn) -> list[tuple[bytes, str]]:
    """(hash, reason) for contents that look like generated thumbnails."""
    # By size: tiny images, or small JPEGs without camera data (what thumbnail generators
    # write). Small screenshots, scans or early VGA camera photos (which carry camera
    # EXIF) are not thumbnails.
    reasons: dict[bytes, list[str]] = {}
    for h, edge in conn.execute(
            "SELECT hash, max(width, height) FROM contents WHERE kind = 'image' AND width > 0 AND "
            "(max(width, height) <= ? OR (max(width, height) <= ? AND format = 'JPEG' AND camera_model IS NULL))",
            (TINY_EDGE, THUMB_EDGE)):
        reasons[h] = [f"small ({edge} px)"]
    by_path: set[bytes] = set()
    for h, path in conn.execute("SELECT content_hash, rel_path FROM files WHERE state != 'missing' "
                                "AND content_hash IS NOT NULL AND kind != 'video'"):
        if h not in by_path and _THUMB_PATH.search(path):
            by_path.add(h)
            reasons.setdefault(h, []).append("thumbnail-style folder or name")
    return [(h, ", ".join(r)) for h, r in reasons.items()]


def _clusters(conn) -> list[tuple[int, bytes]]:
    """(cluster, hash): connected components of same_pixels / near pairs."""
    parent: dict[bytes, bytes] = {}

    def find(x: bytes) -> bytes:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for a, b in conn.execute("SELECT a, b FROM similar_pairs WHERE kind IN ('same_pixels', 'near')"):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
        parent.setdefault(a, a)
        parent.setdefault(b, b)
    roots: dict[bytes, int] = {}
    rows = []
    for h in parent:
        r = find(h)
        rows.append((roots.setdefault(r, len(roots) + 1), h))
    return rows
