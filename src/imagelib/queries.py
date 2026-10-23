"""Read-side queries behind the library browser.

Listing uses keyset pagination (a cursor holding the last row's sort key), so
scrolling deep into a million-file library costs the same as the first page.
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass, replace

import numpy as np

from . import formats
from .catalog import Catalog, to_unsigned64

PROBLEM_STATUSES = ("corrupt", "warning", "unsupported")
SIDECAR = "sidecar"  # macOS metadata files: hidden everywhere unless asked for (status=sidecar)

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
    camera: str | None = None     # a camera model, or UNKNOWN for pictures that do not name one
    year: str | None = None       # "2004", or UNKNOWN for pictures without a usable date
    month: str | None = None      # "01".."12" or UNKNOWN; with a year that month of that year, alone that
                                  # month of any year
    text: str | None = None       # substring of the path
    unique: bool = False          # one row per unique content
    size: str | None = None       # a SIZES key (pixel size of the longer side), or UNKNOWN

    def path_clauses(self) -> tuple[list[str], list]:
        """The conditions on where a file is and what it is called (need only the files table)."""
        clauses: list[str] = []
        args: list = []
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
        if self.text:
            clauses.append("f.rel_path LIKE ? ESCAPE '\\'")
            escaped = self.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{escaped}%")
        return clauses, args

    def where(self) -> tuple[str, list]:
        clauses, args = self.path_clauses()
        clauses.append(f"f.kind != '{formats.OTHER}'")  # the library is pictures and videos
        status = self.status
        if status in ("read_error", "pending", "missing"):
            clauses.append("f.state = ?")
            args.append(status)
        else:
            clauses.append("f.state != 'missing'")
            if status != SIDECAR:
                clauses.append(f"coalesce(c.status, '') != '{SIDECAR}'")
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
        # UNKNOWN for a value means: checked, but the file does not tell (not merely not scanned yet).
        if self.camera == UNKNOWN:  # a camera that calls itself "unknown" is that too
            clauses.append(f"c.hash IS NOT NULL AND coalesce(c.camera_model, '{UNKNOWN}') = '{UNKNOWN}'")
        elif self.camera:
            clauses.append("c.camera_model = ?")
            args.append(self.camera)
        if self.year == UNKNOWN:
            clauses.append(f"c.hash IS NOT NULL AND NOT coalesce(c.taken_at GLOB '{DATED}', 0)")
        elif self.month == UNKNOWN:
            clauses.append(f"c.hash IS NOT NULL AND NOT coalesce(c.taken_at GLOB '{MONTHED}', 0)")
            if self.year:
                clauses.append("c.taken_at >= ? AND c.taken_at < ?")
                args += [self.year, str(int(self.year) + 1)]
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
        if self.size == UNKNOWN:
            clauses.append(f"c.hash IS NOT NULL AND coalesce({LONG_SIDE}, 0) < 1")
        elif self.size in SIZES:
            low, high = SIZES[self.size]
            clauses.append(f"{LONG_SIDE} >= ?" + (f" AND {LONG_SIDE} < ?" if high else ""))
            args += [low, high] if high else [low]
        if self.unique:
            clauses.append("(f.content_hash IS NULL OR f.id = (SELECT min(f3.id) FROM files f3 "
                           "WHERE f3.content_hash = f.content_hash AND f3.state != 'missing'))")
        return " AND ".join(clauses) or "1", args


UNKNOWN = "unknown"
DATED = "[12][0-9][0-9][0-9]*"   # taken_at values that start with a year
MONTHED = "[12][0-9][0-9][0-9]-[01][0-9]*"  # ... and a month
LONG_SIDE = "max(c.width, c.height)"  # NULL when the size is not known (not scanned, unreadable)
# size filter -> pixels on the longer side, [low, high); high None = no upper limit
SIZES: dict[str, tuple[int, int | None]] = {
    "large": (1600, None),
    "normal": (800, None),     # hides icons, sprites and most album art
    "small": (1, 800),
    "tiny": (1, 256),
}

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


def folders(cat: Catalog, volume_id: str, folder: str = "", flt: FileFilter | None = None,
            index: FacetIndex | None = None) -> list[dict]:
    """Immediate sub-folders of ``folder`` with recursive file counts.

    With other filters active (``flt``, counted with ``index``), each folder also gets
    ``matches``: how many of its files those filters show.
    """
    prefix = folder.rstrip("/") + "/" if folder else ""
    start = len(prefix) + 1
    name = "substr(rel_path, ?, instr(substr(rel_path, ?), '/') - 1)"
    where = f"volume_id = ? AND state != 'missing' AND kind != '{formats.OTHER}'" \
        + (" AND rel_path >= ? AND rel_path < ?" if prefix else "") \
        + " AND instr(substr(rel_path, ?), '/') > 0"
    args = [start, start, volume_id] + ([prefix, prefix[:-1] + "0"] if prefix else []) + [start]
    others = replace(flt, volume_id=None, folder="", recursive=True) if flt else FileFilter()
    if others == FileFilter() or index is None:
        sql = f"SELECT {name} AS name, count(*), sum(size) FROM files WHERE {where} GROUP BY name"
        rows = [(r[0], r[1], r[2], None) for r in cat.conn.execute(sql, args)]
    else:
        conn = cat.conn
        old, conn.row_factory = conn.row_factory, None
        try:
            listed = conn.execute(f"SELECT id, {name}, size FROM files WHERE {where}", args).fetchall()
        finally:
            conn.row_factory = old
        ids = np.fromiter((r[0] for r in listed), dtype=np.int64, count=len(listed))
        shown = np.isin(ids, index.matching_ids(cat, others))
        totals: dict[str, list[int]] = {}
        for (_, child, size), hit in zip(listed, shown.tolist()):
            t = totals.setdefault(child, [0, 0, 0])
            t[0] += 1
            t[1] += size
            t[2] += hit
        rows = [(k, v[0], v[1], v[2]) for k, v in totals.items()]
    out = []
    for child, files, size, matches in sorted(rows, key=lambda r: r[0].casefold()):
        f = {"name": child, "path": prefix + child, "files": files, "bytes": size}
        if matches is not None:
            f["matches"] = matches
        out.append(f)
    return out


# facet -> the FileFilter fields it sets (left out when counting that facet's own options)
_FACET_FIELDS = {
    "volumes": ("volume_id", "folder"),
    "kinds": ("kind",),
    "cameras": ("camera",),
    "years": ("year",),
    "months": ("month",),
    "sizes": ("size",),
}
_SIZE_STEPS = (1, 256, 800, 1600)  # lower bounds of the size buckets; every SIZES range is a run of them
_NO_CONTENT, _UNDATED = -2, -1     # year codes for files not analysed yet, and analysed without a date

# One row per file, read in content order (much faster than in file order).
_FACET_ROWS = f"""
SELECT f.id, f.volume_id, f.kind, f.state, c.rowid, c.status, c.camera_model,
       CASE WHEN c.taken_at GLOB '{DATED}' THEN CAST(substr(c.taken_at, 1, 4) AS INTEGER)
            WHEN c.hash IS NOT NULL THEN {_UNDATED} ELSE {_NO_CONTENT} END,
       CASE WHEN c.taken_at GLOB '{MONTHED}' THEN CAST(substr(c.taken_at, 6, 2) AS INTEGER)
            ELSE 0 END,
       {LONG_SIDE}
