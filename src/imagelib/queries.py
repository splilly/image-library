"""Read-side queries behind the library browser.

Listing uses keyset pagination (a cursor holding the last row's sort key), so
scrolling deep into a million-file library costs the same as the first page.
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass

import numpy as np

from .catalog import Catalog, to_unsigned64

PROBLEM_STATUSES = ("corrupt", "warning", "unsupported")

# sort name -> (SQL expressions forming the key, descending?)
_SORTS = {
    "path": (("f.volume_id", "f.rel_path", "f.id"), False),
    "date": (("coalesce(c.taken_at, '')", "f.id"), False),
    "date_desc": (("coalesce(c.taken_at, '')", "f.id"), True),
    "size_desc": (("f.size", "f.id"), True),
}


@dataclass
class FileFilter:
    volume_id: str | None = None
    folder: str = ""              # rel_path prefix without trailing '/'; '' = whole volume
    recursive: bool = True
    status: str | None = None     # ok | warning | corrupt | unsupported | problems | duplicates
                                  # | read_error | pending | missing
    kind: str | None = None
    camera: str | None = None
    year: str | None = None       # "2004", or UNKNOWN_YEAR for pictures without a usable date
    month: str | None = None      # "01".."12"; with a year that month of that year, alone that month of any year
    text: str | None = None       # substring of the path
    unique: bool = False          # one row per unique content

    def where(self) -> tuple[str, list]:
        clauses, args = [], []
        if self.volume_id:
            clauses.append("f.volume_id = ?")
            args.append(self.volume_id)
        if self.folder:
            prefix = self.folder.rstrip("/") + "/"
            # Range on the (volume_id, rel_path) index instead of a LIKE.
            clauses.append("f.rel_path >= ? AND f.rel_path < ?")
            args += [prefix, prefix[:-1] + "0"]  # '0' sorts right after '/'
            if not self.recursive:
                clauses.append("instr(substr(f.rel_path, ?), '/') = 0")
                args.append(len(prefix) + 1)
        elif self.volume_id and not self.recursive:
            clauses.append("instr(f.rel_path, '/') = 0")
        status = self.status
        if status in ("read_error", "pending", "missing"):
            clauses.append("f.state = ?")
            args.append(status)
        else:
            clauses.append("f.state != 'missing'")
            if status == "duplicates":
                clauses.append("EXISTS (SELECT 1 FROM files f4 WHERE f4.content_hash = f.content_hash "
                               "AND f4.id != f.id AND f4.state != 'missing')")
            elif status == "problems":
                clauses.append(f"(c.status IN ({','.join('?' * len(PROBLEM_STATUSES))}) OR f.state = 'read_error')")
                args += PROBLEM_STATUSES
            elif status:
                clauses.append("c.status = ?")
                args.append(status)
        if self.kind:
            clauses.append("f.kind = ?")
            args.append(self.kind)
        if self.camera:
            clauses.append("c.camera_model = ?")
            args.append(self.camera)
        if self.year == UNKNOWN_YEAR:
            # Analysed, but no usable date (not merely not scanned yet).
            clauses.append(f"c.hash IS NOT NULL AND NOT coalesce(c.taken_at GLOB '{DATED}', 0)")
        elif self.year and self.month:
            y, m = int(self.year), int(self.month)
            clauses.append("c.taken_at >= ? AND c.taken_at < ?")
            args += [f"{y:04d}-{m:02d}", f"{y + m // 12:04d}-{m % 12 + 1:02d}"]
        elif self.year:
            clauses.append("c.taken_at >= ? AND c.taken_at < ?")
            args += [self.year, str(int(self.year) + 1)]
        elif self.month:
            clauses.append("c.taken_at GLOB '[12][0-9][0-9][0-9]-*' AND substr(c.taken_at, 6, 2) = ?")
            args.append(f"{int(self.month):02d}")
        if self.text:
            clauses.append("f.rel_path LIKE ? ESCAPE '\\'")
            escaped = self.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{escaped}%")
        if self.unique:
            clauses.append("(f.content_hash IS NULL OR f.id = (SELECT min(f3.id) FROM files f3 "
                           "WHERE f3.content_hash = f.content_hash AND f3.state != 'missing'))")
        return " AND ".join(clauses) or "1", args


UNKNOWN_YEAR = "unknown"
DATED = "[12][0-9][0-9][0-9]*"   # taken_at values that start with a year

_SELECT = """
SELECT f.id, f.volume_id, f.rel_path, f.size, f.kind, f.state, f.error, f.content_hash,
       c.status, c.problems, c.format, c.width, c.height, c.taken_at, c.camera_model, c.has_preview,
       (SELECT count(*) FROM files f2 WHERE f2.content_hash = f.content_hash AND f2.state != 'missing') AS copies,
       {key_cols}
