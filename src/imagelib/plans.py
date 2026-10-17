"""Action plans: carrying out discard decisions, safely and reversibly.

A plan is a snapshot of the current discard decisions. Running it moves each file
into ``_imagelib_quarantine/plan-<id>/<original path>`` on its own drive: a rename,
so instant, and undoable with Restore. Purge deletes quarantined files for good.

Every file is checked again at the moment it is moved:

* the decision still stands, and the file is still there, unchanged (size and
  date; optionally its full content hash);
* the last-copy rule holds *now*: another copy or intact version of the picture
  exists outside this plan, and by default it must be on a drive that is connected
  and actually be found there.

Each move is journaled (``moving`` before the rename, ``quarantined`` after), so a
crash mid-move is reconciled on the next run. Drives registered read-only are never
written to: their files are listed for the user to delete by hand.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import blake3

from .catalog import Catalog, bump_files_version
from .discover import mtime_equivalent, to_os_path
from .review import VALID_DECISION
from .volumes import QUARANTINE_DIR, VolumeRef

ACTIVE = ("pending", "moving", "quarantined", "failed", "blocked", "relayed")
RUNNABLE = ("pending", "failed", "blocked")

QUARANTINE_README = """Files in this folder were set aside by imagelib (Image Library) as duplicates,
damaged files or generated thumbnails you chose to discard.

