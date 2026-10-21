"""Merging two folder trees into one: keep one tree, bring over what only the other has.

A merge is prepared from the catalog. Every file of the tree being merged away (the
"drop" tree) is one of:

* ``covered``: its content is already somewhere in the kept tree, nothing to bring;
* ``move``: brought into the kept tree at the same relative path;
* ``conflict``: that path in the kept tree holds different content, so it is brought
  in beside it as ``name (imagelib merge N).ext``. The name is recorded here, so the
  conflicts of a merge can be found and tidied up later.

Running it checks the disk first and stops if the drop tree changed since it was
scanned (files the catalog does not know, or changed size or date) or a kept copy is
gone. Then it moves the files to bring over, each journaled (``moving`` before the
rename, ``moved`` after) so a crash is reconciled on the next run, and finally renames
the rest of the drop tree into ``_imagelib_quarantine/merge-N/<its path>`` in one step.
Undo reverses all of it; Purge deletes the quarantined tree for good.

On one drive, bringing a file over is a rename. Across drives it is a copy: written to
a temporary name, checked against the catalog's hash, given the original's date, then
renamed into place; Undo deletes such copies (if unchanged). The rest of the drop tree is
quarantined on its own drive. Both drives must be connected here and writable.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import blake3

from .catalog import Catalog, bump_files_version, path_key, transaction
from .discover import SKIP_DIRS, mtime_equivalent, to_os_path
from .plans import _remove_empty_dirs, _write_readme
from .trees import tree_files
from .volumes import MARKER_NAME, QUARANTINE_DIR

MOVE, CONFLICT, COVERED = "move", "conflict", "covered"
CONFLICT_MARK = "imagelib merge"  # in renamed conflict files: "name (imagelib merge 12).ext"


FREE_SPACE_MARGIN = 256 << 20   # left free on the receiving drive when copying across drives


class Roots(NamedTuple):
    keep: Path    # where the kept tree's drive is mounted
    drop: Path    # where the drop tree's drive is mounted

    @property
    def same(self) -> bool:
        return self.keep == self.drop


class MergeRefused(Exception):
    """The merge cannot be prepared or run as asked; the message says why."""


@dataclass
class Progress:
    merge_id: int
    state: str = "starting"    # starting | checking | moving | quarantining | merged | blocked | failed
    done: int = 0
    total: int = 0
    message: str = ""
    problems: list[str] = field(default_factory=list)


def conflict_name(rel: str, merge_id: int, taken: Callable[[str], bool]) -> str:
    """Where a conflicting file goes: beside the kept one, its name marked with the merge."""
    folder, _, name = rel.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    n = 1
    while True:
        tag = f"{CONFLICT_MARK} {merge_id}" + (f"-{n}" if n > 1 else "")
        candidate = f"{stem} ({tag})" + (f".{ext}" if ext else "")
        full = f"{folder}/{candidate}" if folder else candidate
        if not taken(full):
            return full
        n += 1


def _inside(path: str, top: str) -> bool:
    return not top or path == top or path.startswith(top + "/")


def _join(root: str, rel: str) -> str:
    return f"{root}/{rel}" if root else rel


# -- preparing ----------------------------------------------------------------------------------

def prepare(cat: Catalog, keep_volume: str, keep_root: str, drop_volume: str, drop_root: str) -> dict:
    """Plan merging the drop tree into the kept one; nothing on disk changes yet."""
    conn = cat.conn
    if not drop_root:
        raise MergeRefused("A whole drive cannot be merged away; choose a folder.")
    if keep_volume == drop_volume and (_inside(keep_root, drop_root) or _inside(drop_root, keep_root)):
        raise MergeRefused("One tree is inside the other; choose two separate folders.")
    for vid in {keep_volume, drop_volume}:
        vol = conn.execute("SELECT read_only, label FROM volumes WHERE id = ?", (vid,)).fetchone()
        if vol is None:
            raise MergeRefused("Unknown drive.")
        if vol["read_only"]:
            raise MergeRefused(f"{vol['label']} is registered read-only; nothing on it can be moved.")
    active = conn.execute(
        "SELECT id FROM merges WHERE status IN ('planned', 'running', 'blocked', 'failed') AND drop_volume = ? "
        "AND (drop_root = ? OR keep_root = ? OR drop_root = ? OR keep_root = ?)",
        (drop_volume, drop_root, drop_root, keep_root, keep_root)).fetchone()
    if active:
        raise MergeRefused(f"Merge {active[0]} already involves one of these folders; run or cancel it first.")

    keep = tree_files(conn, keep_volume, keep_root)
    drop = _drop_rows(conn, drop_volume, drop_root)
    unread = sum(1 for r in keep if r[1] is None) + sum(1 for r in drop if r["content_hash"] is None)
    if unread:
        raise MergeRefused(f"{unread:,} files in these trees are not scanned yet or could not be read; "
                           "scan the drive first so every file can be compared.")
    if not drop:
        raise MergeRefused("The tree to merge away holds no files.")
    odd = sum(1 for r in drop if r["rel_path_raw"] is not None)
    if odd:
        raise MergeRefused(f"{odd:,} file names in the tree to merge away are not valid text; "
                           "rename them first so they can be moved safely.")

    keep_hashes = {r[1] for r in keep}
    keep_paths = {r[0].lower() for r in keep}
    # Paths the catalog remembers as gone can not take a second record: bring files in beside them.
    remembered = {r[0].lower() for r in _missing_paths(conn, keep_volume, keep_root)}
    now = time.time()
    with transaction(conn):
        merge_id = conn.execute(
            "INSERT INTO merges(keep_volume, keep_root, drop_volume, drop_root, status, created_at) "
            "VALUES(?, ?, ?, ?, 'planned', ?)", (keep_volume, keep_root, drop_volume, drop_root, now)).lastrowid
        taken = keep_paths | remembered
        items = []
        for r in drop:
            rel = r["rel_path"][len(drop_root) + 1:]
            if r["content_hash"] in keep_hashes:
                action, dest = COVERED, None
            elif rel.lower() in keep_paths:
                action = CONFLICT
                dest = conflict_name(rel, merge_id, lambda p: p.lower() in taken)
            elif rel.lower() in taken:
                action = MOVE
                dest = conflict_name(rel, merge_id, lambda p: p.lower() in taken)
            else:
                action, dest = MOVE, rel
            if dest is not None:
                taken.add(dest.lower())
            items.append((merge_id, r["id"], action, r["rel_path"], dest and _join(keep_root, dest),
                          r["size"], r["mtime_ns"], r["content_hash"]))
        conn.executemany(
            "INSERT INTO merge_items(merge_id, file_id, action, src_path, dest_path, size, mtime_ns, content_hash, "
            "status) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending')", items)
    return detail(cat, merge_id)


def _missing_paths(conn, volume: str, root: str) -> list:
    prefix = root + "/" if root else ""
    sql = "SELECT substr(rel_path, ?) FROM files WHERE volume_id = ? AND state = 'missing'"
    args: list = [len(prefix) + 1, volume]
    if prefix:
        sql += " AND rel_path >= ? AND rel_path < ?"
        args += [prefix, prefix[:-1] + "0"]
    return conn.execute(sql, args).fetchall()


def _drop_rows(conn, volume: str, root: str) -> list:
    prefix = root + "/"
    return conn.execute(
        "SELECT id, rel_path, rel_path_raw, size, mtime_ns, content_hash FROM files WHERE volume_id = ? "
        "AND state != 'missing' AND rel_path >= ? AND rel_path < ?", (volume, prefix, prefix[:-1] + "0")).fetchall()


def detail(cat: Catalog, merge_id: int, sample: int = 20) -> dict:
    conn = cat.conn
    m = conn.execute("SELECT * FROM merges WHERE id = ?", (merge_id,)).fetchone()
    if m is None:
        raise KeyError(merge_id)
    counts = {r[0]: {"files": r[1], "bytes": r[2]} for r in conn.execute(
        "SELECT action, count(*), coalesce(sum(size), 0) FROM merge_items WHERE merge_id = ? GROUP BY action",
        (merge_id,))}
    labels = dict(conn.execute("SELECT id, label FROM volumes WHERE id IN (?, ?)",
                               (m["keep_volume"], m["drop_volume"])).fetchall())
    examples = {action: [{"from": r[0], "to": r[1]} for r in conn.execute(
        "SELECT src_path, dest_path FROM merge_items WHERE merge_id = ? AND action = ? ORDER BY src_path LIMIT ?",
        (merge_id, action, sample))] for action in (MOVE, CONFLICT)}
    return {
        "id": m["id"], "status": m["status"], "error": m["error"], "created_at": m["created_at"],
        "finished_at": m["finished_at"], "volume_label": labels.get(m["keep_volume"], "?"),
        "drop_label": labels.get(m["drop_volume"], "?"), "same_drive": m["keep_volume"] == m["drop_volume"],
        "keep_volume": m["keep_volume"], "keep_root": m["keep_root"],
        "drop_volume": m["drop_volume"], "drop_root": m["drop_root"], "quarantine_path": m["quarantine_path"],
        "counts": {a: counts.get(a, {"files": 0, "bytes": 0}) for a in (MOVE, CONFLICT, COVERED)},
        "examples": examples,
    }


def list_merges(cat: Catalog) -> list[dict]:
    return [detail(cat, r[0], sample=0) for r in cat.conn.execute("SELECT id FROM merges ORDER BY id DESC")]


def cancel(cat: Catalog, merge_id: int) -> None:
    """Forget a merge that has not moved anything yet."""
    conn = cat.conn
    m = conn.execute("SELECT status FROM merges WHERE id = ?", (merge_id,)).fetchone()
    moved = conn.execute("SELECT count(*) FROM merge_items WHERE merge_id = ? AND status != 'pending'",
                         (merge_id,)).fetchone()[0]
    if m is None or m[0] not in ("planned", "blocked", "failed") or moved:
        raise MergeRefused("Only a merge that has not moved anything can be cancelled; undo it instead.")
    with transaction(conn):
        conn.execute("DELETE FROM merge_items WHERE merge_id = ?", (merge_id,))
        conn.execute("DELETE FROM merges WHERE id = ?", (merge_id,))


# -- running ------------------------------------------------------------------------------------

def run(cat: Catalog, merge_id: int, online: dict[str, Path], should_stop: Callable[[], bool],
        on_progress: Callable[[Progress], None]) -> Progress:
    conn = cat.conn
    m = conn.execute("SELECT * FROM merges WHERE id = ?", (merge_id,)).fetchone()
    prog = Progress(merge_id)
    if m is None or m["status"] not in ("planned", "blocked", "failed", "running"):
        prog.state, prog.message = "failed", "This merge cannot be run (already merged, undone or purged)."
        return prog
    roots = _roots(m, online)
    if roots is None:
        return _stop(conn, prog, "blocked", "Connect both drives to this computer to merge.", on_progress)
    _set_status(conn, merge_id, "running")
    _reconcile(conn, m, roots)

    prog.state = "checking"
    on_progress(prog)
    problems = _check(conn, m, roots)
    if problems:
        prog.problems = problems[:50]
        return _stop(conn, prog, "blocked", f"{len(problems):,} problems found; nothing more was moved. "
                     "Rescan the drive if the folders changed.", on_progress)

    items = conn.execute("SELECT * FROM merge_items WHERE merge_id = ? AND action != ? AND status = 'pending' "
                         "ORDER BY src_path", (merge_id, COVERED)).fetchall()
    if not roots.same:
        needed = sum(item["size"] for item in items)
        free = shutil.disk_usage(roots.keep).free
        if free < needed + FREE_SPACE_MARGIN:
            return _stop(conn, prog, "blocked", f"Not enough free space on the receiving drive: "
                         f"{needed / 1e9:.1f} GB to copy, {free / 1e9:.1f} GB free.", on_progress)
    prog.state, prog.total = "moving", len(items)
    on_progress(prog)
    for item in items:
        if should_stop():
            return _stop(conn, prog, "blocked", "Paused; run it again to continue.", on_progress)
        problem = _move_in(conn, m, roots, item) if roots.same else _copy_in(conn, m, roots, item)
        if problem:
            prog.problems.append(problem)
            return _stop(conn, prog, "blocked", problem, on_progress)
        prog.done += 1
        if prog.done % 200 == 0:
            on_progress(prog)

    prog.state = "quarantining"
    on_progress(prog)
    q = _quarantine_dir(roots.drop, merge_id, m["drop_root"])
    with transaction(conn):
        conn.execute("UPDATE merges SET quarantine_path = ? WHERE id = ?", (q, merge_id))
    src = to_os_path(roots.drop, m["drop_root"], None)
    dst = to_os_path(roots.drop, q, None)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _write_readme(roots.drop)
        os.rename(src, dst)
    except OSError as e:
        return _stop(conn, prog, "failed", f"Could not move the merged tree to quarantine: {e}", on_progress)
    _mark_dropped(conn, m)
    with transaction(conn):
        conn.execute("UPDATE merges SET status = 'merged', error = NULL, finished_at = ? WHERE id = ?",
                     (time.time(), merge_id))
        bump_files_version(conn)
    prog.state, prog.message = "merged", ""
    on_progress(prog)
    return prog


def _stop(conn, prog: Progress, state: str, message: str, on_progress) -> Progress:
    with transaction(conn):
        conn.execute("UPDATE merges SET status = ?, error = ? WHERE id = ?", (state, message, prog.merge_id))
        bump_files_version(conn)
    prog.state, prog.message = state, message
    on_progress(prog)
    return prog


def _set_status(conn, merge_id: int, status: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE merges SET status = ? WHERE id = ?", (status, merge_id))


def _roots(m, online: dict[str, Path]) -> Roots | None:
    keep, drop = online.get(m["keep_volume"]), online.get(m["drop_volume"])
    return Roots(keep, drop) if keep is not None and drop is not None else None


def _check(conn, m, roots: Roots) -> list[str]:
    """What makes merging unsafe right now: the drop tree changed, or a kept copy is gone."""
    problems = []
    drop_root = to_os_path(roots.drop, m["drop_root"], None)
    keep_root = to_os_path(roots.keep, m["keep_root"], None) if m["keep_root"] else str(roots.keep)
    if not os.path.isdir(drop_root):
        return [f"{m['drop_root']} is not on the drive any more."]
    if not os.path.isdir(keep_root):
        return [f"{m['keep_root'] or 'The top of the drive'} is not on the drive any more."]
    expected = {r["src_path"]: r for r in conn.execute(
        "SELECT src_path, size, mtime_ns, status FROM merge_items WHERE merge_id = ?", (m["id"],))}
    moved = {p for p, r in expected.items() if r["status"] == "moved"}
    seen = set()
    for dirpath, dirnames, filenames in os.walk(drop_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, roots.drop).replace(os.sep, "/")
        for name in filenames:
            if name == MARKER_NAME:
                continue
            rel = f"{rel_dir}/{name}"
            seen.add(rel)
            item = expected.get(rel)
            if item is None:
                problems.append(f"{rel}: not in the catalog (new since the last scan)")
                continue
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError as e:
                problems.append(f"{rel}: {e}")
                continue
            if st.st_size != item["size"] or not mtime_equivalent(st.st_mtime_ns, item["mtime_ns"]):
                problems.append(f"{rel}: changed since the last scan")
    for rel in expected.keys() - seen - moved:
        problems.append(f"{rel}: missing from the drive")
    # Every covered content must still be in the kept tree, unchanged.
    for h, in conn.execute("SELECT DISTINCT content_hash FROM merge_items WHERE merge_id = ? AND action = ?",
                           (m["id"], COVERED)):
        if not _kept_copy(conn, m, roots.keep, h):
            src = conn.execute("SELECT src_path FROM merge_items WHERE merge_id = ? AND content_hash = ? LIMIT 1",
                               (m["id"], h)).fetchone()[0]
            problems.append(f"{src}: its copy in the kept tree is gone or changed")
    return problems


def _kept_copy(conn, m, root: Path, h: bytes) -> bool:
    prefix = m["keep_root"] + "/" if m["keep_root"] else ""
    for r in conn.execute("SELECT rel_path, rel_path_raw, size, mtime_ns FROM files WHERE content_hash = ? "
                          "AND volume_id = ? AND state != 'missing'", (h, m["keep_volume"])):
        if prefix and not r["rel_path"].startswith(prefix):
            continue
        try:
            st = os.stat(to_os_path(root, r["rel_path"], r["rel_path_raw"]))
        except OSError:
            continue
        if st.st_size == r["size"] and mtime_equivalent(st.st_mtime_ns, r["mtime_ns"]):
            return True
    return False


def _move_in(conn, m, roots: Roots, item) -> str | None:
    """Same drive: one rename; the file's catalog record follows it."""
    src = to_os_path(roots.drop, item["src_path"], None)
    dst = to_os_path(roots.keep, item["dest_path"], None)
    if os.path.lexists(dst):  # os.rename would replace it on Linux and macOS
        return f"{item['dest_path']}: a file appeared there since the merge was prepared"
    with transaction(conn):
        conn.execute("UPDATE merge_items SET status = 'moving' WHERE id = ?", (item["id"],))
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
    except OSError as e:
        with transaction(conn):
            conn.execute("UPDATE merge_items SET status = 'pending' WHERE id = ?", (item["id"],))
        return f"{item['src_path']}: could not be moved: {e}"
    _record_move(conn, item, item["dest_path"], "moved")
    return None


