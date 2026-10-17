"""Organize: copy the best unique originals into a new, date-sorted library.

One copy of every picture is written to ``<target>/YYYY/YYYY-MM-DD/<name>``, skipping:

* pictures whose every copy is marked to discard;
* smaller versions (thumbnails, web exports) of an intact original being kept;
* the extra versions in a group of similar images (same pixels, near-identical): the
  version marked keep is copied, otherwise the suggested one;
* damaged pictures, unless asked for.

Orphan thumbnails — the only surviving copy of a picture — are copied.

Nothing is moved or deleted. Each file is copied under a temporary name, read back
and checked against its hash, then renamed into place; the work is journaled and
resumes after a crash. Pictures whose copies are all on disconnected drives wait.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import blake3

from .catalog import Catalog
from .discover import to_os_path
from .review import VALID_DECISION, _SECONDARY, suggest_member, survives

TMP_SUFFIX = ".imagelib-partial"


@dataclass
class Options:
    include_videos: bool = True
    include_raw: bool = True
    include_damaged: bool = False


@dataclass
class Progress:
    export_id: int
    state: str = "running"
    total: int = 0
    done: int = 0
    copied: int = 0
    copied_bytes: int = 0
    failed: int = 0
    waiting: int = 0
    current: str = ""
    message: str = ""
    waiting_volumes: list[str] = field(default_factory=list)


def _selection(cat: Catalog, opts: Options) -> list:
    kinds = ["image", "psd"] + (["raw"] if opts.include_raw else []) + (["video"] if opts.include_videos else [])
    rows = cat.conn.execute(
        f"""SELECT c.hash, c.kind, c.status, c.taken_at, c.size,
                   (SELECT min(f.mtime_ns) FROM files f WHERE f.content_hash = c.hash AND f.state != 'missing') AS mtime
            FROM contents c
            WHERE c.kind IN ({','.join('?' * len(kinds))})
            AND {survives('c.hash')}
            {'' if opts.include_damaged else "AND c.status != 'corrupt'"}
            AND NOT EXISTS (SELECT 1 FROM similar_pairs p JOIN contents o ON o.hash = p.b
                            WHERE p.a = c.hash AND p.kind = 'derived' AND o.status = 'ok' AND {survives('o.hash')})""",
        kinds).fetchall()
    skip = _cluster_extras(cat)
    return [r for r in rows if r["hash"] not in skip]


def _cluster_extras(cat: Catalog) -> set[bytes]:
    """Versions in similar-image groups that are not the one to keep."""
    from .review import _cluster_members

    extras: set[bytes] = set()
    for (cluster,) in cat.conn.execute("SELECT DISTINCT cluster FROM similar_clusters"):
        members = _cluster_members(cat, cluster)
        kept = {m["hash"] for m in members if cat.conn.execute(
            f"SELECT 1 FROM files f JOIN decisions d ON {VALID_DECISION} WHERE f.content_hash = ? "
            f"AND d.decision = 'keep'", (m["hash"],)).fetchone()}
        keep = kept or {suggest_member(members)[0]}
        extras |= {m["hash"] for m in members if m["hash"] not in keep}
    return extras


def _best_name(cat: Catalog, content_hash: bytes) -> str:
    """The file name to use: from a copy marked keep, else one not in a backup-like folder."""
    rows = cat.conn.execute(
        f"""SELECT f.rel_path, EXISTS (SELECT 1 FROM decisions d WHERE {VALID_DECISION} AND d.decision = 'keep') AS kept
            FROM files f WHERE f.content_hash = ? AND f.state != 'missing'""", (content_hash,)).fetchall()
    best = min(rows, key=lambda r: (not r["kept"], bool(_SECONDARY.search(r["rel_path"])), len(r["rel_path"])))
    return best["rel_path"].rsplit("/", 1)[-1]


def _date_folder(taken_at: str | None, mtime_ns: int | None) -> str:
    if taken_at and len(taken_at) >= 10 and taken_at[:4].isdigit() and taken_at[4] == "-":
        day = taken_at[:10]
    elif mtime_ns:
        day = time.strftime("%Y-%m-%d", time.localtime(mtime_ns / 1e9))
    else:
        return "Undated"
    return f"{day[:4]}/{day}"


def create(cat: Catalog, target: Path, opts: Options) -> dict:
    """Decide what goes where, as a resumable list of copies."""
    target = Path(target).resolve()
    rows = _selection(cat, opts)
    conn = cat.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        export_id = conn.execute("INSERT INTO exports(target, options, created_at) VALUES(?,?,?)",
                                 (str(target), json.dumps(vars(opts)), time.time())).lastrowid
        used: set[str] = set()
        items = []
        for r in sorted(rows, key=lambda r: (r["taken_at"] or "", r["hash"])):
            name = _best_name(cat, r["hash"])
            folder = _date_folder(r["taken_at"], r["mtime"])
            dest = f"{folder}/{name}"
            stem, dot, ext = name.rpartition(".")
            n = 2
            while dest.lower() in used:
                dest = f"{folder}/{stem} ({n}).{ext}" if dot else f"{folder}/{name} ({n})"
                n += 1
            used.add(dest.lower())
            items.append((export_id, r["hash"], r["size"], dest))
        conn.executemany("INSERT INTO export_items(export_id, content_hash, size, dest, status) "
                         "VALUES(?,?,?,?, 'pending')", items)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"id": export_id, "items": len(items), "bytes": sum(i[2] for i in items)}


def detail(cat: Catalog, export_id: int) -> dict | None:
    row = cat.conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
    if row is None:
        return None
    counts = {r[0]: {"files": r[1], "bytes": r[2]} for r in cat.conn.execute(
        "SELECT status, count(*), sum(size) FROM export_items WHERE export_id = ? GROUP BY status", (export_id,))}
    return {"id": export_id, "target": row["target"], "options": json.loads(row["options"]),
            "created_at": row["created_at"], "counts": counts}


def list_exports(cat: Catalog) -> list[dict]:
    return [detail(cat, r[0]) for r in cat.conn.execute("SELECT id FROM exports ORDER BY id DESC")]


def run(cat: Catalog, export_id: int, online: dict[str, Path], should_stop: Callable[[], bool] = lambda: False,
        on_progress: Callable[[Progress], None] = lambda p: None) -> Progress:
    conn = cat.conn
    exp = conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
    target = Path(exp["target"])
    target.mkdir(parents=True, exist_ok=True)
    # An interrupted copy leaves only a partial file under a temporary name: start it again.
    conn.execute("UPDATE export_items SET status = 'pending' WHERE export_id = ? AND status = 'copying'", (export_id,))
    items = conn.execute("SELECT * FROM export_items WHERE export_id = ? AND status IN ('pending', 'failed') "
                         "ORDER BY id", (export_id,)).fetchall()
    prog = Progress(export_id, total=len(items))
    on_progress(prog)
    waiting_volumes: set[str] = set()
    for item in items:
        if should_stop():
            prog.state = "paused"
            break
        source, offline = _source(conn, item["content_hash"], online)
        if source is None:
            prog.waiting += 1
            waiting_volumes |= offline
            prog.done += 1
            continue
        prog.current = item["dest"]
        status, error = _copy(conn, item, source, target)
        prog.done += 1
        if status == "done":
            prog.copied += 1
            prog.copied_bytes += item["size"]
        else:
            prog.failed += 1
        on_progress(prog)
    else:
        prog.state = "done"
    labels = {r[0]: r[1] for r in conn.execute("SELECT id, label FROM volumes")}
    prog.waiting_volumes = sorted(labels.get(v, v) for v in waiting_volumes)
    prog.current = ""
    on_progress(prog)
    return prog


def _source(conn, content_hash: bytes, online: dict[str, Path]):
    """A readable copy on a connected drive (copies marked keep first), or the drives to connect."""
    rows = conn.execute(
        f"""SELECT f.volume_id, f.rel_path, f.rel_path_raw,
                   EXISTS (SELECT 1 FROM decisions d WHERE {VALID_DECISION} AND d.decision = 'discard') AS discarded
            FROM files f WHERE f.content_hash = ? AND f.state != 'missing'""", (content_hash,)).fetchall()
    rows = sorted(rows, key=lambda r: r["discarded"])
    for r in rows:
        if r["volume_id"] in online:
            path = to_os_path(online[r["volume_id"]], r["rel_path"], r["rel_path_raw"])
            if os.path.exists(path):
                return path, set()
    return None, {r["volume_id"] for r in rows}


def _copy(conn, item, source, target: Path) -> tuple[str, str | None]:
    dest = Path(to_os_path(target, item["dest"], None))
    if dest.exists():
        if _hash(dest) == item["content_hash"]:  # finished before a crash
            return _set(conn, item, "done")
        return _set(conn, item, "failed", "a different file is already at the destination")
    conn.execute("UPDATE export_items SET status = 'copying' WHERE id = ?", (item["id"],))
    tmp = dest.with_name(dest.name + TMP_SUFFIX)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        hasher = blake3.blake3()
        with open(source, "rb") as src, open(tmp, "wb") as out:
            while chunk := src.read(8 << 20):
                hasher.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if hasher.digest() != item["content_hash"]:
            tmp.unlink(missing_ok=True)
            return _set(conn, item, "failed", "the source file changed since it was scanned")
        if _hash(tmp) != item["content_hash"]:  # read back what was written
            tmp.unlink(missing_ok=True)
            return _set(conn, item, "failed", "the copy did not read back correctly")
        shutil_copystat(source, tmp)
        os.replace(tmp, dest)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return _set(conn, item, "failed", str(e))
    return _set(conn, item, "done")


def shutil_copystat(src, dst) -> None:
    import shutil

    try:
        shutil.copystat(src, dst)  # keep the file date
    except OSError:
        pass


def _hash(path) -> bytes:
    h = blake3.blake3()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.digest()


def _set(conn, item, status: str, error: str | None = None) -> tuple[str, str | None]:
    conn.execute("UPDATE export_items SET status = ?, error = ? WHERE id = ?", (status, error, item["id"]))
    return status, error