Nothing here has been deleted. Each plan-N folder keeps the original folder layout,
so a file can be restored by moving it back, or with "Restore" in the app.
Delete this folder (or use "Purge" in the app) only when you are sure.
"""


# -- creating plans ------------------------------------------------------------------------

def create(cat: Catalog) -> dict:
    """Snapshot every discard decision not already in an active plan into a new plan."""
    conn = cat.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("INSERT INTO plans(created_at, machine_id) VALUES(?, ?)", (time.time(), cat.machine_id))
        plan_id = cur.lastrowid
        conn.execute(
            f"""INSERT INTO plan_items(plan_id, file_id, volume_id, rel_path, rel_path_raw, content_hash, size,
                                       mtime_ns, reason, status, updated_at)
                SELECT ?, f.id, f.volume_id, f.rel_path, f.rel_path_raw, f.content_hash, f.size, f.mtime_ns,
                       d.reason, 'pending', ?
                FROM decisions d JOIN files f ON {VALID_DECISION}
                WHERE d.decision = 'discard' AND f.state != 'missing'
                AND NOT EXISTS (SELECT 1 FROM plan_items i WHERE i.file_id = f.id
                                AND i.status IN ({','.join('?' * len(ACTIVE))}))""",
            (plan_id, time.time(), *ACTIVE))
        count = conn.execute("SELECT count(*) FROM plan_items WHERE plan_id = ?", (plan_id,)).fetchone()[0]
        if count == 0:
            conn.execute("ROLLBACK")
            return {"id": None, "items": 0}
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"id": plan_id, "items": count}


def list_plans(cat: Catalog) -> list[dict]:
    out = []
    for p in cat.conn.execute("SELECT * FROM plans ORDER BY id DESC"):
        counts = {r[0]: {"files": r[1], "bytes": r[2]} for r in cat.conn.execute(
            "SELECT status, count(*), sum(size) FROM plan_items WHERE plan_id = ? GROUP BY status", (p["id"],))}
        out.append({"id": p["id"], "created_at": p["created_at"], "counts": counts})
    return out


def detail(cat: Catalog, plan_id: int, online: dict[str, Path]) -> dict | None:
    plan = cat.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        return None
    volumes = {}
    for r in cat.conn.execute(
            "SELECT i.volume_id, v.label, v.read_only, i.status, count(*) AS n, sum(i.size) AS bytes "
            "FROM plan_items i JOIN volumes v ON v.id = i.volume_id WHERE i.plan_id = ? "
            "GROUP BY i.volume_id, i.status ORDER BY v.label", (plan_id,)):
        v = volumes.setdefault(r["volume_id"], {
            "volume_id": r["volume_id"], "label": r["label"], "read_only": bool(r["read_only"]),
            "online": r["volume_id"] in online, "counts": {}})
        v["counts"][r["status"]] = {"files": r["n"], "bytes": r["bytes"]}
    return {"id": plan_id, "created_at": plan["created_at"], "volumes": list(volumes.values())}


def items(cat: Catalog, plan_id: int, status: str | None = None, volume: str | None = None,
          after: int = 0, limit: int = 200) -> dict:
    sql = ("SELECT i.*, v.label FROM plan_items i JOIN volumes v ON v.id = i.volume_id "
           "WHERE i.plan_id = ? AND i.id > ?")
    args: list = [plan_id, after]
    if status:
        sql += " AND i.status = ?"
        args.append(status)
    if volume:
        sql += " AND i.volume_id = ?"
        args.append(volume)
    rows = cat.conn.execute(sql + " ORDER BY i.id LIMIT ?", args + [limit]).fetchall()
    return {"items": [{
        "id": r["id"], "file_id": r["file_id"], "volume_id": r["volume_id"], "volume_label": r["label"],
        "path": r["rel_path"], "size": r["size"], "reason": r["reason"], "status": r["status"],
        "error": r["error"], "quarantine_path": r["quarantine_path"],
        "hash": r["content_hash"].hex() if r["content_hash"] else None} for r in rows],
        "next": rows[-1]["id"] if len(rows) == limit else None}


def manual_list(cat: Catalog, plan_id: int, volume_id: str, root: str | None) -> str:
    """Files on a read-only drive for the user to delete themselves (never done by the app)."""
    vol = cat.conn.execute("SELECT label FROM volumes WHERE id = ?", (volume_id,)).fetchone()
    lines = [f"# Files marked to discard on '{vol['label'] if vol else volume_id}' (plan {plan_id}).",
             "# This drive is registered read-only, so imagelib will not touch it.",
             "# Delete or move these yourself if you wish; a later scan will notice they are gone.",
             ""]
    for r in cat.conn.execute("SELECT rel_path FROM plan_items WHERE plan_id = ? AND volume_id = ? "
                              "AND status IN ('pending', 'failed', 'blocked') ORDER BY rel_path",
                              (plan_id, volume_id)):
        lines.append(_display_path(root, r[0]))
    return "\n".join(lines) + "\n"


def _display_path(root: str | None, rel: str) -> str:
    if not root:
        return rel
    windows = len(root) >= 2 and root[1] == ":"
    return root.rstrip("/\\") + ("\\" + rel.replace("/", "\\") if windows else "/" + rel)


# -- running plans ---------------------------------------------------------------------------

@dataclass
class RunOptions:
    verify_hash: bool = True
    require_connected_copy: bool = True


@dataclass
class RunProgress:
    plan_id: int
    state: str = "running"           # running | paused | done | failed
    total: int = 0
    done: int = 0
    moved: int = 0
    moved_bytes: int = 0
    blocked: int = 0
    skipped: int = 0
    failed: int = 0
    current: str = ""
    message: str = ""
    waiting_volumes: list[str] = field(default_factory=list)


class _Stop(Exception):
    pass


def run(cat: Catalog, plan_id: int, online: dict[str, Path], refs: dict[str, VolumeRef],
        options: RunOptions, should_stop: Callable[[], bool] = lambda: False,
        on_progress: Callable[[RunProgress], None] = lambda p: None) -> RunProgress:
    """Move every runnable item on connected, writable drives into quarantine."""
    conn = cat.conn
    _reconcile(cat, plan_id, online)
    prog = RunProgress(plan_id)
    rows = conn.execute(
        f"SELECT i.*, v.read_only FROM plan_items i JOIN volumes v ON v.id = i.volume_id "
        f"WHERE i.plan_id = ? AND i.status IN ({','.join('?' * len(RUNNABLE))}) ORDER BY i.volume_id, i.rel_path",
        (plan_id, *RUNNABLE)).fetchall()
    todo = [r for r in rows if not r["read_only"] and r["volume_id"] in online]
    prog.waiting_volumes = sorted({r["volume_id"] for r in rows if not r["read_only"] and r["volume_id"] not in online})
    prog.total = len(todo)
    on_progress(prog)
    readmes: set[str] = set()
    try:
        for item in todo:
            if should_stop():
                prog.state = "paused"
                raise _Stop
            root = online[item["volume_id"]]
            prog.current = item["rel_path"]
            ref = refs[item["volume_id"]]
            status, error, qpath = _process(cat, plan_id, item, root, online, options,
                                            present=lambda: ref.present_at(root))
            if status == "offline":
                prog.state, prog.message = "paused", f"drive '{ref.label}' disconnected"
                raise _Stop
            if status == "quarantined":
                prog.moved += 1
                prog.moved_bytes += item["size"]
                if item["volume_id"] not in readmes:
                    _write_readme(root)
                    readmes.add(item["volume_id"])
            elif status == "blocked":
                prog.blocked += 1
            elif status == "skipped":
                prog.skipped += 1
            elif status == "failed":
                prog.failed += 1
            prog.done += 1
            on_progress(prog)
        prog.state = "done"
    except _Stop:
        pass
    prog.current = ""
    on_progress(prog)
    return prog


def _set(conn: sqlite3.Connection, item_id: int, status: str, error: str | None = None,
         qpath: str | None = None) -> None:
    conn.execute("UPDATE plan_items SET status = ?, error = ?, quarantine_path = coalesce(?, quarantine_path), "
                 "updated_at = ? WHERE id = ?", (status, error, qpath, time.time(), item_id))


def _process(cat: Catalog, plan_id: int, item, root: Path, online: dict[str, Path],
             options: RunOptions, present: Callable[[], bool]) -> tuple[str, str | None, str | None]:
    """Check one item and move it. Returns ("offline", ...) without recording anything when
    the drive itself has gone, so an unplugged drive never turns into skipped files."""
    conn = cat.conn
    # 1. The decision still stands.
    dec = conn.execute(
        f"SELECT 1 FROM files f JOIN decisions d ON {VALID_DECISION} WHERE f.id = ? AND d.decision = 'discard' "
        f"AND f.content_hash IS ?", (item["file_id"], item["content_hash"])).fetchone()
    if dec is None:
        return _finish(conn, item, "skipped", "the discard decision was withdrawn or the file changed since")
    # 2. The file is still there and unchanged.
    src = to_os_path(root, item["rel_path"], item["rel_path_raw"])
    try:
        st = os.stat(src)
    except OSError as e:
        if not present():
            return "offline", None, None
        if isinstance(e, FileNotFoundError):
            return _finish(conn, item, "skipped", "the file is no longer there")
        return _finish(conn, item, "failed", str(e))
    if st.st_size != item["size"] or not mtime_equivalent(st.st_mtime_ns, item["mtime_ns"]):
        return _finish(conn, item, "skipped", "the file changed since it was scanned")
    if options.verify_hash and item["content_hash"] is not None:
        try:
            if _hash_file(src) != item["content_hash"]:
                return _finish(conn, item, "skipped", "the file's contents changed since it was scanned")
        except OSError as e:
            if not present():
                return "offline", None, None
            return _finish(conn, item, "failed", f"could not read the file: {e}")
    # 3. Another copy or version survives, now.
    problem = survivor_problem(conn, item, online, options.require_connected_copy)
    if problem:
        return _finish(conn, item, "blocked", problem)
    # 4. Move, journaled.
    qrel = _quarantine_rel(root, plan_id, item["rel_path"])
    dst = to_os_path(root, qrel, None)
    conn.execute("BEGIN IMMEDIATE")
    _set(conn, item["id"], "moving", None, qrel)
    conn.execute("COMMIT")
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)  # same drive: instant and atomic
    except OSError as e:
        if not present():  # leave it 'moving': the next run reconciles against the disk
            return "offline", None, None
        return _finish(conn, item, "failed", f"could not move the file: {e}")
    conn.execute("BEGIN IMMEDIATE")
    _set(conn, item["id"], "quarantined", None, qrel)
    conn.execute("UPDATE files SET state = 'missing', error = ? WHERE id = ?",
                 (f"moved to quarantine (plan {plan_id})", item["file_id"]))
    bump_files_version(conn)
    conn.execute("COMMIT")
    return "quarantined", None, qrel


def _finish(conn: sqlite3.Connection, item, status: str, error: str | None):
    conn.execute("BEGIN IMMEDIATE")
    _set(conn, item["id"], status, error)
    conn.execute("COMMIT")
    return status, error, None


def _hash_file(path) -> bytes:
    hasher = blake3.blake3(max_threads=blake3.blake3.AUTO)
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            hasher.update(chunk)
    return hasher.digest()


def _quarantine_rel(root: Path, plan_id: int, rel_path: str) -> str:
    base = f"{QUARANTINE_DIR}/plan-{plan_id}/{rel_path}"
    candidate, n = base, 1
    while os.path.lexists(to_os_path(root, candidate, None)):
        stem, dot, ext = base.rpartition(".")
        candidate = f"{stem} ({n}).{ext}" if dot else f"{base} ({n})"
        n += 1
    return candidate


def _write_readme(root: Path) -> None:
    path = Path(to_os_path(root, f"{QUARANTINE_DIR}/README.txt", None))
    if not path.exists():
        try:
            path.write_text(QUARANTINE_README, encoding="utf-8")
        except OSError:
            pass


def survivor_problem(conn: sqlite3.Connection, item, online: dict[str, Path], require_connected: bool,
                     trusted: frozenset[str] | set[str] = frozenset()) -> str | None:
    """Why moving this file would lose the picture, or None if it is safe.

    ``trusted`` volumes count as connected without a look on disk: drives an agent
    reported connected moments ago, which this machine cannot see.
    """
    h = item["content_hash"]
    if h is None:
        return None  # unreadable file: nothing to preserve
    row = conn.execute("SELECT status FROM contents WHERE hash = ?", (h,)).fetchone()
    if row is None or row[0] == "corrupt":
        return None
    # Other copies of the same content, or of an intact version it may stand in for.
    versions = [h] + [r[0] for r in conn.execute(
        f"""SELECT o.hash FROM contents o WHERE o.status = 'ok' AND o.hash != ? AND (
                o.hash IN (SELECT b FROM similar_pairs WHERE a = ? AND kind IN ('derived', 'near', 'same_pixels'))
             OR o.hash IN (SELECT a FROM similar_pairs WHERE b = ? AND kind IN ('near', 'same_pixels'))
             OR o.hash IN (SELECT m2.hash FROM similar_clusters m1 JOIN similar_clusters m2
                           ON m2.cluster = m1.cluster WHERE m1.hash = ?))""", (h, h, h, h))]
    survivors = []
    for v in versions:
        survivors += conn.execute(
            f"""SELECT f.id, f.volume_id, f.rel_path, f.rel_path_raw, f.size FROM files f
                WHERE f.content_hash = ? AND f.id != ? AND f.state != 'missing'
                AND NOT EXISTS (SELECT 1 FROM decisions d WHERE {VALID_DECISION} AND d.decision = 'discard')
                AND NOT EXISTS (SELECT 1 FROM plan_items i WHERE i.file_id = f.id
                                AND i.status IN ('pending', 'moving', 'quarantined', 'failed', 'blocked',
                                                 'relayed'))""",
            (v, item["file_id"])).fetchall()
    if not survivors:
        return "no other copy or version of this picture would be left"
    if not require_connected:
        return None
    for s in survivors:
        if s["volume_id"] in trusted:
            return None
        root = online.get(s["volume_id"])
        if root is None:
            continue
        try:
            if os.stat(to_os_path(root, s["rel_path"], s["rel_path_raw"])).st_size == s["size"]:
                return None
        except OSError:
            continue
    return ("every remaining copy is on a drive that is not connected; connect one, "
            "or allow trusting the catalog for disconnected drives")


def _reconcile(cat: Catalog, plan_id: int, online: dict[str, Path]) -> None:
    """Settle items left 'moving' by a crash, by looking at what is actually on disk."""
    conn = cat.conn
    for item in conn.execute("SELECT * FROM plan_items WHERE plan_id = ? AND status = 'moving'",
                             (plan_id,)).fetchall():
        root = online.get(item["volume_id"])
        if root is None:
            continue
        src = os.path.lexists(to_os_path(root, item["rel_path"], item["rel_path_raw"]))
        dst = item["quarantine_path"] and os.path.lexists(to_os_path(root, item["quarantine_path"], None))
        conn.execute("BEGIN IMMEDIATE")
        if dst and not src:
            _set(conn, item["id"], "quarantined")
            conn.execute("UPDATE files SET state = 'missing', error = ? WHERE id = ?",
                         (f"moved to quarantine (plan {plan_id})", item["file_id"]))
            bump_files_version(conn)
        elif src and not dst:
            _set(conn, item["id"], "pending")
        else:
            _set(conn, item["id"], "failed", "interrupted move; found the file in neither or both places")
        conn.execute("COMMIT")


# -- undo & purge ----------------------------------------------------------------------------

def _relay(conn: sqlite3.Connection, item, relay: dict[str, str], request: str) -> bool:
    """Ask the agent holding this item's drive to do it; True if one does."""
    machine = relay.get(item["volume_id"])
    if machine is None:
        return False
    conn.execute("UPDATE plan_items SET relay_machine = ?, relay_request = ?, updated_at = ? WHERE id = ?",
                 (machine, request, time.time(), item["id"]))
    return True