def _copy_in(conn, m, roots: Roots, item) -> str | None:
    """Across drives: copy under a temporary name, check it, then put it in place and record it."""
    src = to_os_path(roots.drop, item["src_path"], None)
    dst = to_os_path(roots.keep, item["dest_path"], None)
    if os.path.lexists(dst):
        return f"{item['dest_path']}: a file appeared there since the merge was prepared"
    with transaction(conn):
        conn.execute("UPDATE merge_items SET status = 'moving' WHERE id = ?", (item["id"],))
    tmp = _temp_name(dst, item)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        hasher = blake3.blake3(max_threads=blake3.blake3.AUTO)
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while chunk := fin.read(8 << 20):
                hasher.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        if hasher.digest() != item["content_hash"]:
            os.remove(tmp)
            raise OSError("its content changed since the last scan")
        st = os.stat(src)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        if os.path.lexists(dst):
            os.remove(tmp)
            raise OSError(f"a file appeared at {item['dest_path']}")
        os.replace(tmp, dst)
    except OSError as e:
        with transaction(conn):
            conn.execute("UPDATE merge_items SET status = 'pending' WHERE id = ?", (item["id"],))
        return f"{item['src_path']}: could not be copied: {e}"
    _record_copy(conn, m, item)
    return None


def _temp_name(dst: str, item) -> str:
    return os.path.join(os.path.dirname(dst), f".imagelib-merge-{item['merge_id']}-{item['id']}.tmp")


