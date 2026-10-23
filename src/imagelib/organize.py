"""Organize: gather the best unique originals into one date-sorted library.

One file of every picture goes to ``<library>/YYYY/YYYY-MM-DD/<name>``, skipping:

* pictures whose every copy is marked to discard;
* smaller versions (thumbnails, web exports) of an intact original being kept;
* the extra versions in a group of similar images (same pixels, near-identical): the
  version marked keep goes in, otherwise the suggested one;
* damaged pictures, unless asked for.

Orphan thumbnails — the only surviving copy of a picture — go in.

Preparing a library only plans it, so the proposed layout can be browsed first. Then:

* **copy** mode copies each picture (nothing else changes);
* **move** mode gathers them without needing room for a second copy. A picture already on
  the library's drive is renamed into place; one on another drive is copied, checked and
  its original quarantined on its own drive (space comes back when that is purged); one
  whose copies are all on read-only drives waits. Once a picture is in the library, its
  other exact copies are quarantined too, so the old folders keep only what was not
  organized. Undo reverses it all; Purge deletes the quarantined files.

Every copy is written under a temporary name, read back and checked against its hash,
then renamed into place. Every step is journaled and resumes after a crash. Pictures
whose source drive is disconnected wait.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import blake3

from .catalog import Catalog, bump_files_version, path_key, transaction
from .discover import mtime_equivalent, to_os_path
from .plans import _remove_empty_dirs, _write_readme
from .review import VALID_DECISION, _SECONDARY, suggest_member, survives
from .volumes import QUARANTINE_DIR

TMP_SUFFIX = ".imagelib-partial"
COPY, MOVE = "copy", "move"
RENAME, TRANSFER, WAIT = "rename", "transfer", "wait"   # how a picture gets into a moved-into library


@dataclass
class Options:
    include_videos: bool = True
    include_raw: bool = True
    include_damaged: bool = False
    mode: str = COPY


@dataclass
class Progress:
    export_id: int
    state: str = "running"
    total: int = 0
    done: int = 0
    copied: int = 0             # pictures now in the library (copied, renamed or transferred)
    copied_bytes: int = 0
    failed: int = 0
    waiting: int = 0
    extras: int = 0             # other copies quarantined
    current: str = ""
    message: str = ""
    waiting_volumes: list[str] = field(default_factory=list)


class Refused(Exception):
    pass


def _selection(cat: Catalog, opts: Options) -> list:
    kinds = ["image", "psd"] + (["raw"] if opts.include_raw else []) + (["video"] if opts.include_videos else [])
    rows = cat.conn.execute(
        f"""SELECT c.hash, c.kind, c.status, c.taken_at, c.size,
                   (SELECT min(f.mtime_ns) FROM files f WHERE f.content_hash = c.hash AND f.state != 'missing') AS mtime
            FROM contents c
            WHERE c.kind IN ({','.join('?' * len(kinds))}) AND c.status != 'sidecar'
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


def _copies(cat: Catalog, hashes: set[bytes]) -> dict[bytes, list]:
    """Every present file of the selected pictures, in one pass (not a query per picture)."""
    out: dict[bytes, list] = defaultdict(list)
    for r in cat.conn.execute(
            f"""SELECT f.id, f.content_hash, f.volume_id, f.rel_path, f.rel_path_raw, v.read_only,
                       EXISTS (SELECT 1 FROM decisions d WHERE {VALID_DECISION} AND d.decision = 'keep') AS kept,
                       EXISTS (SELECT 1 FROM decisions d WHERE {VALID_DECISION} AND d.decision = 'discard') AS discarded
                FROM files f JOIN volumes v ON v.id = f.volume_id
                WHERE f.state != 'missing' AND f.content_hash IS NOT NULL"""):
        if r["content_hash"] in hashes:
            out[r["content_hash"]].append(r)
    return out