def restore(cat: Catalog, plan_id: int, online: dict[str, Path], item_ids: list[int] | None = None,
            relay: dict[str, str] | None = None) -> dict:
    """Move quarantined files back and withdraw their discard decisions.

    Files on drives connected to an agent (``relay``: volume id -> machine id) are
    restored by that agent on its next sync.
    """
    conn = cat.conn
    sql = "SELECT * FROM plan_items WHERE plan_id = ? AND status = 'quarantined'"
    args: list = [plan_id]
    if item_ids is not None:
        sql += f" AND id IN ({','.join('?' * len(item_ids)) or 'NULL'})"
        args += item_ids
    restored, problems, relayed = 0, [], 0
    for item in conn.execute(sql, args).fetchall():
        root = online.get(item["volume_id"])
        if root is None:
            if _relay(conn, item, relay or {}, "restore"):
                relayed += 1
            else:
                problems.append(f"{item['rel_path']}: drive not connected")
            continue
        src = to_os_path(root, item["quarantine_path"], None)
        dst = to_os_path(root, item["rel_path"], item["rel_path_raw"])
        if os.path.lexists(dst):
            problems.append(f"{item['rel_path']}: something else is now at the original location")
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
        except OSError as e:
            problems.append(f"{item['rel_path']}: {e}")
            continue
        conn.execute("BEGIN IMMEDIATE")
        _set(conn, item["id"], "restored")
        conn.execute("UPDATE files SET state = 'done', error = NULL WHERE id = ?", (item["file_id"],))
        conn.execute("DELETE FROM decisions WHERE file_id = ?", (item["file_id"],))
        bump_files_version(conn)
        conn.execute("COMMIT")
        restored += 1
    return {"restored": restored, "relayed": relayed, "problems": problems}