FROM files f INDEXED BY files_hash LEFT JOIN contents c ON c.hash = f.content_hash
"""


class _Codes:
    """Strings as small integers (None = -1), for counting with numpy."""

    def __init__(self, values):
        index: dict = {}
        codes = [-1 if v is None else index.setdefault(v, len(index)) for v in values]
        self.names = list(index)
        self.index = index
        self.codes = np.array(codes, dtype=np.int32)

    def code(self, name) -> int:
        return self.index.get(name, -2)  # -2 matches nothing


class _FacetData:
    def __init__(self, rows: list):
        cols = list(zip(*rows)) if rows else [()] * 10
        self.ids = np.array(cols[0], dtype=np.int64)
        self.volume = _Codes(cols[1])
        self.kind = _Codes(cols[2])
        self.state = _Codes(cols[3])
        content = np.array([-1 if c is None else c for c in cols[4]], dtype=np.int64)
        self.status = _Codes(cols[5])
        self.camera = _Codes([None if v == UNKNOWN else v for v in cols[6]])
        self.year = np.array(cols[7], dtype=np.int32)
        self.month = np.array(cols[8], dtype=np.int8)
        side = np.array([0 if v is None else v for v in cols[9]], dtype=np.int64)
        self.bucket = np.searchsorted(np.array(_SIZE_STEPS), side, side="right").astype(np.int8) - 1  # -1: unknown
        self.present = self.state.codes != self.state.code("missing")
        self.media = self.kind.codes != self.kind.code(formats.OTHER)
        # Copies per content (present files only), and the first present file of each content.
        has = self.present & (content >= 0)
        self.duplicated = np.zeros(len(self.ids), dtype=bool)
        self.first = ~(content >= 0)  # files without content always show
        if has.any():
            idx = np.nonzero(has)[0]
            order = idx[np.lexsort((self.ids[idx], content[idx]))]
            keys = content[order]
            starts = np.r_[True, keys[1:] != keys[:-1]]
            self.first[order[starts]] = True
            counts = np.diff(np.r_[np.nonzero(starts)[0], len(order)])
            self.duplicated[order] = np.repeat(counts, counts) > 1

    def rows_with_ids(self, ids: list[int]) -> np.ndarray:
        return np.isin(self.ids, np.array(ids, dtype=np.int64))

    def mask(self, flt: FileFilter, paths: np.ndarray | None) -> np.ndarray:
        """Rows ``flt`` shows; must agree with FileFilter.where (the tests compare the two)."""
        m = self.media.copy() if paths is None else paths & self.media
        if flt.volume_id:
            m &= self.volume.codes == self.volume.code(flt.volume_id)
        status = flt.status
        if status in ("read_error", "pending", "missing"):
            m &= self.state.codes == self.state.code(status)
        else:
            m &= self.present
            if status != SIDECAR:
                m &= self.status.codes != self.status.code(SIDECAR)
            if status == "duplicates":
                m &= self.duplicated
            elif status == "problems":
                m &= np.isin(self.status.codes, [self.status.code(s) for s in PROBLEM_STATUSES]) | \
                    (self.state.codes == self.state.code("read_error"))
            elif status:
                m &= self.status.codes == self.status.code(status)
        if flt.kind:
            m &= self.kind.codes == self.kind.code(flt.kind)
        checked = self.year != _NO_CONTENT
        if flt.camera == UNKNOWN:
            m &= checked & (self.camera.codes == -1)
        elif flt.camera:
            m &= self.camera.codes == self.camera.code(flt.camera)
        if flt.year == UNKNOWN:
            m &= self.year == _UNDATED
        elif flt.year:
            m &= self.year == int(flt.year)
        if flt.month == UNKNOWN and flt.year != UNKNOWN:
            m &= checked & (self.month == 0)
        elif flt.month and flt.year != UNKNOWN:
            m &= self.month == int(flt.month)
            if not flt.year:
                m &= self.year >= 0
        if flt.size == UNKNOWN:
            m &= checked & (self.bucket == -1)
        elif flt.size in SIZES:
            low, high = SIZES[flt.size]
            m &= self.bucket >= _SIZE_STEPS.index(low)
            if high:
                m &= self.bucket < _SIZE_STEPS.index(high)
        if flt.unique:
            m &= self.first
        return m


class FacetIndex:
    """Per-file filter values held in memory, so every filter's counts take milliseconds.

    Loading takes seconds for a million files, so after the first load a changed catalog
    (a scan running) is reloaded in the background while the previous values keep serving.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stamp = None
        self._data: _FacetData | None = None
        self._reloading = False

    @staticmethod
    def _stamp_of(cat: Catalog):
        return cat.conn.execute("SELECT value FROM meta WHERE key = 'files_version'").fetchone()[0]

    def _load(self, cat: Catalog) -> None:
        stamp = self._stamp_of(cat)
        conn = cat.conn
        old, conn.row_factory = conn.row_factory, None
        try:
            data = _FacetData(conn.execute(_FACET_ROWS).fetchall())
        finally:
            conn.row_factory = old
        self._data, self._stamp = data, stamp

    def _reload_in_background(self, cat: Catalog) -> None:
        def run():
            try:
                self._load(cat)
            finally:
                self._reloading = False

        self._reloading = True
        threading.Thread(target=run, name="facet-index", daemon=True).start()

    def _current(self, cat: Catalog) -> _FacetData:
        with self._lock:
            if self._data is None:
                self._load(cat)
            elif not self._reloading and self._stamp_of(cat) != self._stamp:
                self._reload_in_background(cat)
            return self._data

    @staticmethod
    def _path_rows(cat: Catalog, d: _FacetData, f: FileFilter) -> np.ndarray | None:
        """Rows the folder and name search allow (looked up in the catalog), None = all."""
        if not (f.folder or f.text or (f.volume_id and not f.recursive)):
            return None  # the drive alone is one of the in-memory values
        clauses, args = f.path_clauses()
        sql = f"SELECT f.id FROM files f WHERE {' AND '.join(clauses)}"
        return d.rows_with_ids([r[0] for r in cat.conn.execute(sql, args)])

    def matching_ids(self, cat: Catalog, flt: FileFilter) -> np.ndarray:
        """Ids of the files ``flt`` shows."""
        d = self._current(cat)
        return d.ids[d.mask(flt, self._path_rows(cat, d, flt))]

    def facets(self, cat: Catalog, flt: FileFilter | None = None) -> dict:
        """How many files each filter option would show, given all the *other* filters in ``flt``."""
        flt = flt or FileFilter()
        d = self._current(cat)

        def path_rows(f: FileFilter) -> np.ndarray | None:
            return self._path_rows(cat, d, f)

        paths = path_rows(flt)
        masks = {}
        for facet, fields in _FACET_FIELDS.items():
            cleared = {name: None for name in fields}
            if "folder" in fields:
                cleared["folder"] = ""
            other = replace(flt, **cleared)
            masks[facet] = d.mask(other, path_rows(other) if "folder" in fields else paths)

        def tally(codes: np.ndarray, m: np.ndarray, offset: int = 0) -> dict[int, int]:
            picked = codes[m].astype(np.int64) + offset
            picked = picked[picked >= 0]
            counts = np.bincount(picked) if len(picked) else np.zeros(0, dtype=np.int64)
            return {i - offset: int(n) for i, n in enumerate(counts) if n}

        def by_name(codes: _Codes, m: np.ndarray) -> dict:
            return {codes.names[i]: n for i, n in tally(codes.codes, m).items()}

        checked = d.year != _NO_CONTENT
        years = tally(d.year, masks["years"] & checked, offset=1)
        months = tally(d.month.astype(np.int32), masks["months"] & checked)
        buckets = tally(d.bucket.astype(np.int32), masks["sizes"] & checked, offset=1)  # -1: unknown
        sizes = {}
        for name, (low, high) in SIZES.items():
            sizes[name] = sum(n for b, n in buckets.items()
                              if b >= 0 and _SIZE_STEPS[b] >= low and (high is None or _SIZE_STEPS[b] < high))
        no_camera = int(np.count_nonzero(masks["cameras"] & checked & (d.camera.codes == -1)))
        desc = lambda kv: (-kv[1], kv[0])  # noqa: E731

        def listed(items, unknown: int = 0) -> list[dict]:
            """Options with their counts, then "unknown" (checked, but the file does not tell) if any."""
            return [{"name": k, "count": v} for k, v in items] + \
                ([{"name": UNKNOWN, "count": unknown}] if unknown else [])

        return {
            "volumes": listed(sorted(by_name(d.volume, masks["volumes"]).items())),
            "kinds": listed(sorted(by_name(d.kind, masks["kinds"]).items(), key=desc)),
            "cameras": listed(sorted(by_name(d.camera, masks["cameras"]).items(), key=desc), no_camera),
            "years": listed(((str(y), n) for y, n in sorted(years.items()) if y >= 0), years.get(_UNDATED, 0)),
            "months": [] if flt.year == UNKNOWN else  # no date, no month: the month filter does not apply
                      listed(((f"{m:02d}", n) for m, n in sorted(months.items()) if m > 0), months.get(0, 0)),
            "sizes": listed(sizes.items(), buckets.get(-1, 0)),
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