def _best_name(copies: list) -> str:
    """The file name to use: from a copy marked keep, else one not in a backup-like folder."""
    best = min(copies, key=lambda r: (not r["kept"], bool(_SECONDARY.search(r["rel_path"])), len(r["rel_path"])))
    return best["rel_path"].rsplit("/", 1)[-1]


def _source_for_move(copies: list, target_volume: str, below: str | None) -> tuple[str, object]:
    """Which copy goes into the library, and how: rename on the library's drive, else transfer
    from a writable drive, else wait (read-only drives are never written)."""
    def rank(r):  # like the duplicates' suggested keeper: fewest backup-like signs in the path
        return (r["discarded"], not r["kept"], len(_SECONDARY.findall(r["rel_path"])), len(r["rel_path"]))

    # Names that are not valid text could not be put back exactly by Undo: never moved.
    writable = sorted((r for r in copies if not r["read_only"] and r["rel_path_raw"] is None), key=rank)
    here = [r for r in writable if r["volume_id"] == target_volume
            and not (below is not None and r["rel_path"].startswith(below))]
    if here:
        return RENAME, here[0]
    if writable:
        return TRANSFER, writable[0]
    return WAIT, sorted(copies, key=rank)[0]


def _date_folder(taken_at: str | None, mtime_ns: int | None) -> str:
    if taken_at and len(taken_at) >= 10 and taken_at[:4].isdigit() and taken_at[4] == "-":
        day = taken_at[:10]
    elif mtime_ns:
        day = time.strftime("%Y-%m-%d", time.localtime(mtime_ns / 1e9))
    else:
        return "Undated"
    return f"{day[:4]}/{day}"


def create(cat: Catalog, target: Path, opts: Options, target_volume: str | None = None,
           target_rel: str | None = None) -> dict:
    """Plan the library: what goes where, and (moving) from which copy and how. Nothing changes on disk.

    Moving needs the library's drive: ``target_volume`` and the library's path below its root.
    """
    target = Path(target).resolve()
    if opts.mode == MOVE and target_volume is None:
        raise Refused("To move pictures, the library must be on a drive in the catalog; add the drive first.")
    rows = _selection(cat, opts)
    copies = _copies(cat, {r["hash"] for r in rows})
    below = f"{target_rel}/" if target_rel else ("" if target_rel == "" else None)
    conn = cat.conn
    with transaction(conn):
        export_id = conn.execute(
            "INSERT INTO exports(target, options, created_at, mode, state, target_volume, target_rel) "
            "VALUES(?, ?, ?, ?, 'planned', ?, ?)",
            (str(target), json.dumps(vars(opts)), time.time(), opts.mode, target_volume, target_rel)).lastrowid
        used: set[str] = set()
        items, extras = [], []
        for r in sorted(rows, key=lambda r: (r["taken_at"] or "", r["hash"])):
            found = copies.get(r["hash"])
            if not found:
                continue
            name = _best_name(found)
            folder = _date_folder(r["taken_at"], r["mtime"])
            dest = f"{folder}/{name}"
            stem, dot, ext = name.rpartition(".")
            n = 2
            while dest.lower() in used:
                dest = f"{folder}/{stem} ({n}).{ext}" if dot else f"{folder}/{name} ({n})"
                n += 1
            used.add(dest.lower())
            if opts.mode == MOVE:
                method, source = _source_for_move(found, target_volume, below)
                items.append((export_id, r["hash"], r["size"], dest, method, source["id"]))
                if method != WAIT:
                    extras += [(export_id, r["hash"], c["id"]) for c in found
                               if c["id"] != source["id"] and not c["read_only"] and c["rel_path_raw"] is None]
            else:
                items.append((export_id, r["hash"], r["size"], dest, COPY, None))
        conn.executemany("INSERT INTO export_items(export_id, content_hash, size, dest, method, source_file_id, "
                         "status) VALUES(?, ?, ?, ?, ?, ?, 'pending')", items)
        conn.executemany("INSERT INTO export_extras(export_id, content_hash, file_id, status) "
                         "VALUES(?, ?, ?, 'pending')", extras)
    return {"id": export_id, "items": len(items), "bytes": sum(i[2] for i in items)}


def discard(cat: Catalog, export_id: int) -> None:
    """Forget a planned library that was never run."""
    conn = cat.conn
    row = conn.execute("SELECT state FROM exports WHERE id = ?", (export_id,)).fetchone()
    if row is None or row["state"] != "planned":
        raise Refused("Only a library that was not started can be discarded.")
    with transaction(conn):
        conn.execute("DELETE FROM export_extras WHERE export_id = ?", (export_id,))
        conn.execute("DELETE FROM export_items WHERE export_id = ?", (export_id,))
        conn.execute("DELETE FROM exports WHERE id = ?", (export_id,))


def detail(cat: Catalog, export_id: int) -> dict | None:
    conn = cat.conn
    row = conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
    if row is None:
        return None
    counts = {r[0]: {"files": r[1], "bytes": r[2] or 0} for r in conn.execute(
        "SELECT status, count(*), sum(size) FROM export_items WHERE export_id = ? GROUP BY status", (export_id,))}
    extras = {r[0]: r[1] for r in conn.execute(
        "SELECT status, count(*) FROM export_extras WHERE export_id = ? GROUP BY status", (export_id,))}
    return {"id": export_id, "target": row["target"], "options": json.loads(row["options"]),
            "created_at": row["created_at"], "mode": row["mode"], "state": row["state"],
            "counts": counts, "extras": extras}


def list_exports(cat: Catalog) -> list[dict]:
    return [detail(cat, r[0]) for r in cat.conn.execute("SELECT id FROM exports ORDER BY id DESC")]


# -- preview -----------------------------------------------------------------------------------

def summary(cat: Catalog, export_id: int, online: dict[str, Path]) -> dict:
    """What running the plan would do, per source drive, and the room it needs."""
    conn = cat.conn
    exp = conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
    if exp is None:
        raise KeyError(export_id)
    labels = dict(conn.execute("SELECT id, label FROM volumes"))
    drives: dict[str, dict] = {}
    for vol, method, n, size in conn.execute(
            "SELECT f.volume_id, i.method, count(*), sum(i.size) FROM export_items i "
            "LEFT JOIN files f ON f.id = i.source_file_id WHERE i.export_id = ? AND i.status != 'done' "
            "GROUP BY 1, 2", (export_id,)):
        d = drives.setdefault(vol or "", {"volume_id": vol, "label": labels.get(vol, "any connected drive"),
                                          "online": vol in online if vol else True, "methods": {}})
        d["methods"][method] = {"files": n, "bytes": size or 0}
    for vol, n in conn.execute(
            "SELECT f.volume_id, count(*) FROM export_extras x JOIN files f ON f.id = x.file_id "
            "WHERE x.export_id = ? AND x.status = 'pending' GROUP BY 1", (export_id,)):
        d = drives.setdefault(vol, {"volume_id": vol, "label": labels.get(vol, "?"), "online": vol in online,
                                    "methods": {}})
        d["extras"] = n
    needed = conn.execute("SELECT coalesce(sum(size), 0) FROM export_items WHERE export_id = ? "
                          "AND method IN ('copy', 'transfer') AND status != 'done'", (export_id,)).fetchone()[0]
    free = None
    target = Path(exp["target"])
    probe = next((p for p in [target, *target.parents] if p.exists()), None)
    if probe is not None:
        free = shutil.disk_usage(probe).free
    return {**detail(cat, export_id), "drives": list(drives.values()), "needed_bytes": needed, "free_bytes": free,
            "target_label": labels.get(exp["target_volume"]) if exp["target_volume"] else None}


def browse(cat: Catalog, export_id: int, folder: str = "", offset: int = 0, limit: int = 200) -> dict:
    """One folder of the planned library: its sub-folders, and the pictures landing directly in it."""
    conn = cat.conn
    prefix = folder.strip("/") + "/" if folder.strip("/") else ""
    start = len(prefix) + 1
    rng = (" AND dest >= ? AND dest < ?", [prefix, prefix[:-1] + "0"]) if prefix else ("", [])
    folders = [{"name": r[0], "path": prefix + r[0], "files": r[1], "bytes": r[2]} for r in conn.execute(
        f"SELECT substr(dest, ?, instr(substr(dest, ?), '/') - 1) AS name, count(*), sum(size) FROM export_items "
        f"WHERE export_id = ?{rng[0]} AND instr(substr(dest, ?), '/') > 0 GROUP BY name ORDER BY name",
        [start, start, export_id, *rng[1], start])]
    labels = dict(conn.execute("SELECT id, label FROM volumes"))
    rows = conn.execute(
        f"""SELECT i.dest, i.content_hash, i.size, i.method, i.status, i.error, f.volume_id, f.rel_path,
                   c.has_preview, c.width, c.height, c.taken_at, c.kind
            FROM export_items i LEFT JOIN files f ON f.id = i.source_file_id
            LEFT JOIN contents c ON c.hash = i.content_hash
            WHERE i.export_id = ?{rng[0]} AND instr(substr(i.dest, ?), '/') = 0
            ORDER BY i.dest LIMIT ? OFFSET ?""", [export_id, *rng[1], start, limit, offset]).fetchall()
    total = conn.execute(f"SELECT count(*) FROM export_items WHERE export_id = ?{rng[0]} "
                         f"AND instr(substr(dest, ?), '/') = 0", [export_id, *rng[1], start]).fetchone()[0]
    files = [{"dest": r["dest"], "name": r["dest"][len(prefix):], "hash": r["content_hash"].hex(), "size": r["size"],
              "method": r["method"], "status": r["status"], "error": r["error"],
              "source_label": labels.get(r["volume_id"]) if r["volume_id"] else None, "source_path": r["rel_path"],
              "has_preview": bool(r["has_preview"]), "width": r["width"], "height": r["height"],
              "taken_at": r["taken_at"], "kind": r["kind"]} for r in rows]
    return {"folder": prefix.rstrip("/"), "folders": folders, "files": files, "total_files": total}


# -- running -------------------------------------------------------------------------------------

def run(cat: Catalog, export_id: int, online: dict[str, Path], should_stop: Callable[[], bool] = lambda: False,
        on_progress: Callable[[Progress], None] = lambda p: None) -> Progress:
    conn = cat.conn
    exp = conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
    if exp["state"] == "undone":
        prog = Progress(export_id, state="failed", message="This library was undone; prepare a new one.")
        on_progress(prog)
        return prog
    with transaction(conn):
        conn.execute("UPDATE exports SET state = 'started' WHERE id = ?", (export_id,))
    if exp["mode"] == MOVE:
        return _run_move(cat, exp, online, should_stop, on_progress)
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
    try:
        _checked_copy(source, dest, item["content_hash"])
    except OSError as e:
        return _set(conn, item, "failed", str(e))
    return _set(conn, item, "done")


def _checked_copy(source, dest: Path, content_hash: bytes) -> None:
    """Copy under a temporary name, check it, give it the original's date, then put it in place."""
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
        if hasher.digest() != content_hash:
            raise OSError("the source file changed since it was scanned")
        if _hash(tmp) != content_hash:  # read back what was written
            raise OSError("the copy did not read back correctly")
        shutil_copystat(source, tmp)
        if dest.exists():
            raise OSError("a different file is already at the destination")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def shutil_copystat(src, dst) -> None:
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
    with transaction(conn):
        conn.execute("UPDATE export_items SET status = ?, error = ? WHERE id = ?", (status, error, item["id"]))
    return status, error


# -- moving --------------------------------------------------------------------------------------

def _library_rel(exp, dest: str) -> str:
    """A library file's path on its drive."""
    return f"{exp['target_rel']}/{dest}" if exp["target_rel"] else dest