def _record_copy(conn, m, item) -> None:
    kind = conn.execute("SELECT kind FROM files WHERE id = ?", (item["file_id"],)).fetchone()[0]
    now = time.time()
    with transaction(conn):
        new_id = conn.execute(
            "INSERT INTO files(volume_id, rel_path, path_key, kind, size, mtime_ns, state, content_hash, "
            "first_seen_at, last_seen_scan, analyzed_at) VALUES(?, ?, ?, ?, ?, ?, 'done', ?, ?, 0, ?)",
            (m["keep_volume"], item["dest_path"], path_key(item["dest_path"]), kind, item["size"],
             item["mtime_ns"], item["content_hash"], now, now)).lastrowid
        conn.execute("UPDATE merge_items SET status = 'moved', dest_file_id = ? WHERE id = ?", (new_id, item["id"]))


def _record_move(conn, item, rel_path: str, status: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE files SET rel_path = ?, rel_path_raw = NULL, path_key = ? WHERE id = ?",
                     (rel_path, path_key(rel_path), item["file_id"]))
        conn.execute("UPDATE merge_items SET status = ? WHERE id = ?", (status, item["id"]))


def _reconcile(conn, m, roots: Roots) -> None:
    """Settle moves and copies interrupted by a crash, from where the files actually are."""
    for item in conn.execute("SELECT * FROM merge_items WHERE merge_id = ? AND status = 'moving'",
                             (m["id"],)).fetchall():
        dst = to_os_path(roots.keep, item["dest_path"], None)
        at_dest = os.path.lexists(dst)
        at_src = os.path.lexists(to_os_path(roots.drop, item["src_path"], None))
        if roots.same and at_dest and not at_src:
            _record_move(conn, item, item["dest_path"], "moved")
        elif not roots.same and at_dest and os.path.getsize(dst) == item["size"]:
            _record_copy(conn, m, item)  # copies are renamed into place only once complete and checked
        else:
            tmp = _temp_name(dst, item)
            if os.path.lexists(tmp):
                os.remove(tmp)
            with transaction(conn):
                conn.execute("UPDATE merge_items SET status = 'pending' WHERE id = ?", (item["id"],))