FROM files f LEFT JOIN contents c ON c.hash = f.content_hash
"""


def _encode_cursor(values: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def _decode_cursor(cursor: str) -> list:
    return json.loads(base64.urlsafe_b64decode(cursor.encode()))


def file_row_dict(row) -> dict:
    h = row["content_hash"]
    return {
        "id": row["id"], "volume_id": row["volume_id"], "path": row["rel_path"], "size": row["size"],
        "kind": row["kind"], "state": row["state"], "error": row["error"],
        "hash": h.hex() if h else None, "status": row["status"], "problems": row["problems"],
        "format": row["format"], "width": row["width"], "height": row["height"],
        "taken_at": row["taken_at"], "camera": row["camera_model"],
        "has_preview": bool(row["has_preview"]), "copies": row["copies"],
    }


def list_files(cat: Catalog, flt: FileFilter, sort: str = "path", cursor: str | None = None,
               limit: int = 200) -> dict:
    keys, desc = _SORTS.get(sort, _SORTS["path"])
    where, args = flt.where()
    key_cols = ", ".join(f"{expr} AS k{i}" for i, expr in enumerate(keys))
    sql = _SELECT.format(key_cols=key_cols) + f" WHERE {where}"
    if cursor:
        op = "<" if desc else ">"
        sql += f" AND ({', '.join(keys)}) {op} ({', '.join('?' * len(keys))})"
        args = args + _decode_cursor(cursor)
    direction = "DESC" if desc else "ASC"
    sql += f" ORDER BY {', '.join(f'{k} {direction}' for k in keys)} LIMIT ?"
    rows = cat.conn.execute(sql, args + [limit]).fetchall()
    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = _encode_cursor([last[f"k{i}"] for i in range(len(keys))])
    return {"items": [file_row_dict(r) for r in rows], "next": next_cursor}


def count_files(cat: Catalog, flt: FileFilter) -> dict:
    where, args = flt.where()
    row = cat.conn.execute(
        f"SELECT count(*), coalesce(sum(f.size), 0) FROM files f LEFT JOIN contents c ON c.hash = f.content_hash "
        f"WHERE {where}", args).fetchone()
    return {"files": row[0], "bytes": row[1]}


def folders(cat: Catalog, volume_id: str, folder: str = "") -> list[dict]:
    """Immediate sub-folders of ``folder`` with recursive file counts."""
    prefix = folder.rstrip("/") + "/" if folder else ""
    start = len(prefix) + 1
    args: list = [start, start, volume_id]
    sql = ("SELECT substr(rel_path, ?, instr(substr(rel_path, ?), '/') - 1) AS name, count(*) AS files, "
           "sum(size) AS bytes FROM files WHERE volume_id = ? AND state != 'missing'")
    if prefix:
        sql += " AND rel_path >= ? AND rel_path < ?"
        args += [prefix, prefix[:-1] + "0"]
    sql += " AND instr(substr(rel_path, ?), '/') > 0 GROUP BY name ORDER BY name COLLATE NOCASE"
    args.append(start)
    return [{"name": r["name"], "path": prefix + r["name"], "files": r["files"], "bytes": r["bytes"]}
            for r in cat.conn.execute(sql, args)]


def facets(cat: Catalog, year: str | None = None) -> dict:
    c = cat.conn
    month_sql = ("SELECT substr(taken_at, 6, 2) AS m, count(*) FROM contents "
                 "WHERE taken_at GLOB '[12][0-9][0-9][0-9]-[01][0-9]*'")
    month_args: list = []
    if year == UNKNOWN_YEAR:
        month_sql += " AND 0"  # no dates, no months
    elif year:  # months of the selected year only
        month_sql += " AND taken_at >= ? AND taken_at < ?"
        month_args = [year, str(int(year) + 1)]
    undated = c.execute(f"SELECT count(*) FROM contents WHERE NOT coalesce(taken_at GLOB '{DATED}', 0)").fetchone()[0]
    return {
        "months": [{"name": r[0], "count": r[1]} for r in c.execute(month_sql + " GROUP BY m ORDER BY m", month_args)],
        "cameras": [{"name": r[0], "count": r[1]} for r in c.execute(
            "SELECT camera_model, count(*) FROM contents WHERE camera_model IS NOT NULL "
            "GROUP BY camera_model ORDER BY count(*) DESC")],
        "years": [{"name": r[0], "count": r[1]} for r in c.execute(
            f"SELECT substr(taken_at, 1, 4) AS y, count(*) FROM contents WHERE taken_at GLOB '{DATED}' "
            "GROUP BY y ORDER BY y")] + ([{"name": UNKNOWN_YEAR, "count": undated}] if undated else []),
        "kinds": [{"name": r[0], "count": r[1]} for r in c.execute(
            "SELECT kind, count(*) FROM files WHERE state != 'missing' GROUP BY kind ORDER BY count(*) DESC")],
    }


def content_detail(cat: Catalog, content_hash: bytes) -> dict | None:
    row = cat.conn.execute("SELECT * FROM contents WHERE hash = ?", (content_hash,)).fetchone()
    if row is None:
        return None
    locations = [
        {"file_id": r["id"], "volume_id": r["volume_id"], "volume_label": r["label"], "path": r["rel_path"],
         "state": r["state"], "read_only": bool(r["read_only"])}
        for r in cat.conn.execute(
            "SELECT f.id, f.volume_id, v.label, v.read_only, f.rel_path, f.state FROM files f "
            "JOIN volumes v ON v.id = f.volume_id WHERE f.content_hash = ? ORDER BY v.label, f.rel_path",
            (content_hash,))]
    return {
        "hash": content_hash.hex(), "size": row["size"], "kind": row["kind"], "format": row["format"],
        "width": row["width"], "height": row["height"], "status": row["status"],
        "problems": row["problems"].split("; ") if row["problems"] else [],
        "taken_at": row["taken_at"], "camera_make": row["camera_make"], "camera_model": row["camera_model"],
        "software": row["software"], "duration_s": row["duration_s"], "has_preview": bool(row["has_preview"]),
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        "phash": to_unsigned64(row["phash"]), "locations": locations,
    }


class PhashIndex:
    """In-memory copy of every pHash, refreshed when the catalog gains contents."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stamp = None
        self._hashes: list[bytes] = []
        self._values = np.zeros(0, dtype=np.uint64)

    def _refresh(self, cat: Catalog) -> None:
        stamp = tuple(cat.conn.execute("SELECT count(*), max(analyzed_at) FROM contents").fetchone())
        if stamp == self._stamp:
            return
        hashes, values = [], []
        for h, p in cat.conn.execute("SELECT hash, phash FROM contents WHERE phash IS NOT NULL"):
            hashes.append(h)
            values.append(to_unsigned64(p))
        self._hashes, self._values = hashes, np.array(values, dtype=np.uint64)
        self._stamp = stamp

    def similar(self, cat: Catalog, phash_value: int, max_distance: int = 8, limit: int = 50,
                exclude: bytes | None = None) -> list[tuple[bytes, int]]:
        with self._lock:
            self._refresh(cat)
            hashes, values = self._hashes, self._values
        if not len(values):
            return []
        dist = np.bitwise_count(values ^ np.uint64(phash_value))
        idx = np.nonzero(dist <= max_distance)[0]
        idx = idx[np.argsort(dist[idx], kind="stable")]
        out = [(hashes[i], int(dist[i])) for i in idx if hashes[i] != exclude]
        return out[:limit]


def contents_brief(cat: Catalog, hashes: list[bytes]) -> dict[bytes, dict]:
    """Small per-content summaries (for look-alike strips and search results)."""
    if not hashes:
        return {}
    out = {}
    for i in range(0, len(hashes), 500):
        chunk = hashes[i:i + 500]
        for r in cat.conn.execute(
                f"SELECT hash, width, height, status, taken_at, format, has_preview, size FROM contents "
                f"WHERE hash IN ({','.join('?' * len(chunk))})", chunk):
            out[r["hash"]] = {"hash": r["hash"].hex(), "width": r["width"], "height": r["height"],
                              "status": r["status"], "taken_at": r["taken_at"], "format": r["format"],
                              "has_preview": bool(r["has_preview"]), "size": r["size"]}
    return out
