"""Review: keep/discard decisions for problem files and duplicates.

Decisions only record intent; nothing on disk changes until an action plan is run
(milestone 5). Two rules protect the library:

* A decision is tied to the content it was made on. If the file changes, the old
  decision silently stops applying and the file needs review again.
* Discarding the last copy of an intact image is refused, unless another version of
  the same picture survives: its original (for a thumbnail or web export), or a
  twin with the same pixels or a near-identical re-encode. An original is never
  discardable because a thumbnail of it exists. Content judged corrupt (or files
  that cannot be read) may lose every copy: reviewing those is the point.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass

from .catalog import Catalog
from .queries import PhashIndex, contents_brief

KEEP = "keep"
DISCARD = "discard"
REASONS = ("problem", "duplicate", "folder", "thumbnail", "similar", "manual")

# A decision applies only while the file still holds the content it was made on.
VALID_DECISION = "d.file_id = f.id AND d.content_hash IS f.content_hash"


def survives(h: str) -> str:
    """SQL: content ``h`` has a copy that is present and not marked to discard."""
    return (f"EXISTS (SELECT 1 FROM files sf WHERE sf.content_hash = {h} AND sf.state != 'missing' "
            f"AND NOT EXISTS (SELECT 1 FROM decisions sd WHERE sd.file_id = sf.id "
            f"AND sd.content_hash IS sf.content_hash AND sd.decision = 'discard'))")


# Another intact version of the picture in ``x.hash`` survives: a larger original it was
# derived from, or a same-pixels / near twin (directly paired or in the same cluster).
_OTHER_VERSION_SURVIVES = f"""(
    EXISTS (SELECT 1 FROM similar_pairs p JOIN contents o ON o.hash = p.b
            WHERE p.a = x.hash AND p.kind IN ('derived', 'near', 'same_pixels')
            AND o.status = 'ok' AND {survives('o.hash')})
    OR EXISTS (SELECT 1 FROM similar_pairs p JOIN contents o ON o.hash = p.a
               WHERE p.b = x.hash AND p.kind IN ('near', 'same_pixels') AND o.status = 'ok' AND {survives('o.hash')})
    OR EXISTS (SELECT 1 FROM similar_clusters m JOIN similar_clusters o2 ON o2.cluster = m.cluster
               JOIN contents o ON o.hash = o2.hash
               WHERE m.hash = x.hash AND o.hash != x.hash AND o.status = 'ok' AND {survives('o.hash')}))"""


_lock = threading.Lock()


class Refused(Exception):
    """A batch of decisions would break a safety rule; nothing was changed."""

    def __init__(self, message: str, details: list[dict]):
        super().__init__(message)
        self.details = details


# -- decisions ---------------------------------------------------------------------

def decide(cat: Catalog, file_ids: list[int], decision: str | None, reason: str = "manual") -> dict:
    """Set (or clear, with ``decision=None``) the decision for files, atomically."""
    if decision not in (KEEP, DISCARD, None):
        raise ValueError(f"unknown decision {decision!r}")
    if reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    file_ids = sorted(set(file_ids))
    if not file_ids:
        return {"changed": 0}
    conn = cat.conn
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = _fetch_files(conn, file_ids)
            if len(rows) != len(file_ids):
                raise ValueError("some files no longer exist in the catalog")
            if decision is None:
                conn.executemany("DELETE FROM decisions WHERE file_id = ?", [(i,) for i in file_ids])
            else:
                now = time.time()
                conn.executemany(
                    "INSERT INTO decisions(file_id, content_hash, decision, reason, decided_at, machine_id) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET content_hash=excluded.content_hash, "
                    "decision=excluded.decision, reason=excluded.reason, decided_at=excluded.decided_at, "
                    "machine_id=excluded.machine_id",
                    [(r["id"], r["content_hash"], decision, reason, now, cat.machine_id) for r in rows])
            if decision == DISCARD:
                _check_last_copies(conn, {r["content_hash"] for r in rows if r["content_hash"] is not None})
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {"changed": len(file_ids)}


def _fetch_files(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    out = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out += conn.execute(f"SELECT id, content_hash FROM files WHERE id IN ({','.join('?' * len(chunk))})",
                            chunk).fetchall()
    return out


def _check_last_copies(conn: sqlite3.Connection, hashes: set[bytes]) -> None:
    """Refuse if any intact content would be left without a copy that is not discarded."""
    offenders = []
    hashes_list = list(hashes)
    for i in range(0, len(hashes_list), 400):
        chunk = hashes_list[i:i + 400]
        offenders += conn.execute(
            f"""SELECT x.hash, x.status, count(f.id) AS copies FROM contents x
                JOIN files f ON f.content_hash = x.hash AND f.state != 'missing'
                WHERE x.hash IN ({','.join('?' * len(chunk))}) AND x.status != 'corrupt'
                AND NOT {_OTHER_VERSION_SURVIVES}
                GROUP BY x.hash
                HAVING sum(CASE WHEN EXISTS (SELECT 1 FROM decisions d WHERE {VALID_DECISION}
                                             AND d.decision = 'discard') THEN 0 ELSE 1 END) = 0""",
            chunk).fetchall()
    if offenders:
        details = []
        for h, status, copies in offenders[:50]:
            paths = [r[0] + ": " + r[1] for r in conn.execute(
                "SELECT v.label, f.rel_path FROM files f JOIN volumes v ON v.id = f.volume_id "
                "WHERE f.content_hash = ? AND f.state != 'missing' LIMIT 5", (h,))]
            details.append({"hash": h.hex(), "status": status, "copies": copies, "paths": paths})
        n = len(offenders)
        raise Refused(
            f"Refused: this would discard every copy of {n} image{'s' if n != 1 else ''} that "
            f"{'are' if n != 1 else 'is'} not corrupt. Keep at least one copy of each.", details)


def summary(cat: Catalog) -> dict:
    rows = cat.conn.execute(
        f"""SELECT d.decision, d.reason, count(*) AS files, coalesce(sum(f.size), 0) AS bytes
            FROM decisions d JOIN files f ON {VALID_DECISION}
            GROUP BY d.decision, d.reason""").fetchall()
    stale = cat.conn.execute(
        "SELECT count(*) FROM decisions d JOIN files f ON f.id = d.file_id "
        "WHERE d.content_hash IS NOT f.content_hash").fetchone()[0]
    unplanned = cat.conn.execute(
        f"""SELECT count(*) FROM decisions d JOIN files f ON {VALID_DECISION}
            WHERE d.decision = 'discard' AND f.state != 'missing' AND NOT EXISTS (
                SELECT 1 FROM plan_items i WHERE i.file_id = f.id
                AND i.status IN ('pending', 'moving', 'quarantined', 'failed', 'blocked'))""").fetchone()[0]
    out = {"keep": {"files": 0, "bytes": 0}, "discard": {"files": 0, "bytes": 0}, "by_reason": [], "stale": stale,
           "unplanned_discard": unplanned}
    for r in rows:
        out[r["decision"]]["files"] += r["files"]
        out[r["decision"]]["bytes"] += r["bytes"]
        out["by_reason"].append(dict(r))
    return out


def list_decisions(cat: Catalog, decision: str | None = None, after_id: int = 0, limit: int = 200) -> dict:
    args: list = [after_id]
    sql = (f"""SELECT f.id, f.volume_id, v.label, f.rel_path, f.size, f.content_hash, d.decision, d.reason,
                      d.decided_at, c.status, c.has_preview
               FROM decisions d JOIN files f ON {VALID_DECISION} JOIN volumes v ON v.id = f.volume_id
               LEFT JOIN contents c ON c.hash = f.content_hash WHERE f.id > ?""")
    if decision:
        sql += " AND d.decision = ?"
        args.append(decision)
    sql += " ORDER BY f.id LIMIT ?"
    rows = cat.conn.execute(sql, args + [limit]).fetchall()
    items = [{"file_id": r["id"], "volume_id": r["volume_id"], "volume_label": r["label"], "path": r["rel_path"],
              "size": r["size"], "hash": r["content_hash"].hex() if r["content_hash"] else None,
              "decision": r["decision"], "reason": r["reason"], "decided_at": r["decided_at"],
              "status": r["status"], "has_preview": bool(r["has_preview"])} for r in rows]
    return {"items": items, "next": rows[-1]["id"] if len(rows) == limit else None}


def file_decisions(cat: Catalog, file_ids: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for i in range(0, len(file_ids), 500):
        chunk = file_ids[i:i + 500]
        for fid, dec in cat.conn.execute(
                f"SELECT f.id, d.decision FROM files f JOIN decisions d ON {VALID_DECISION} "
                f"WHERE f.id IN ({','.join('?' * len(chunk))})", chunk):
            out[fid] = dec
    return out


# -- duplicate caches ------------------------------------------------------------------

def _meta(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(row[0]) if row else 0


REBUILD_INTERVAL = 60.0  # while files keep changing (a scan), rebuild at most this often
_last_rebuild: dict[str, float] = {}


def refresh_duplicates(cat: Catalog, force: bool = False, busy: bool = False) -> bool:
    """Rebuild dup_groups if files changed since the last build (folder trees: trees.py).

    The grouping is computed into temporary tables, which does not lock the catalog;
    only the final swap is a (short) write. While a scan or plan is running (``busy``)
    and keeps changing files, the previous result is served for up to REBUILD_INTERVAL
    seconds instead of rebuilding on every request.
    """
    conn = cat.conn
    key = str(cat.db_path)
    with _lock:
        files_version = _meta(conn, "files_version")
        dup_version = _meta(conn, "dup_version")
        if not force and dup_version == files_version:
            return False
        if (busy and not force and dup_version >= 0
                and time.monotonic() - _last_rebuild.get(key, -1e9) < REBUILD_INTERVAL):
            return False
        conn.execute("DROP TABLE IF EXISTS temp.new_dup_groups")
        conn.execute("CREATE TEMP TABLE new_dup_groups AS SELECT * FROM dup_groups WHERE 0")
        conn.execute("""
                INSERT INTO temp.new_dup_groups(hash, copies, drives, max_per_drive, size)
                SELECT content_hash, sum(n), count(*), max(n), max(size) FROM (
                    SELECT content_hash, volume_id, count(*) AS n, max(size) AS size FROM files
                    WHERE content_hash IS NOT NULL AND state != 'missing' AND kind != 'other'
                    AND content_hash NOT IN (SELECT hash FROM contents WHERE status = 'sidecar')
                    GROUP BY content_hash, volume_id)
                GROUP BY content_hash HAVING sum(n) > 1""")
        conn.execute("BEGIN IMMEDIATE")  # the swap: the only part that locks the catalog
        try:
            conn.execute("DELETE FROM dup_groups")
            conn.execute("INSERT INTO dup_groups SELECT * FROM temp.new_dup_groups")
            conn.execute("UPDATE meta SET value = ? WHERE key = 'dup_version'", (str(files_version),))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("DROP TABLE IF EXISTS temp.new_dup_groups")
        _last_rebuild[key] = time.monotonic()
    return True


# Path fragments that mark a copy as the less likely original.
_SECONDARY = re.compile(
    r"backup|\bbak\b|\bcopy\b|copy of|- copy|\(\d+\)|\bold\b|\bdup|duplicate|\btemp\b|\btmp\b|download|"
    r"recycle|trash|thumbnails?|\bcache\b|export|sync conflict|_imagelib_quarantine", re.IGNORECASE)


@dataclass
class Copy:
    file_id: int
    volume_id: str
    volume_label: str
    path: str
    mtime_ns: int
    read_only: bool


def suggest_keeper(copies: list[Copy]) -> tuple[int, str]:
    """Pick the copy to keep: not in a backup/copy-looking folder, then the oldest, then the shortest path."""
    def score(c: Copy):
        hits = len(_SECONDARY.findall(c.path))
        return (hits, c.mtime_ns, len(c.path), c.path)
    best = min(copies, key=score)
    others_flagged = [c for c in copies if c is not best and _SECONDARY.search(c.path)]
    if others_flagged and not _SECONDARY.search(best.path):
        why = "the other copies are in backup/copy-like folders"
    elif any(c.mtime_ns != best.mtime_ns for c in copies):
        why = "oldest file date (most likely the original)"
    else:
        why = "shortest path"
    return best.file_id, why


def group_copies(cat: Catalog, content_hash: bytes) -> list[Copy]:
    return [Copy(r["id"], r["volume_id"], r["label"], r["rel_path"], r["mtime_ns"], bool(r["read_only"]))
            for r in cat.conn.execute(
                "SELECT f.id, f.volume_id, v.label, v.read_only, f.rel_path, f.mtime_ns FROM files f "
                "JOIN volumes v ON v.id = f.volume_id WHERE f.content_hash = ? AND f.state != 'missing' "
                "ORDER BY v.label, f.rel_path", (content_hash,))]


def duplicate_groups(cat: Catalog, scope: str = "any", volume: str | None = None, undecided: bool = False,
                     offset: int = 0, limit: int = 100, busy: bool = False) -> dict:
    """Groups of identical files, biggest waste first."""
    refresh_duplicates(cat, busy=busy)
    where, args = ["1"], []
    if scope == "same_drive":
        where.append("g.max_per_drive > 1")
    if volume:
        where.append("EXISTS (SELECT 1 FROM files f WHERE f.content_hash = g.hash AND f.volume_id = ? "
                     "AND f.state != 'missing')")
        args.append(volume)
    if undecided:
        where.append(f"NOT EXISTS (SELECT 1 FROM files f JOIN decisions d ON {VALID_DECISION} "
                     "WHERE f.content_hash = g.hash)")
    cond = " AND ".join(where)
    total = cat.conn.execute(f"SELECT count(*), coalesce(sum((copies - 1) * size), 0) FROM dup_groups g "
                             f"WHERE {cond}", args).fetchone()
    rows = cat.conn.execute(
        f"SELECT g.* FROM dup_groups g WHERE {cond} ORDER BY (copies - 1) * size DESC, hash LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    briefs = contents_brief(cat, [r["hash"] for r in rows])
    decided = dict(cat.conn.execute(
        f"SELECT f.content_hash, count(*) FROM files f JOIN decisions d ON {VALID_DECISION} "
        f"WHERE f.content_hash IN ({','.join('?' * len(rows)) or 'NULL'}) GROUP BY f.content_hash",
        [r["hash"] for r in rows]).fetchall()) if rows else {}
    items = [{**briefs.get(r["hash"], {"hash": r["hash"].hex()}), "copies": r["copies"], "drives": r["drives"],
              "same_drive": r["max_per_drive"] > 1, "wasted": (r["copies"] - 1) * r["size"],
              "decided": decided.get(r["hash"], 0)} for r in rows]
    return {"items": items, "total": total[0], "total_wasted": total[1]}


def duplicate_group(cat: Catalog, content_hash: bytes, online: set[str]) -> dict:
    copies = group_copies(cat, content_hash)
    keeper, why = suggest_keeper(copies) if copies else (None, "")
    decisions = file_decisions(cat, [c.file_id for c in copies])
    return {
        **contents_brief(cat, [content_hash]).get(content_hash, {"hash": content_hash.hex()}),
        "suggested_keep": keeper, "suggestion_reason": why,
        "copies": [{"file_id": c.file_id, "volume_id": c.volume_id, "volume_label": c.volume_label,
                    "path": c.path, "online": c.volume_id in online, "read_only": c.read_only,
                    "mtime": c.mtime_ns / 1e9, "decision": decisions.get(c.file_id)} for c in copies],
    }


def apply_suggestions(cat: Catalog, hashes: list[bytes]) -> dict:
    """For each group: keep the suggested copy, discard the rest (skips groups already decided)."""
    keep_ids, discard_ids = [], []
    for h in hashes:
        copies = group_copies(cat, h)
        if len(copies) < 2 or file_decisions(cat, [c.file_id for c in copies]):
            continue
        keeper, _ = suggest_keeper(copies)
        keep_ids.append(keeper)
        discard_ids += [c.file_id for c in copies if c.file_id != keeper]
    decide(cat, keep_ids, KEEP, "duplicate")
    decide(cat, discard_ids, DISCARD, "duplicate")
    return {"groups": len(keep_ids), "discarded": len(discard_ids)}


# -- problem queue -----------------------------------------------------------------------

PROBLEM_STATUSES = ("corrupt", "warning", "unsupported")


def problems(cat: Catalog, status: str | None = None, volume: str | None = None, undecided: bool = False,
             after: int = 0, limit: int = 200) -> dict:
    """Files needing a look: damaged or suspicious content, and files the disk could not read."""
    where = ["f.id > ?", "f.state != 'missing'", "f.kind != 'other'"]
    args: list = [after]
    if status == "read_error":
        where.append("f.state = 'read_error'")
    elif status in PROBLEM_STATUSES:
        where.append("c.status = ?")
        args.append(status)
    else:
        where.append(f"(f.state = 'read_error' OR c.status IN ({','.join('?' * len(PROBLEM_STATUSES))}))")
        args += PROBLEM_STATUSES
    if volume:
        where.append("f.volume_id = ?")
        args.append(volume)
    if undecided:
        where.append(f"NOT EXISTS (SELECT 1 FROM decisions d WHERE {VALID_DECISION})")
    rows = cat.conn.execute(
        f"""SELECT f.id, f.volume_id, v.label, f.rel_path, f.size, f.state, f.error, f.content_hash,
                   c.status, c.problems, c.has_preview, c.width, c.height, c.format,
                   (SELECT d.decision FROM decisions d WHERE {VALID_DECISION}) AS decision
            FROM files f JOIN volumes v ON v.id = f.volume_id LEFT JOIN contents c ON c.hash = f.content_hash
            WHERE {' AND '.join(where)} ORDER BY f.id LIMIT ?""", args + [limit]).fetchall()
    items = [{"file_id": r["id"], "volume_id": r["volume_id"], "volume_label": r["label"], "path": r["rel_path"],
              "size": r["size"], "hash": r["content_hash"].hex() if r["content_hash"] else None,
              "status": "read_error" if r["state"] == "read_error" else r["status"],
              "problems": r["problems"] or r["error"], "has_preview": bool(r["has_preview"]),
              "width": r["width"], "height": r["height"], "format": r["format"], "decision": r["decision"]}
             for r in rows]
    return {"items": items, "next": rows[-1]["id"] if len(rows) == limit else None}


def intact_versions(cat: Catalog, previews, index: PhashIndex, content_hash: bytes) -> list[dict]:
    """Intact images that are probably the undamaged original of a damaged one.

    Damage usually ruins the later part of an image (JPEG data runs top to bottom), which
    can shift its overall look a long way. So candidates come from the same file name
    or a loose look-alike match, and are then confirmed by comparing only the top of the
    two previews, above the damage.
    """
    from .catalog import NAME_EXPR, to_unsigned64

    c = cat.conn
    row = c.execute("SELECT phash, width, height FROM contents WHERE hash = ?", (content_hash,)).fetchone()
    if row is None:
        return []
    candidates: dict[bytes, set[str]] = {}
    names = {r[0] for r in c.execute(f"SELECT {NAME_EXPR.format(t='f')} FROM files f WHERE f.content_hash = ?",
                                     (content_hash,))}
    for name in names:
        for (h,) in c.execute(
                f"SELECT DISTINCT f.content_hash FROM files f JOIN contents k ON k.hash = f.content_hash "
                f"WHERE {NAME_EXPR.format(t='f')} = ? AND k.status = 'ok' AND f.state != 'missing' "
                f"AND f.content_hash != ? LIMIT 20", (name, content_hash)):
            candidates.setdefault(h, set()).add("same file name")
    distances: dict[bytes, int] = {}
    if row["phash"] is not None:
        for h, dist in index.similar(cat, to_unsigned64(row["phash"]), max_distance=24, limit=30,
                                     exclude=content_hash):
            distances[h] = dist
            candidates.setdefault(h, set())
    briefs = contents_brief(cat, list(candidates))
    damaged_preview = previews.get(content_hash, "s")
    out = []
    for h, why in candidates.items():
        b = briefs.get(h)
        if not b or b["status"] != "ok":
            continue
        if row["width"] and row["height"] and b["width"] and b["height"]:
            if abs(row["width"] / row["height"] - b["width"] / b["height"]) > 0.02:
                continue
        top = _top_difference(damaged_preview, previews.get(h, "s"))
        dist = distances.get(h)
        if top is not None and top <= TOP_MATCH:
            why.add("identical above the damaged area")
        elif dist is not None and dist <= 12:
            why.add("looks the same")
        elif not why:
            continue
        locs = [{"file_id": r[0], "volume_label": r[1], "path": r[2]} for r in c.execute(
            "SELECT f.id, v.label, f.rel_path FROM files f JOIN volumes v ON v.id = f.volume_id "
            "WHERE f.content_hash = ? AND f.state != 'missing' LIMIT 3", (h,))]
        out.append({**b, "distance": dist, "top_difference": top, "why": sorted(why), "locations": locs})
    out.sort(key=lambda v: (v["top_difference"] if v["top_difference"] is not None else 999,
                            v["distance"] if v["distance"] is not None else 99))
    return out[:6]


TOP_MATCH = 8.0  # mean absolute difference (0-255) over the top of two previews


def _top_difference(a: bytes | None, b: bytes | None, fraction: float = 0.4) -> float | None:
    """Mean pixel difference over the top part of two previews, or None if not comparable."""
    if not a or not b:
        return None
    import io

    import numpy as np
    from PIL import Image

    def load(data: bytes) -> np.ndarray:
        with Image.open(io.BytesIO(data)) as im:
            small = im.convert("L").resize((64, 64), Image.Resampling.BILINEAR)
        return np.asarray(small, dtype=np.float32)

    try:
        pa, pb = load(a), load(b)
    except Exception:
        return None
    rows = max(1, int(64 * fraction))
    return float(np.abs(pa[:rows] - pb[:rows]).mean())


# -- thumbnails & reduced copies ----------------------------------------------------------

THUMB_CLASSES = ("original_ok", "original_damaged", "original_uncertain", "orphan")

_THUMB_ROWS = f"""
SELECT c.hash, c.width, c.height, c.size, c.has_preview, c.taken_at, t.reason AS thumb_reason,
       (SELECT p.b FROM similar_pairs p JOIN contents o ON o.hash = p.b
        WHERE p.a = c.hash AND p.kind = 'derived' AND o.status = 'ok' AND {survives('o.hash')}
        ORDER BY o.width * o.height DESC LIMIT 1) AS original,
       EXISTS (SELECT 1 FROM similar_pairs p WHERE p.a = c.hash AND p.kind = 'derived') AS has_derived,
       EXISTS (SELECT 1 FROM similar_pairs p WHERE p.a = c.hash AND p.kind = 'derived_uncertain') AS has_uncertain,
       EXISTS (SELECT 1 FROM files f JOIN decisions d ON {VALID_DECISION} WHERE f.content_hash = c.hash) AS decided
