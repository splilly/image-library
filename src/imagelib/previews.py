"""Preview images, kept in their own SQLite file next to the catalog.

One database file avoids creating millions of tiny files (slow on NTFS and with
antivirus scanners), is trivial to copy to the hub, and keeps catalog.db small
enough to back up often.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .catalog import transaction

SMALL = "s"   # grid thumbnail
MEDIUM = "m"  # review preview

_SCHEMA = """
CREATE TABLE IF NOT EXISTS previews (
    hash  BLOB NOT NULL,
    size  TEXT NOT NULL,
    data  BLOB NOT NULL,
    PRIMARY KEY (hash, size)
) WITHOUT ROWID;
"""


class PreviewStore:
    _shared: dict[Path, "PreviewStore"] = {}
    _shared_lock = threading.Lock()

    @classmethod
    def shared(cls, path: Path) -> "PreviewStore":
        key = Path(path).resolve()
        with cls._shared_lock:
            if key not in cls._shared:
                cls._shared[key] = cls(key)
            return cls._shared[key]

    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self.conn.executescript(_SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def put_many(self, rows: list[tuple[bytes, str, bytes]]) -> None:
        if not rows:
            return
        conn = self.conn
        with transaction(conn, kind="DEFERRED"):
            conn.executemany("INSERT OR REPLACE INTO previews(hash, size, data) VALUES(?,?,?)", rows)

    def get(self, content_hash: bytes, size: str = SMALL) -> bytes | None:
        row = self.conn.execute(
            "SELECT data FROM previews WHERE hash=? AND size=?", (content_hash, size)
        ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