def _quarantine_dir(root: Path, merge_id: int, drop_root: str) -> str:
    base = f"{QUARANTINE_DIR}/merge-{merge_id}/{drop_root}"
    candidate, n = base, 1
    while os.path.lexists(to_os_path(root, candidate, None)):
        n += 1
        candidate = f"{base} ({n})"
    return candidate


def _mark_dropped(conn, m) -> None:
    prefix = m["drop_root"] + "/"
    with transaction(conn):
        conn.execute(
            "UPDATE files SET state = 'missing', error = ? WHERE volume_id = ? AND state != 'missing' "
            "AND rel_path >= ? AND rel_path < ?",
            (f"merged into {m['keep_root'] or 'the top of the drive'} (merge {m['id']}); in quarantine",
             m["drop_volume"], prefix, prefix[:-1] + "0"))


# -- undo and purge -----------------------------------------------------------------------------

def undo(cat: Catalog, merge_id: int, online: dict[str, Path]) -> dict:
    """Put the merged-away tree back, and take back what was brought over (moved back, or copies deleted)."""
    conn = cat.conn
    m = conn.execute("SELECT * FROM merges WHERE id = ?", (merge_id,)).fetchone()
    if m is None or m["status"] not in ("merged", "blocked", "failed"):
        raise MergeRefused("Only a merge that moved files can be undone.")
    roots = _roots(m, online)
    if roots is None:
        raise MergeRefused("Connect both drives to this computer to undo the merge.")
    _reconcile(conn, m, roots)
    if m["status"] == "merged":
        src = to_os_path(roots.drop, m["quarantine_path"], None)
        dst = to_os_path(roots.drop, m["drop_root"], None)
        if os.path.lexists(dst):
            raise MergeRefused(f"{m['drop_root']} exists again on the drive; move it aside first.")
        try:
            os.makedirs(os.path.dirname(dst) or str(roots.drop), exist_ok=True)
            os.rename(src, dst)
        except OSError as e:
            raise MergeRefused(f"Could not move the tree back from quarantine: {e}")
        prefix = m["drop_root"] + "/"
        with transaction(conn):
            conn.execute(
                "UPDATE files SET state = 'done', error = NULL WHERE volume_id = ? AND state = 'missing' "
                "AND error LIKE ? AND rel_path >= ? AND rel_path < ?",
                (m["drop_volume"], f"merged into % (merge {merge_id}); in quarantine", prefix, prefix[:-1] + "0"))
    problems = []
    for item in conn.execute("SELECT * FROM merge_items WHERE merge_id = ? AND status = 'moved' "
                             "ORDER BY src_path DESC", (merge_id,)).fetchall():
        problem = _take_back(conn, roots, item)
        if problem:
            problems.append(problem)
        else:
            _prune(to_os_path(roots.keep, item["dest_path"], None), to_os_path(roots.keep, m["keep_root"], None)
                   if m["keep_root"] else str(roots.keep))
    with transaction(conn):
        conn.execute("UPDATE merges SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                     ("restored" if not problems else "failed", "; ".join(problems[:5]) or None, time.time(),
                      merge_id))
        bump_files_version(conn)
    return {"problems": problems}