def _quarantine_rel(root: Path, export_id: int, rel_path: str) -> str:
    base = f"{QUARANTINE_DIR}/organize-{export_id}/{rel_path}"
    candidate, n = base, 1
    while os.path.lexists(to_os_path(root, candidate, None)):
        n += 1
        stem, dot, ext = base.rpartition(".")
        candidate = f"{stem} ({n}).{ext}" if dot else f"{base} ({n})"
    return candidate


def _unchanged(path, row) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_size == row["size"] and mtime_equivalent(st.st_mtime_ns, row["mtime_ns"])


def _run_move(cat: Catalog, exp, online: dict[str, Path], should_stop, on_progress) -> Progress:
    conn = cat.conn
    export_id = exp["id"]
    prog = Progress(export_id)
    target_root = online.get(exp["target_volume"])
    if target_root is None:
        prog.state, prog.message = "paused", "Connect the drive the library is on."
        on_progress(prog)
        return prog
    _reconcile(conn, exp, online)
    items = conn.execute("SELECT * FROM export_items WHERE export_id = ? AND method IN (?, ?) "
                         "AND status IN ('pending', 'failed', 'copied') ORDER BY id",
                         (export_id, RENAME, TRANSFER)).fetchall()
    prog.total = len(items)
    prog.waiting = conn.execute("SELECT count(*) FROM export_items WHERE export_id = ? AND method = ?",
                                (export_id, WAIT)).fetchone()[0]
    needed = sum(i["size"] for i in items if i["method"] == TRANSFER and i["status"] != "copied")
    free = shutil.disk_usage(target_root).free
    if needed and free < needed + (256 << 20):
        prog.state = "paused"
        prog.message = (f"Not enough free space on the library's drive: {needed / 1e9:.1f} GB to copy, "
                        f"{free / 1e9:.1f} GB free. Purge earlier quarantines or free some space.")
        on_progress(prog)
        return prog
    on_progress(prog)
    waiting_volumes: set[str] = set()
    for item in items:
        if should_stop():
            prog.state = "paused"
            break
        src = conn.execute("SELECT * FROM files WHERE id = ?", (item["source_file_id"],)).fetchone()
        root = online.get(src["volume_id"]) if src else None
        if root is None:
            prog.waiting += 1
            waiting_volumes.add(src["volume_id"] if src else "?")
            prog.done += 1
            continue
        prog.current = item["dest"]
        error = _move_one(conn, exp, item, src, root, target_root)
        prog.done += 1
        if error:
            prog.failed += 1
            _set(conn, item, "failed", error)
        else:
            prog.copied += 1
            prog.copied_bytes += item["size"]
        if prog.done % 100 == 0:
            on_progress(prog)
    else:
        prog.state = "done"
    prog.extras = _quarantine_extras(conn, exp, online, should_stop)
    with transaction(conn):
        bump_files_version(conn)
    labels = dict(conn.execute("SELECT id, label FROM volumes"))
    prog.waiting_volumes = sorted(labels.get(v, v) for v in waiting_volumes)
    prog.current = ""
    on_progress(prog)
    return prog


def _move_one(conn, exp, item, src, root: Path, target_root: Path) -> str | None:
    source = to_os_path(root, src["rel_path"], src["rel_path_raw"])
    lib_rel = _library_rel(exp, item["dest"])
    dest = Path(to_os_path(target_root, lib_rel, None))
    if item["status"] == "copied":  # transferred before a crash: the original still needs quarantining
        return _quarantine_original(conn, exp, item, src, root)
    if src["state"] == "missing" or not _unchanged(source, src):
        return "the file changed or is gone since it was scanned; scan its drive again"
    if os.path.lexists(dest):
        return "a different file is already at the destination"
    if item["method"] == RENAME:
        with transaction(conn):
            conn.execute("UPDATE export_items SET status = 'moving' WHERE id = ?", (item["id"],))
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, dest)
        except OSError as e:
            _set(conn, item, "pending")
            return str(e)
        _prune(root, src["rel_path"], None)  # folders left empty by the move (Undo recreates them)
        return _record_rename(conn, exp, item, lib_rel, src["rel_path"])
    with transaction(conn):
        conn.execute("UPDATE export_items SET status = 'copying' WHERE id = ?", (item["id"],))
    try:
        _checked_copy(source, dest, item["content_hash"])
    except OSError as e:
        return str(e)
    _record_transfer(conn, exp, item, src, lib_rel)
    return _quarantine_original(conn, exp, item, src, root)