FROM contents c LEFT JOIN thumb_candidates t ON t.hash = c.hash
WHERE (t.hash IS NOT NULL OR EXISTS (SELECT 1 FROM similar_pairs p WHERE p.a = c.hash
                                     AND p.kind IN ('derived', 'derived_uncertain')))
  AND EXISTS (SELECT 1 FROM files f WHERE f.content_hash = c.hash AND f.state != 'missing')
"""

_THUMB_CLASSIFIED = f"""
SELECT *, CASE WHEN original IS NOT NULL THEN 'original_ok'
               WHEN has_derived THEN 'original_damaged'
               WHEN has_uncertain THEN 'original_uncertain' ELSE 'orphan' END AS class,
          CASE WHEN thumb_reason IS NOT NULL THEN 'thumbnail' ELSE 'reduced' END AS kind
FROM ({_THUMB_ROWS})
"""


def _thumb_filter(cls: str | None, kind: str | None, undecided: bool) -> tuple[str, list]:
    where, args = ["1"], []
    if cls:
        where.append("class = ?")
        args.append(cls)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if undecided:
        where.append("NOT decided")
    return " AND ".join(where), args


def thumbnails(cat: Catalog, cls: str | None = None, kind: str | None = None, undecided: bool = False,
               offset: int = 0, limit: int = 100) -> dict:
    """Thumbnail-like and reduced-size images, classified by whether their original survives."""
    where, args = _thumb_filter(cls, kind, undecided)
    rows = cat.conn.execute(f"SELECT * FROM ({_THUMB_CLASSIFIED}) WHERE {where} "
                            f"ORDER BY class, kind DESC, hash LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    counts = {f"{r[0]}/{r[1]}": r[2] for r in cat.conn.execute(
        f"SELECT class, kind, count(*) FROM ({_THUMB_CLASSIFIED}) WHERE {'NOT decided' if undecided else '1'} "
        f"GROUP BY class, kind")}
    originals = contents_brief(cat, [r["original"] for r in rows if r["original"]])
    items = []
    for r in rows:
        items.append({
            "hash": r["hash"].hex(), "width": r["width"], "height": r["height"], "size": r["size"],
            "has_preview": bool(r["has_preview"]), "class": r["class"], "kind": r["kind"],
            "reason": r["thumb_reason"], "decided": bool(r["decided"]),
            "original": originals.get(r["original"]) if r["original"] else None,
        })
    return {"items": items, "counts": counts}


def _locations(cat: Catalog, content_hash: bytes, online: set[str]) -> list[dict]:
    rows = cat.conn.execute(
        "SELECT f.id, f.volume_id, v.label, f.rel_path FROM files f JOIN volumes v ON v.id = f.volume_id "
        "WHERE f.content_hash = ? AND f.state != 'missing' ORDER BY v.label, f.rel_path", (content_hash,)).fetchall()
    decisions = file_decisions(cat, [r[0] for r in rows])
    return [{"file_id": r[0], "volume_id": r[1], "volume_label": r[2], "path": r[3], "online": r[1] in online,
             "decision": decisions.get(r[0])} for r in rows]


def thumbnail_detail(cat: Catalog, content_hash: bytes, online: set[str]) -> dict | None:
    row = cat.conn.execute(f"SELECT * FROM ({_THUMB_CLASSIFIED}) WHERE hash = ?", (content_hash,)).fetchone()
    if row is None:
        return None
    rows = cat.conn.execute(
        "SELECT p.b, p.kind FROM similar_pairs p JOIN contents o ON o.hash = p.b "
        "WHERE p.a = ? AND p.kind IN ('derived', 'derived_uncertain') "
        "ORDER BY p.kind = 'derived' DESC, o.status = 'ok' DESC, o.width * o.height DESC LIMIT 5",
        (content_hash,)).fetchall()
    originals = [r[0] for r in rows]
    certain = {r[0]: r[1] == "derived" for r in rows}
    briefs = contents_brief(cat, originals)
    return {
        "hash": content_hash.hex(), "width": row["width"], "height": row["height"], "size": row["size"],
        "has_preview": bool(row["has_preview"]), "class": row["class"], "kind": row["kind"],
        "reason": row["thumb_reason"], "locations": _locations(cat, content_hash, online),
        "originals": [{**briefs[h], "certain": certain[h], "locations": _locations(cat, h, online)}
                      for h in originals if h in briefs],
    }


def discard_verified_thumbnails(cat: Catalog, kind: str | None = "thumbnail") -> dict:
    """Discard every undecided thumbnail (or reduced copy) whose original is intact and kept."""
    where, args = _thumb_filter("original_ok", kind, True)
    hashes = [r[0] for r in cat.conn.execute(f"SELECT hash FROM ({_THUMB_CLASSIFIED}) WHERE {where}", args)]
    ids = []
    for i in range(0, len(hashes), 500):
        chunk = hashes[i:i + 500]
        ids += [r[0] for r in cat.conn.execute(
            f"SELECT id FROM files WHERE state != 'missing' AND content_hash IN ({','.join('?' * len(chunk))})",
            chunk)]
    decide(cat, ids, DISCARD, "thumbnail")
    return {"images": len(hashes), "files": len(ids)}


# -- similar images (same pixels / near duplicates) ---------------------------------------------

def _cluster_members(cat: Catalog, cluster: int) -> list[sqlite3.Row]:
    return cat.conn.execute(
        """SELECT c.hash, c.width, c.height, c.size, c.format, c.status, c.taken_at, c.has_preview, c.has_exif,
                  c.camera_model, c.software, length(coalesce(c.metadata_json, '')) AS meta_len,
                  (SELECT min(f.mtime_ns) FROM files f WHERE f.content_hash = c.hash AND f.state != 'missing') AS mtime,
                  (SELECT count(*) FROM files f WHERE f.content_hash = c.hash AND f.state != 'missing') AS copies
           FROM similar_clusters m JOIN contents c ON c.hash = m.hash WHERE m.cluster = ?""", (cluster,)).fetchall()


def suggest_member(members: list) -> tuple[bytes, str]:
    """Most pixels, then the most metadata, then the biggest file (least compressed), then the oldest."""
    best = max(members, key=lambda m: (m["status"] == "ok", (m["width"] or 0) * (m["height"] or 0),
                                       m["meta_len"], m["size"], -(m["mtime"] or 0)))
    pixels = {(m["width"], m["height"]) for m in members}
    if len(pixels) > 1:
        why = "highest resolution"
    elif len({m["meta_len"] for m in members}) > 1 and best["meta_len"] == max(m["meta_len"] for m in members):
        why = "keeps the most metadata (camera, date, tags)"
    else:
        why = "largest file (least compressed)"
    return best["hash"], why


def similar_clusters(cat: Catalog, undecided: bool = False, offset: int = 0, limit: int = 100) -> dict:
    where = "1"
    if undecided:
        where = (f"NOT EXISTS (SELECT 1 FROM similar_clusters m2 JOIN files f ON f.content_hash = m2.hash "
                 f"JOIN decisions d ON {VALID_DECISION} WHERE m2.cluster = g.cluster)")
    sql = f"""SELECT g.cluster, count(*) AS members, sum(c.size) AS bytes, max(c.size) AS biggest,
                     min(c.hash) AS sample,
                     max(EXISTS (SELECT 1 FROM similar_pairs p WHERE (p.a = c.hash OR p.b = c.hash)
                                 AND p.kind = 'near')) AS has_near
              FROM similar_clusters g JOIN contents c ON c.hash = g.hash
              WHERE {where} GROUP BY g.cluster"""
    total = cat.conn.execute(f"SELECT count(*), coalesce(sum(bytes - biggest), 0) FROM ({sql})").fetchone()
    rows = cat.conn.execute(f"SELECT * FROM ({sql}) ORDER BY bytes - biggest DESC, cluster LIMIT ? OFFSET ?",
                            (limit, offset)).fetchall()
    briefs = contents_brief(cat, [r["sample"] for r in rows])
    items = [{"cluster": r["cluster"], "members": r["members"], "extra_bytes": r["bytes"] - r["biggest"],
              "kind": "near" if r["has_near"] else "same_pixels", "sample": briefs.get(r["sample"])}
             for r in rows]
    return {"items": items, "total": total[0], "total_extra": total[1]}


def similar_cluster(cat: Catalog, cluster: int, online: set[str]) -> dict:
    members = _cluster_members(cat, cluster)
    if not members:
        return {"cluster": cluster, "members": []}
    keep, why = suggest_member(members)
    return {
        "cluster": cluster, "suggested_keep": keep.hex(), "suggestion_reason": why,
        "members": [{"hash": m["hash"].hex(), "width": m["width"], "height": m["height"], "size": m["size"],
                     "format": m["format"], "status": m["status"], "taken_at": m["taken_at"],
                     "has_preview": bool(m["has_preview"]), "has_exif": bool(m["has_exif"]),
                     "camera": m["camera_model"], "software": m["software"], "metadata_size": m["meta_len"],
                     "locations": _locations(cat, m["hash"], online)}
                    for m in sorted(members, key=lambda m: m["hash"] != keep)],
    }


def keep_cluster_member(cat: Catalog, cluster: int, keep_hash: bytes) -> dict:
    """Keep every copy of one member; discard every copy of the others."""
    members = [m["hash"] for m in _cluster_members(cat, cluster)]
    if keep_hash not in members:
        raise ValueError("that image is not in this group")

    def copies(hashes: list[bytes]) -> list[int]:
        return [r[0] for h in hashes for r in cat.conn.execute(
            "SELECT id FROM files WHERE content_hash = ? AND state != 'missing'", (h,))]

    keep_ids = copies([keep_hash])
    discard_ids = copies([h for h in members if h != keep_hash])
    decide(cat, keep_ids, KEEP, "similar")
    decide(cat, discard_ids, DISCARD, "similar")
    return {"kept": len(keep_ids), "discarded": len(discard_ids)}