def _prune(path: str, top: str) -> None:
    """Remove the folders above ``path`` that are left empty, up to (not including) ``top``."""
    folder = os.path.dirname(path)
    while len(folder) > len(top) and folder.startswith(top):
        try:
            os.rmdir(folder)  # only succeeds when empty
        except OSError:
            return
        folder = os.path.dirname(folder)


def _take_back(conn, roots: Roots, item) -> str | None:
    brought = to_os_path(roots.keep, item["dest_path"], None)
    if roots.same:
        original = to_os_path(roots.drop, item["src_path"], None)
        if os.path.lexists(original):
            return f"{item['src_path']}: something is there again; left at {item['dest_path']}"
        try:
            os.makedirs(os.path.dirname(original), exist_ok=True)
            os.rename(brought, original)
        except OSError as e:
            return f"{item['dest_path']}: {e}"
        _record_move(conn, item, item["src_path"], "pending")
        return None
    # Across drives the original never left; the copy goes, unless it was changed since.
    try:
        st = os.stat(brought)
        if st.st_size != item["size"] or not mtime_equivalent(st.st_mtime_ns, item["mtime_ns"]):
            return f"{item['dest_path']}: changed since the merge; left in place"
        os.remove(brought)
    except FileNotFoundError:
        pass
    except OSError as e:
        return f"{item['dest_path']}: {e}"
    with transaction(conn):
        try:
            conn.execute("DELETE FROM files WHERE id = ?", (item["dest_file_id"],))
        except sqlite3.IntegrityError:  # decided on since: keep the record, as gone
            conn.execute("UPDATE files SET state = 'missing', error = ? WHERE id = ?",
                         (f"removed by undoing merge {item['merge_id']}", item["dest_file_id"]))
        conn.execute("UPDATE merge_items SET status = 'pending', dest_file_id = NULL WHERE id = ?", (item["id"],))
    return None


def purge(cat: Catalog, merge_id: int, online: dict[str, Path]) -> dict:
    """Delete the quarantined tree of a finished merge, for good."""
    conn = cat.conn
    m = conn.execute("SELECT * FROM merges WHERE id = ?", (merge_id,)).fetchone()
    if m is None or m["status"] != "merged":
        raise MergeRefused("Only a finished merge has a quarantined tree to delete.")
    root = online.get(m["drop_volume"])
    if root is None:
        raise MergeRefused("The drive holding the quarantined tree is not connected to this computer.")
    path = to_os_path(root, m["quarantine_path"], None)
    freed = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                freed += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    shutil.rmtree(path)
    _remove_empty_dirs(Path(to_os_path(root, f"{QUARANTINE_DIR}/merge-{merge_id}", None)))
    with transaction(conn):
        conn.execute("UPDATE merges SET status = 'purged', finished_at = ? WHERE id = ?", (time.time(), merge_id))
    return {"bytes": freed}