def _record_rename(conn, exp, item, lib_rel: str, original: str) -> str | None:
    try:
        with transaction(conn):
            conn.execute("UPDATE files SET rel_path = ?, rel_path_raw = NULL, path_key = ? WHERE id = ?",
                         (lib_rel, path_key(lib_rel), item["source_file_id"]))
            conn.execute("UPDATE export_items SET status = 'done', error = NULL, source_path = ? WHERE id = ?",
                         (original, item["id"]))
    except sqlite3.IntegrityError:
        return "the catalog already has a file at this place in the library"
    return None


def _record_transfer(conn, exp, item, src, lib_rel: str) -> None:
    now = time.time()
    with transaction(conn):
        new_id = conn.execute(
            "INSERT INTO files(volume_id, rel_path, path_key, kind, size, mtime_ns, state, content_hash, "
            "first_seen_at, last_seen_scan, analyzed_at) VALUES(?, ?, ?, ?, ?, ?, 'done', ?, ?, 0, ?)",
            (exp["target_volume"], lib_rel, path_key(lib_rel), src["kind"], src["size"], src["mtime_ns"],
             item["content_hash"], now, now)).lastrowid
        conn.execute("UPDATE export_items SET status = 'copied', dest_file_id = ? WHERE id = ?", (new_id, item["id"]))


def _quarantine_original(conn, exp, item, src, root: Path) -> str | None:
    q = _quarantine_rel(root, exp["id"], src["rel_path"])
    try:
        dst = to_os_path(root, q, None)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _write_readme(root)
        os.rename(to_os_path(root, src["rel_path"], src["rel_path_raw"]), dst)
    except OSError as e:
        return f"copied into the library, but the original could not be quarantined: {e}"
    _prune(root, src["rel_path"], None)
    with transaction(conn):
        conn.execute("UPDATE files SET state = 'missing', error = ? WHERE id = ?",
                     (f"moved into the organized library (organize {exp['id']}); original in quarantine", src["id"]))
        conn.execute("UPDATE export_items SET status = 'done', error = NULL, quarantine_path = ? WHERE id = ?",
                     (q, item["id"]))
    return None


def _quarantine_extras(conn, exp, online: dict[str, Path], should_stop) -> int:
    """The other exact copies of pictures now in the library."""
    done = 0
    for x in conn.execute(
            """SELECT x.id, x.file_id, f.volume_id, f.rel_path, f.rel_path_raw, f.size, f.mtime_ns, f.state
               FROM export_extras x JOIN files f ON f.id = x.file_id
               JOIN export_items i ON i.export_id = x.export_id AND i.content_hash = x.content_hash
               WHERE x.export_id = ? AND x.status = 'pending' AND i.status = 'done'""", (exp["id"],)).fetchall():
        if should_stop():
            break
        root = online.get(x["volume_id"])
        if root is None or x["state"] == "missing":
            continue
        source = to_os_path(root, x["rel_path"], x["rel_path_raw"])
        if not _unchanged(source, x):
            continue  # changed since scanned: not an exact copy any more; leave it
        q = _quarantine_rel(root, exp["id"], x["rel_path"])
        try:
            dst = to_os_path(root, q, None)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _write_readme(root)
            os.rename(source, dst)
        except OSError:
            continue
        _prune(root, x["rel_path"], None)
        with transaction(conn):
            conn.execute("UPDATE files SET state = 'missing', error = ? WHERE id = ?",
                         (f"copy of a picture in the organized library (organize {exp['id']}); in quarantine",
                          x["file_id"]))
            conn.execute("UPDATE export_extras SET status = 'quarantined', quarantine_path = ? WHERE id = ?",
                         (q, x["id"]))
        done += 1
    return done


