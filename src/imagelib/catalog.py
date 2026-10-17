"""The catalog: a SQLite database describing every volume, file and unique content.

Design notes
------------
* ``files`` is one row per path on a volume. ``contents`` is one row per unique
  byte stream (keyed by BLAKE3 hash), so analysis and previews are done once no
  matter how many copies exist.
* Paths are stored relative to the volume root with ``/`` separators, exactly as
  found on disk. ``path_key`` is a Unicode-normalised, case-folded form used for
  matching across operating systems.
* Volumes and machines are identified by UUIDs so catalogs from several
  machines can later be merged by the hub.
* WAL mode plus small, frequent transactions make the catalog crash safe.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import unicodedata
import uuid
from pathlib import Path

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS machines (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    os         TEXT NOT NULL,
    last_seen  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS volumes (
    id                TEXT PRIMARY KEY,           -- UUID stored in the volume marker file
    label             TEXT NOT NULL,
    notes             TEXT NOT NULL DEFAULT '',
    fs_type           TEXT,
    total_bytes       INTEGER,
    created_at        REAL NOT NULL,
    last_seen_at      REAL,
    last_seen_path    TEXT,                       -- mount point where it was last found
    last_seen_machine TEXT REFERENCES machines(id)
);

CREATE TABLE IF NOT EXISTS files (
    id             INTEGER PRIMARY KEY,
    volume_id      TEXT NOT NULL REFERENCES volumes(id),
    rel_path       TEXT NOT NULL,                 -- '/' separated, as found on disk
    rel_path_raw   BLOB,                          -- original bytes when the name is not valid UTF-8
    path_key       TEXT NOT NULL,                 -- NFC + casefold, for cross-OS matching
    kind           TEXT NOT NULL,                 -- image | raw | psd | video
    size           INTEGER NOT NULL,
    mtime_ns       INTEGER NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending', -- pending | done | read_error | missing
    error          TEXT,
    content_hash   BLOB REFERENCES contents(hash),
    first_seen_at  REAL NOT NULL,
    last_seen_scan INTEGER NOT NULL,
    analyzed_at    REAL,
    UNIQUE (volume_id, rel_path)
);
CREATE INDEX IF NOT EXISTS files_pending ON files(volume_id, state);
CREATE INDEX IF NOT EXISTS files_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS files_path_key ON files(volume_id, path_key);

CREATE TABLE IF NOT EXISTS contents (
    hash             BLOB PRIMARY KEY,            -- BLAKE3, 32 bytes
    size             INTEGER NOT NULL,
    kind             TEXT NOT NULL,
    format           TEXT,                        -- decoder's format name, e.g. JPEG, CR2, HEIF
    width            INTEGER,
    height           INTEGER,
    status           TEXT NOT NULL,               -- ok | warning | corrupt | unsupported
    problems         TEXT,                        -- '; ' separated human readable findings
    taken_at         TEXT,                        -- ISO-ish timestamp from metadata
    camera_make      TEXT,
    camera_model     TEXT,
    software         TEXT,
    orientation      INTEGER,
    has_exif         INTEGER NOT NULL DEFAULT 0,
    duration_s       REAL,
    phash            INTEGER,                     -- 64-bit perceptual hashes (signed storage)
    dhash            INTEGER,
    has_preview      INTEGER NOT NULL DEFAULT 0,
    metadata_json    TEXT,
    analyzer_version INTEGER NOT NULL,
    analyzed_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS contents_status ON contents(status);

CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY,
    volume_id    TEXT NOT NULL REFERENCES volumes(id),
    machine_id   TEXT NOT NULL REFERENCES machines(id),
    root_subpath TEXT NOT NULL DEFAULT '',        -- '' = whole volume
    state        TEXT NOT NULL,                   -- discovering | analyzing | paused | offline | done | failed
    discovered   INTEGER NOT NULL DEFAULT 0,      -- 1 once a full walk has completed
    started_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    finished_at  REAL,
    message      TEXT
);
CREATE INDEX IF NOT EXISTS scans_volume ON scans(volume_id, state);
"""

# Incremental upgrades: version -> SQL taking the previous version to it.
_MIGRATIONS = {
    2: """
    -- Read-only volumes have no marker file; they are recognised by filesystem fingerprints.
    ALTER TABLE volumes ADD COLUMN read_only INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE volume_fingerprints (
        kind      TEXT NOT NULL,                    -- serial32 | fs_uuid | mac_volume_uuid
        value     TEXT NOT NULL,
        volume_id TEXT NOT NULL REFERENCES volumes(id),
        PRIMARY KEY (kind, value, volume_id)
    );
    CREATE INDEX volume_fingerprints_volume ON volume_fingerprints(volume_id);
    """,
    3: """
    -- Library browsing: sort by date, filter by camera.
    CREATE INDEX contents_taken ON contents(taken_at);
    CREATE INDEX contents_camera ON contents(camera_model);
    """,
}


def path_key(rel_path: str) -> str:
    return unicodedata.normalize("NFC", rel_path).casefold()


def to_signed64(v: int | None) -> int | None:
    if v is None:
        return None
    return v - (1 << 64) if v >= (1 << 63) else v


def to_unsigned64(v: int | None) -> int | None:
    if v is None:
        return None
    return v + (1 << 64) if v < 0 else v


class Catalog:
    """Owns the catalog directory: ``catalog.db`` plus ``previews.db``."""

    _shared: dict[Path, "Catalog"] = {}
    _shared_lock = threading.Lock()

    @classmethod
    def shared(cls, directory: Path) -> "Catalog":
        """One instance per directory for long-lived processes (connections stay per thread)."""
        key = Path(directory).resolve()
        with cls._shared_lock:
            if key not in cls._shared:
                cls._shared[key] = cls(key)
            return cls._shared[key]

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "catalog.db"
        self.previews_path = self.dir / "previews.db"
        self._local = threading.local()
        with self.connect() as conn:
            self._migrate(conn)
            self.machine_id = self._ensure_machine(conn)

    # -- connections -------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA cache_size=-65536")  # 64 MB
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """A connection private to the calling thread."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self.connect()
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- schema ------------------------------------------------------------

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Catalog {self.db_path} was created by a newer version of imagelib "
                f"(schema {version}, this build supports {SCHEMA_VERSION})."
            )
        if version == 0:
            conn.executescript(_SCHEMA)
            version = 1
        while version < SCHEMA_VERSION:
            version += 1
            conn.executescript("BEGIN;" + _MIGRATIONS[version] + f"PRAGMA user_version={version}; COMMIT;")

    def _ensure_machine(self, conn: sqlite3.Connection) -> str:
        import platform

        row = conn.execute("SELECT value FROM meta WHERE key='machine_id'").fetchone()
        machine_id = row[0] if row else str(uuid.uuid4())
        if not row:
            conn.execute("INSERT INTO meta(key, value) VALUES('machine_id', ?)", (machine_id,))
        conn.execute(
            "INSERT INTO machines(id, name, os, last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, os=excluded.os, last_seen=excluded.last_seen",
            (machine_id, platform.node() or "unknown", platform.system(), time.time()),
        )
        return machine_id

    # -- volumes -----------------------------------------------------------

    def get_volume(self, volume_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()

    def list_volumes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM volumes ORDER BY label").fetchall()

    def upsert_volume(self, volume_id: str, label: str, fs_type: str | None, total_bytes: int | None,
                      read_only: bool = False) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT INTO volumes(id, label, fs_type, total_bytes, created_at, read_only) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET label=excluded.label, read_only=excluded.read_only, "
            "fs_type=coalesce(excluded.fs_type, fs_type), total_bytes=coalesce(excluded.total_bytes, total_bytes)",
            (volume_id, label, fs_type, total_bytes, now, int(read_only)),
        )

    def add_fingerprints(self, volume_id: str, fingerprints: set[tuple[str, str]]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO volume_fingerprints(kind, value, volume_id) VALUES(?,?,?)",
            [(kind, value, volume_id) for kind, value in fingerprints])

    def fingerprints(self, volume_id: str) -> set[tuple[str, str]]:
        return {(r[0], r[1]) for r in self.conn.execute(
            "SELECT kind, value FROM volume_fingerprints WHERE volume_id=?", (volume_id,))}

    def volumes_matching(self, ident) -> list[sqlite3.Row]:
        """Read-only volumes whose fingerprints and capacity match an ``fsid.FsIdentity``."""
        if not ident.fingerprints:
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT v.* FROM volumes v JOIN volume_fingerprints f ON f.volume_id=v.id "
            f"WHERE v.read_only=1 AND ({' OR '.join('(f.kind=? AND f.value=?)' for _ in ident.fingerprints)})",
            [x for fp in ident.fingerprints for x in fp]).fetchall()
        return [r for r in rows if ident.matches(self.fingerprints(r["id"]), r["total_bytes"])]

    def mark_volume_seen(self, volume_id: str, mount_path: str) -> None:
        self.conn.execute(
            "UPDATE volumes SET last_seen_at=?, last_seen_path=?, last_seen_machine=? WHERE id=?",
            (time.time(), mount_path, self.machine_id, volume_id),
        )

    # -- scans -------------------------------------------------------------

    def resumable_scan(self, volume_id: str, root_subpath: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scans WHERE volume_id=? AND root_subpath=? AND machine_id=? "
            "AND state NOT IN ('done', 'failed') ORDER BY id DESC LIMIT 1",
            (volume_id, root_subpath, self.machine_id),
        ).fetchone()

    def unfinished_scans(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.*, v.label FROM scans s JOIN volumes v ON v.id=s.volume_id "
            "WHERE s.machine_id=? AND s.state NOT IN ('done', 'failed') ORDER BY s.id",
            (self.machine_id,),
        ).fetchall()

    def create_scan(self, volume_id: str, root_subpath: str) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO scans(volume_id, machine_id, root_subpath, state, started_at, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (volume_id, self.machine_id, root_subpath, "discovering", now, now),
        )
        return cur.lastrowid

    def set_scan_state(self, scan_id: int, state: str, message: str | None = None, **fields) -> None:
        now = time.time()
        sets = ["state=?", "updated_at=?", "message=?"]
        args: list = [state, now, message]
        if state in ("done", "failed"):
            sets.append("finished_at=?")
            args.append(now)
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(v)
        args.append(scan_id)
        self.conn.execute(f"UPDATE scans SET {', '.join(sets)} WHERE id=?", args)

    # -- summary -----------------------------------------------------------

    def volume_stats(self, volume_id: str) -> dict:
        row = self.conn.execute(
            "SELECT count(*) AS files, coalesce(sum(size),0) AS bytes, "
            "sum(state='pending') AS pending, sum(state='done') AS done, "
            "sum(state='read_error') AS read_errors, sum(state='missing') AS missing, "
            "coalesce(sum(CASE WHEN state='pending' THEN size END),0) AS pending_bytes "
            "FROM files WHERE volume_id=?",
            (volume_id,),
        ).fetchone()
        return {k: (row[k] or 0) for k in row.keys()}

    def summary(self) -> dict:
        c = self.conn
        files = c.execute(
            "SELECT count(*), coalesce(sum(size),0), sum(state='pending'), sum(state='read_error'), "
            "sum(state='missing') FROM files"
        ).fetchone()
        contents = c.execute(
            "SELECT count(*), sum(status='corrupt'), sum(status='warning'), sum(status='unsupported') FROM contents"
        ).fetchone()
        dup = c.execute(
            "SELECT count(*), coalesce(sum(n-1),0), coalesce(sum((n-1)*size),0) FROM "
            "(SELECT count(*) AS n, max(size) AS size FROM files WHERE content_hash IS NOT NULL "
            "AND state='done' GROUP BY content_hash HAVING n>1)"
        ).fetchone()
        return {
            "files": files[0], "bytes": files[1], "pending": files[2] or 0,
            "read_errors": files[3] or 0, "missing": files[4] or 0,
            "unique_contents": contents[0], "corrupt": contents[1] or 0,
            "warnings": contents[2] or 0, "unsupported": contents[3] or 0,
            "duplicate_groups": dup[0], "redundant_copies": dup[1], "redundant_bytes": dup[2],
        }
