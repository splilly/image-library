"""Stage 1: walk a volume and record every media file's path, size and mtime.

Re-walking is cheap: unchanged files are left alone, changed files are reset to
``pending`` and files that disappeared are marked ``missing`` (never deleted from
the catalog) once a walk has completed without interruption.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import formats
from .catalog import bump_files_version, path_key
from .volumes import MARKER_NAME, QUARANTINE_DIR

# Folders that never hold the user's photos. Thumbnail caches such as .thumbnails
# or @eaDir are deliberately NOT skipped: finding those is part of the job.
SKIP_DIRS = frozenset({
    QUARANTINE_DIR, "$RECYCLE.BIN", "System Volume Information", ".Trashes", ".Trash-1000",
    ".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100", ".TemporaryItems", "lost+found",
})

BATCH = 2000
TWO_SECONDS_NS = 2_000_000_000
QUARTER_HOUR_NS = 15 * 60 * 1_000_000_000
MAX_TZ_SHIFT_NS = 14 * 60 * 60 * 1_000_000_000


def mtime_equivalent(a: int, b: int) -> bool:
    """True if two mtimes describe the same unchanged file.

    FAT/exFAT store local time with 2 s resolution, so the same file reports
    times shifted by whole time-zone offsets on machines set to different zones.
    """
    diff = abs(a - b)
    if diff <= TWO_SECONDS_NS:
        return True
    if diff > MAX_TZ_SHIFT_NS + TWO_SECONDS_NS:
        return False
    rem = diff % QUARTER_HOUR_NS
    return rem <= TWO_SECONDS_NS or QUARTER_HOUR_NS - rem <= TWO_SECONDS_NS


def register_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("mtime_equiv", 2, lambda a, b: int(mtime_equivalent(a, b)), deterministic=True)


@dataclass
class Found:
    rel_path: str
    rel_path_raw: bytes | None
    kind: str
    size: int
    mtime_ns: int


class VolumeOffline(Exception):
    pass


def encode_rel_path(parts: list[str]) -> tuple[str, bytes | None]:
    """Join path parts with '/', keeping raw bytes for names that are not valid UTF-8."""
    rel = "/".join(parts)
    try:
        rel.encode("utf-8")
        return rel, None
    except UnicodeEncodeError:  # undecodable bytes surfaced as surrogates on POSIX
        raw = os.fsencode(rel)
        return raw.decode("utf-8", "replace"), raw


def to_os_path(root: Path, rel_path: str, rel_path_raw: bytes | None):
    if rel_path_raw is not None:
        return os.path.join(os.fsencode(root), rel_path_raw)
    return os.path.join(root, *rel_path.split("/"))


def walk(root: Path, subpath: str, on_error: Callable[[str, OSError], None],
         should_stop: Callable[[], bool]) -> Iterator[Found]:
    start = os.path.join(root, *subpath.split("/")) if subpath else str(root)
    stack: list[tuple[str, list[str]]] = [(start, subpath.split("/") if subpath else [])]
    while stack:
        if should_stop():
            return
        directory, parts = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError as e:
            if not parts:
                raise VolumeOffline(str(e)) from e
            on_error(directory, e)
            continue
        subdirs = []
        for entry in entries:
            name = entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if name not in SKIP_DIRS:
                        subdirs.append((entry.path, parts + [name]))
                    continue
                if not entry.is_file(follow_symlinks=False) or name == MARKER_NAME:
                    continue
                kind = formats.kind_for(name)
                if kind is None:
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError as e:
                on_error(entry.path, e)
                continue
            rel, raw = encode_rel_path(parts + [name])
            yield Found(rel, raw, kind, st.st_size, st.st_mtime_ns)
        # Reverse so directories are visited in name order (stack is LIFO).
        stack.extend(sorted(subdirs, reverse=True))


_UPSERT = """
INSERT INTO files(volume_id, rel_path, rel_path_raw, path_key, kind, size, mtime_ns, state,
                  first_seen_at, last_seen_scan)
VALUES(?,?,?,?,?,?,?,'pending',?,?)
ON CONFLICT(volume_id, rel_path) DO UPDATE SET
    last_seen_scan = excluded.last_seen_scan,
    state = CASE
        WHEN files.size != excluded.size OR NOT mtime_equiv(files.mtime_ns, excluded.mtime_ns) THEN 'pending'
        WHEN files.state = 'missing' THEN CASE WHEN files.content_hash IS NULL THEN 'pending' ELSE 'done' END
        ELSE files.state END,
    content_hash = CASE
        WHEN files.size != excluded.size OR NOT mtime_equiv(files.mtime_ns, excluded.mtime_ns) THEN NULL
        ELSE files.content_hash END,
    size = excluded.size,
    mtime_ns = CASE
        WHEN mtime_equiv(files.mtime_ns, excluded.mtime_ns) THEN files.mtime_ns ELSE excluded.mtime_ns END
"""


@dataclass
class DiscoverStats:
    found: int = 0
    errors: int = 0
    missing: int = 0


def discover(conn: sqlite3.Connection, volume_id: str, root: Path, subpath: str, scan_id: int,
             should_stop: Callable[[], bool], on_progress: Callable[[DiscoverStats], None],
             present: Callable[[], bool]) -> bool:
    """Walk the volume, upserting files. Returns True if the walk completed.

    ``present`` reports whether the volume is still the one mounted at ``root``.
    """
    register_functions(conn)
    stats = DiscoverStats()
    batch: list[tuple] = []
    now = time.time()

    def on_error(path: str, e: OSError) -> None:
        # An unplugged drive looks like a burst of errors; never record it as missing files.
        if not present():
            raise VolumeOffline(f"volume disappeared while reading {path}")
        stats.errors += 1

    def flush() -> None:
        if batch:
            conn.execute("BEGIN IMMEDIATE")
            bump_files_version(conn)
            conn.executemany(_UPSERT, batch)
            conn.execute("COMMIT")
            batch.clear()
        on_progress(stats)

    for f in walk(root, subpath, on_error, should_stop):
        batch.append((volume_id, f.rel_path, f.rel_path_raw, path_key(f.rel_path), f.kind,
                      f.size, f.mtime_ns, now, scan_id))
        stats.found += 1
        if len(batch) >= BATCH:
            flush()
    flush()
    if should_stop():
        return False
    if not present():
        raise VolumeOffline("volume disappeared during the walk")

    prefix = subpath + "/" if subpath else ""
    cur = conn.execute(
        "UPDATE files SET state='missing' WHERE volume_id=? AND last_seen_scan < ? "
        "AND state != 'missing' AND substr(rel_path, 1, ?) = ?",
        (volume_id, scan_id, len(prefix), prefix),
    )
    stats.missing = cur.rowcount
    bump_files_version(conn)
    on_progress(stats)
    return True