def _reconcile(conn, exp, online: dict[str, Path]) -> None:
    """Settle steps interrupted by a crash, from where the files actually are."""
    target_root = online.get(exp["target_volume"])
    for item in conn.execute("SELECT * FROM export_items WHERE export_id = ? AND status IN ('moving', 'copying')",
                             (exp["id"],)).fetchall():
        src = conn.execute("SELECT * FROM files WHERE id = ?", (item["source_file_id"],)).fetchone()
        root = online.get(src["volume_id"])
        if root is None or target_root is None:
            continue
        lib_rel = _library_rel(exp, item["dest"])
        dest = to_os_path(target_root, lib_rel, None)
        if item["status"] == "moving":
            if os.path.lexists(dest) and not os.path.lexists(to_os_path(root, src["rel_path"], src["rel_path_raw"])):
                _record_rename(conn, exp, item, lib_rel, src["rel_path"])
            else:
                _set(conn, item, "pending")
        elif os.path.lexists(dest) and _hash(dest) == item["content_hash"]:  # copied, not yet recorded
            _record_transfer(conn, exp, item, src, lib_rel)
        else:
            _set(conn, item, "pending")


# -- undo and purge ------------------------------------------------------------------------------

def undo(cat: Catalog, export_id: int, online: dict[str, Path]) -> dict:
    """Put every moved picture and quarantined copy back where it was."""
    conn = cat.conn
    exp = conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
    if exp is None or exp["mode"] != MOVE or exp["state"] != "started":
        raise Refused("Only a library that moved pictures can be undone.")
    target_root = online.get(exp["target_volume"])
    if target_root is None:
        raise Refused("Connect the drive the library is on.")
    _reconcile(conn, exp, online)
    problems: list[str] = []
    for x in conn.execute("SELECT x.*, f.volume_id, f.rel_path, f.rel_path_raw FROM export_extras x "
                          "JOIN files f ON f.id = x.file_id WHERE x.export_id = ? AND x.status = 'quarantined'",
                          (export_id,)).fetchall():
        root = online.get(x["volume_id"])
        if root is None:
            problems.append(f"{x['rel_path']}: its drive is not connected")
            continue
        if _restore(root, x["quarantine_path"], x["rel_path"], x["rel_path_raw"], problems):
            with transaction(conn):
                conn.execute("UPDATE files SET state = 'done', error = NULL WHERE id = ?", (x["file_id"],))
                conn.execute("UPDATE export_extras SET status = 'restored' WHERE id = ?", (x["id"],))
    for item in conn.execute("SELECT * FROM export_items WHERE export_id = ? AND status IN ('done', 'copied') "
                             "ORDER BY id DESC", (export_id,)).fetchall():
        src = conn.execute("SELECT * FROM files WHERE id = ?", (item["source_file_id"],)).fetchone()
        root = online.get(src["volume_id"])
        lib_rel = _library_rel(exp, item["dest"])
        if root is None:
            problems.append(f"{item['dest']}: the drive it came from is not connected")
            continue
        if item["method"] == RENAME:
            back = item["source_path"]
            if _restore(target_root, lib_rel, back, None, problems, root):
                with transaction(conn):
                    conn.execute("UPDATE files SET rel_path = ?, path_key = ? WHERE id = ?",
                                 (back, path_key(back), src["id"]))
                    conn.execute("UPDATE export_items SET status = 'pending' WHERE id = ?", (item["id"],))
                _prune(target_root, lib_rel, exp["target_rel"])
            continue
        # Transfer: the original comes back from quarantine; the library copy goes (if unchanged).
        if item["quarantine_path"] and not _restore(root, item["quarantine_path"], src["rel_path"],
                                                    src["rel_path_raw"], problems):
            continue
        copy = conn.execute("SELECT * FROM files WHERE id = ?", (item["dest_file_id"],)).fetchone()
        path = to_os_path(target_root, lib_rel, None)
        if copy is not None and _unchanged(path, copy):
            os.remove(path)
            _prune(target_root, lib_rel, exp["target_rel"])
        elif os.path.lexists(path):
            problems.append(f"{item['dest']}: changed in the library since; left there")
        with transaction(conn):
            conn.execute("UPDATE files SET state = 'done', error = NULL WHERE id = ?", (src["id"],))
            if copy is not None:
                try:
                    conn.execute("DELETE FROM files WHERE id = ?", (copy["id"],))
                except sqlite3.IntegrityError:
                    conn.execute("UPDATE files SET state = 'missing', error = ? WHERE id = ?",
                                 (f"removed by undoing organize {export_id}", copy["id"]))
            conn.execute("UPDATE export_items SET status = 'pending', dest_file_id = NULL, quarantine_path = NULL "
                         "WHERE id = ?", (item["id"],))
    with transaction(conn):
        if not problems:
            conn.execute("UPDATE exports SET state = 'undone' WHERE id = ?", (export_id,))
        bump_files_version(conn)
    return {"problems": problems}