def purge(cat: Catalog, plan_id: int, online: dict[str, Path], relay: dict[str, str] | None = None) -> dict:
    """Permanently delete this plan's quarantined files on connected drives (or ask their agent)."""
    conn = cat.conn
    purged, freed, problems, relayed = 0, 0, [], 0
    touched: set[tuple[str, str]] = set()
    for item in conn.execute("SELECT * FROM plan_items WHERE plan_id = ? AND status = 'quarantined'",
                             (plan_id,)).fetchall():
        root = online.get(item["volume_id"])
        if root is None:
            if _relay(conn, item, relay or {}, "purge"):
                relayed += 1
            else:
                problems.append(f"{item['rel_path']}: drive not connected")
            continue
        path = to_os_path(root, item["quarantine_path"], None)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            problems.append(f"{item['rel_path']}: {e}")
            continue
        conn.execute("BEGIN IMMEDIATE")
        _set(conn, item["id"], "purged")
        conn.execute("UPDATE files SET error = ? WHERE id = ?", (f"deleted permanently (plan {plan_id})",
                                                                 item["file_id"]))
        conn.execute("COMMIT")
        purged += 1
        freed += item["size"]
        touched.add((str(root), item["volume_id"]))
    for root, _ in touched:
        _remove_empty_dirs(Path(to_os_path(Path(root), f"{QUARANTINE_DIR}/plan-{plan_id}", None)))
    return {"purged": purged, "bytes": freed, "relayed": relayed, "problems": problems}


def _remove_empty_dirs(top: Path) -> None:
    if not top.is_dir():
        return
    # Bottom-up; emptiness is checked now, since removing a child empties its parent.
    for dirpath, _, _ in os.walk(top, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass
