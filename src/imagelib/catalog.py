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

import logging
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 9

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
    4: """
    -- Review decisions. Only valid while the file still has the content it was decided on.
    CREATE TABLE decisions (
        file_id      INTEGER PRIMARY KEY REFERENCES files(id),
        content_hash BLOB,                          -- files.content_hash when decided (NULL: unreadable file)
        decision     TEXT NOT NULL,                 -- keep | discard
        reason       TEXT NOT NULL,                 -- problem | duplicate | folder | manual
        decided_at   REAL NOT NULL,
        machine_id   TEXT NOT NULL
    );
    CREATE INDEX decisions_decision ON decisions(decision);

    -- Derived duplicate data, rebuilt when meta.files_version moves past meta.dup_version.
    CREATE TABLE dup_groups (
        hash         BLOB PRIMARY KEY,
        copies       INTEGER NOT NULL,
        drives       INTEGER NOT NULL,
        max_per_drive INTEGER NOT NULL,             -- >1 means redundant copies on one drive
        size         INTEGER NOT NULL
    );
    CREATE INDEX dup_groups_wasted ON dup_groups((copies - 1) * size);
    CREATE TABLE dup_folder_pairs (
        a_volume TEXT NOT NULL, a_folder TEXT NOT NULL,
        b_volume TEXT NOT NULL, b_folder TEXT NOT NULL,
        shared   INTEGER NOT NULL,
        bytes    INTEGER NOT NULL
    );
    CREATE INDEX dup_folder_pairs_shared ON dup_folder_pairs(shared);
    INSERT OR IGNORE INTO meta(key, value) VALUES ('files_version', '0'), ('dup_version', '-1');

    -- File name (lower-cased, without folders): find the same photo under another path.
    CREATE INDEX files_name ON files(lower(substr(rel_path, length(rtrim(rel_path, replace(rel_path, '/', ''))) + 1)));
    """,
    5: """
    -- Same decoded picture, whatever the metadata (see analyze._pixel_hash).
    ALTER TABLE contents ADD COLUMN pixel_hash BLOB;
    CREATE INDEX contents_pixel_hash ON contents(pixel_hash);

    -- Relations between different contents, found by the relate job (relate.py).
    --   same_pixels: identical picture, different bytes (a < b)
    --   near:        same size, visually the same: re-encoded or lightly edited (a < b)
    --   derived:     a is a smaller version of b (thumbnail, web export)
    --   derived_uncertain: a looks like a smaller version of b but failed the pixel check
    --                (often a burst neighbour of the true original); shown, never relied on
    CREATE TABLE similar_pairs (
        a          BLOB NOT NULL,
        b          BLOB NOT NULL,
        kind       TEXT NOT NULL,
        phash_dist INTEGER NOT NULL,
        scale      REAL NOT NULL,                    -- b's long edge / a's long edge
        PRIMARY KEY (a, b)
    ) WITHOUT ROWID;
    CREATE INDEX similar_pairs_b ON similar_pairs(b);
    -- Clusters of same_pixels / near pairs (connected components), for review.
    CREATE TABLE similar_clusters (
        cluster INTEGER NOT NULL,
        hash    BLOB NOT NULL PRIMARY KEY
    );
    CREATE INDEX similar_clusters_cluster ON similar_clusters(cluster);
    -- Contents that look like generated thumbnails (by size or by path).
    CREATE TABLE thumb_candidates (
        hash   BLOB PRIMARY KEY,
        reason TEXT NOT NULL
    ) WITHOUT ROWID;
    INSERT OR IGNORE INTO meta(key, value) VALUES ('relate_version', '-1'), ('relate_block', '0'),
        ('relate_target', '-1'), ('contents_version', '0');
    """,
    6: """
    -- Action plans: discard decisions turned into moves to quarantine (plans.py).
    CREATE TABLE plans (
        id         INTEGER PRIMARY KEY,
        created_at REAL NOT NULL,
        machine_id TEXT NOT NULL
    );
    CREATE TABLE plan_items (
        id              INTEGER PRIMARY KEY,
        plan_id         INTEGER NOT NULL REFERENCES plans(id),
        file_id         INTEGER NOT NULL REFERENCES files(id),
        volume_id       TEXT NOT NULL,
        rel_path        TEXT NOT NULL,
        rel_path_raw    BLOB,
        content_hash    BLOB,
        size            INTEGER NOT NULL,
        mtime_ns        INTEGER NOT NULL,
        reason          TEXT NOT NULL,
        -- pending | moving | quarantined | failed | blocked | skipped | restored | purged
        status          TEXT NOT NULL,
        quarantine_path TEXT,                       -- relative to the volume root, '/' separated
        error           TEXT,
        updated_at      REAL NOT NULL
    );
    CREATE INDEX plan_items_plan ON plan_items(plan_id, volume_id, status);
    CREATE INDEX plan_items_file ON plan_items(file_id);
    """,
    7: """
    -- Settings of this installation (remote access etc.; see remote.py).
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    -- Signed-in remote devices. Only a hash of the session token is stored.
    CREATE TABLE sessions (
        token_hash   TEXT PRIMARY KEY,
        created_at   REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        expires_at   REAL NOT NULL,
        via          TEXT NOT NULL,                  -- tailscale | cloudflare
        client       TEXT NOT NULL,                  -- who/where, for the device list
        user_agent   TEXT NOT NULL DEFAULT ''
    );
    """,
    8: """
    -- Hub & agents (sync.py). When a file row last really changed, for incremental sync and
    -- for resolving conflicts between machines (most recent change wins). Maintained by
    -- triggers; a writer that sets changed_at itself (applying another machine's row) keeps it.
    ALTER TABLE files ADD COLUMN changed_at REAL NOT NULL DEFAULT 0;
    UPDATE files SET changed_at = coalesce(analyzed_at, first_seen_at);
    CREATE INDEX files_changed ON files(changed_at, id);
    CREATE TRIGGER files_changed_insert AFTER INSERT ON files WHEN NEW.changed_at = 0 BEGIN
        UPDATE files SET changed_at = (julianday('now') - 2440587.5) * 86400.0 WHERE id = NEW.id;
    END;
    CREATE TRIGGER files_changed_update AFTER UPDATE ON files
    WHEN NEW.changed_at IS OLD.changed_at AND (OLD.size IS NOT NEW.size OR OLD.mtime_ns IS NOT NEW.mtime_ns
        OR OLD.state IS NOT NEW.state OR OLD.content_hash IS NOT NEW.content_hash OR OLD.error IS NOT NEW.error)
    BEGIN
        UPDATE files SET changed_at = (julianday('now') - 2440587.5) * 86400.0 WHERE id = NEW.id;
    END;

    -- Hub side: agents allowed to sync, and where their drives were last seen connected.
    CREATE TABLE agents (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        created_at   REAL NOT NULL,
        machine_id   TEXT,
        last_sync_at REAL,
        revoked      INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE volume_presence (
        volume_id  TEXT NOT NULL,
        machine_id TEXT NOT NULL,
        root       TEXT NOT NULL,
        seen_at    REAL NOT NULL,
        PRIMARY KEY (volume_id, machine_id)
    );
    -- Plan items carried out by an agent: which machine, and what it was asked to do.
    ALTER TABLE plan_items ADD COLUMN relay_machine TEXT;
    ALTER TABLE plan_items ADD COLUMN relay_request TEXT;  -- move | restore | purge (waiting for the agent)

    -- Agent side: actions received from the hub, journaled like local plan items.
    CREATE TABLE relayed_actions (
        hub_item_id     INTEGER PRIMARY KEY,
        kind            TEXT NOT NULL,                 -- move | restore | purge
        plan_id         INTEGER NOT NULL,
        volume_id       TEXT NOT NULL,
        rel_path        TEXT NOT NULL,
        rel_path_raw    BLOB,
        content_hash    BLOB,
        size            INTEGER NOT NULL,
        mtime_ns        INTEGER NOT NULL,
        quarantine_path TEXT,
        status          TEXT NOT NULL,                 -- pending | moving | quarantined | restored | purged | skipped | failed
        error           TEXT,
        reported        INTEGER NOT NULL DEFAULT 0
    );
    """,
    9: """
    -- Organize: copying the best unique originals into a new, date-sorted library (organize.py).
    CREATE TABLE exports (
        id         INTEGER PRIMARY KEY,
        target     TEXT NOT NULL,                     -- folder on this computer
        options    TEXT NOT NULL,                     -- JSON
        created_at REAL NOT NULL
    );
    CREATE TABLE export_items (
        id           INTEGER PRIMARY KEY,
        export_id    INTEGER NOT NULL REFERENCES exports(id),
        content_hash BLOB NOT NULL,
        size         INTEGER NOT NULL,
        dest         TEXT NOT NULL,                   -- relative to the target, '/' separated
        status       TEXT NOT NULL,                   -- pending | copying | done | failed
        error        TEXT,
        UNIQUE (export_id, dest)
    );
    CREATE INDEX export_items_status ON export_items(export_id, status);
    """,
}


LOCK_PATIENCE = 15 * 60  # seconds a background writer waits out a busy catalog before giving up
log = logging.getLogger("imagelib")


def _busy(e: sqlite3.OperationalError) -> bool:
    text = str(e).lower()
    return "locked" in text or "busy" in text


def write_transaction(conn: sqlite3.Connection, work, patience: float = LOCK_PATIENCE) -> None:
    """Run ``work()`` in one write transaction, waiting out a busy catalog.

    Each attempt to start already waits up to the connection timeout (60 s); after that
    it backs off and tries again for up to ``patience`` seconds, so a long write
    elsewhere delays a scan instead of stopping it. ``work`` must be safe to repeat.
    """
    started, delay = time.monotonic(), 1.0
    while True:
        if conn.in_transaction:  # left open by an earlier failure: never build on it
            log.warning("discarding an unfinished transaction")
            conn.execute("ROLLBACK")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if not _busy(e) or time.monotonic() - started > patience:
                raise
            log.warning("catalog busy (%s); retrying in %.0f s", e, delay)
            time.sleep(delay)
            delay = min(30.0, delay * 2)
            continue
        try:
            work()
            conn.execute("COMMIT")
            return
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


@contextmanager
def transaction(conn: sqlite3.Connection, kind: str = "IMMEDIATE") -> Iterator[None]:
    """One write transaction that is always closed: committed, or rolled back on any error.

    A transaction left open after an error keeps the catalog locked for everything else.
    """
    if conn.in_transaction:  # left open by an earlier failure: never build on it
        log.warning("discarding an unfinished transaction")
        conn.execute("ROLLBACK")
    conn.execute(f"BEGIN {kind}")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def setup_logging(directory: Path) -> Path:
    """Log scan events and errors to <catalog>/imagelib.log (rotated), as well as the console."""
    from logging.handlers import RotatingFileHandler

    path = Path(directory) / "imagelib.log"
    handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for name in ("imagelib", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(h, RotatingFileHandler) and h.baseFilename == handler.baseFilename
                   for h in logger.handlers):
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return path


def bump_files_version(conn: sqlite3.Connection, contents: bool = False) -> None:
    """Mark derived data stale. Call once per batch that changes files; ``contents`` also
    when analysis results were written (relations depend only on those)."""
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'files_version'"
                 + (" OR key = 'contents_version'" if contents else ""))


# Must match the files_name index expression exactly for SQLite to use the index.
NAME_EXPR = "lower(substr({t}.rel_path, length(rtrim({t}.rel_path, replace({t}.rel_path, '/', ''))) + 1))"


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

    def connect(self, shared: bool = False) -> sqlite3.Connection:
        """A new connection; ``shared`` allows several threads to use it (the caller serialises access)."""
        conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None, check_same_thread=not shared)
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

    def storage(self, count_previews: bool = True) -> dict:
        """Disk space used by the catalog: database, previews, other files, and room left."""
        import shutil

        def size(*names: str) -> int:
            return sum((self.dir / n).stat().st_size for n in names if (self.dir / n).exists())

        database = size("catalog.db", "catalog.db-wal", "catalog.db-shm")
        previews = size("previews.db", "previews.db-wal", "previews.db-shm")
        everything = sum(p.stat().st_size for p in self.dir.iterdir() if p.is_file())
        out = {"dir": str(self.dir), "database": database, "previews": previews,
               "other": everything - database - previews, "total": everything, "preview_counts": None}
        try:
            usage = shutil.disk_usage(self.dir)
            out["disk_free"], out["disk_total"] = usage.free, usage.total
        except OSError:
            out["disk_free"] = out["disk_total"] = None
        if count_previews and not self.previews_path.exists():
            out["preview_counts"] = {}
        elif count_previews:
            conn = sqlite3.connect(self.previews_path, timeout=60)
            try:
                out["preview_counts"] = dict(conn.execute("SELECT size, count(*) FROM previews GROUP BY size"))
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        return out

    def summary(self, include_duplicates: bool = True) -> dict:
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
        ).fetchone() if include_duplicates else (None, None, None)
        return {
            "files": files[0], "bytes": files[1], "pending": files[2] or 0,
            "read_errors": files[3] or 0, "missing": files[4] or 0,
            "unique_contents": contents[0], "corrupt": contents[1] or 0,
            "warnings": contents[2] or 0, "unsupported": contents[3] or 0,
            "duplicate_groups": dup[0], "redundant_copies": dup[1], "redundant_bytes": dup[2],
        }