def _restore(root: Path, from_rel: str, to_rel: str, to_raw, problems: list[str], to_root: Path | None = None) -> bool:
    src = to_os_path(root, from_rel, None)
    dst = to_os_path(to_root or root, to_rel, to_raw)
    if os.path.lexists(dst):
        problems.append(f"{to_rel}: something is there again; left at {from_rel}")
        return False
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
    except OSError as e:
        problems.append(f"{to_rel}: {e}")
        return False
    return True


def _prune(root: Path, rel: str, top: str | None) -> None:
    """Remove the folders above ``rel`` left empty, up to ``top`` (the drive's top when None)."""
    folder = os.path.dirname(to_os_path(root, rel, None))
    stop = to_os_path(root, top, None) if top else str(root)
    while len(folder) > len(stop) and folder.startswith(stop):
        try:
            os.rmdir(folder)
        except OSError:
            return
        folder = os.path.dirname(folder)


def purge(cat: Catalog, export_id: int, online: dict[str, Path]) -> dict:
    """Delete this library's quarantined originals and copies, on every connected drive."""
    conn = cat.conn
    exp = conn.execute("SELECT * FROM exports WHERE id = ?", (export_id,)).fetchone()
    if exp is None or exp["mode"] != MOVE or exp["state"] != "started":
        raise Refused("Only a library that moved pictures has quarantined files.")
    freed, waiting = 0, set()
    vols = {r[0] for r in conn.execute(
        "SELECT f.volume_id FROM export_extras x JOIN files f ON f.id = x.file_id "
        "WHERE x.export_id = ? AND x.status = 'quarantined' UNION SELECT f.volume_id FROM export_items i "
        "JOIN files f ON f.id = i.source_file_id WHERE i.export_id = ? AND i.quarantine_path IS NOT NULL",
        (export_id, export_id))}
    labels = dict(conn.execute("SELECT id, label FROM volumes"))
    for vid in vols:
        root = online.get(vid)
        if root is None:
            waiting.add(labels.get(vid, vid))
            continue
        top = Path(to_os_path(root, f"{QUARANTINE_DIR}/organize-{export_id}", None))
        for dirpath, _, names in os.walk(top):
            for name in names:
                try:
                    freed += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
        shutil.rmtree(top, ignore_errors=True)
        _remove_empty_dirs(top)
        with transaction(conn):
            conn.execute("UPDATE export_extras SET status = 'purged' WHERE export_id = ? AND status = 'quarantined' "
                         "AND file_id IN (SELECT id FROM files WHERE volume_id = ?)", (export_id, vid))
            conn.execute("UPDATE export_items SET quarantine_path = NULL WHERE export_id = ? AND source_file_id IN "
                         "(SELECT id FROM files WHERE volume_id = ?)", (export_id, vid))
    return {"bytes": freed, "waiting": sorted(waiting)}
