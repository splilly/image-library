"""Long-running work owned by the GUI server: scans, and which volumes are online.

Scans run in one background thread. Asking for another volume while a scan is
running pauses it gracefully and restarts with the union of volumes; because scans
resume from the catalog, the restart costs only a moment.
"""

from __future__ import annotations

import threading
import time
from dataclasses import fields
from pathlib import Path

from . import volumes
from .catalog import Catalog
from .previews import PreviewStore
from .scanner import Scanner, TargetProgress

FINISHED = ("done", "failed", "offline")


class OnlineVolumes:
    """Which volumes are reachable, re-checked at most every ``ttl`` seconds.

    Checking can mean probing every mount point (and, for read-only drives, asking
    the OS for serial numbers), far too slow to repeat for every request.
    """

    def __init__(self, catalog_dir: Path, ttl: float = 5.0):
        self.catalog_dir = catalog_dir
        self.ttl = ttl
        self._lock = threading.Lock()
        self._at = 0.0
        self._online: dict[str, Path] = {}

    def get(self, force: bool = False) -> dict[str, Path]:
        with self._lock:
            if force or time.monotonic() - self._at > self.ttl:
                self._online = volumes.locate_online(Catalog.shared(self.catalog_dir))
                self._at = time.monotonic()
            return dict(self._online)

    def invalidate(self) -> None:
        with self._lock:
            self._at = 0.0


def progress_dict(p: TargetProgress) -> dict:
    # Not dataclasses.asdict(): it deep-copies the private sample deque, which the scan
    # thread appends to concurrently ("deque mutated during iteration").
    d = {f.name: getattr(p, f.name) for f in fields(p) if not f.name.startswith("_")}
    d["rate_bps"] = round(p.rate_bps)
    d["eta_s"] = round(p.eta_s) if p.eta_s is not None else None
    return d


class ScanManager:
    def __init__(self, catalog_dir: Path, online: OnlineVolumes, workers: int | None = None):
        self.catalog_dir = catalog_dir
        self.online = online
        self.workers = workers
        self._lock = threading.Lock()
        self._wanted: dict[tuple[str, str], None] = {}   # (volume_id, subpath), insertion ordered
        self._pause_requested = False
        self._thread: threading.Thread | None = None
        self._scanner: Scanner | None = None
        self.last: list[dict] = []        # progress of the most recent session, kept after it ends
        self.notes: list[str] = []        # e.g. "waiting for drive X"
        self.version = 0                  # bumps on every state change, for change detection

    # -- control -----------------------------------------------------------

    def start(self, requests: list[tuple[str, str]]) -> None:
        with self._lock:
            for req in requests:
                self._wanted[req] = None
            self._pause_requested = False
            self.version += 1
            if self._thread is not None:
                if self._scanner:
                    self._scanner.stop()  # the runner loop restarts with the union
                return
            self._thread = threading.Thread(target=self._run, name="scan-manager", daemon=True)
            self._thread.start()

    def pause(self) -> None:
        with self._lock:
            self._pause_requested = True
            self.version += 1
            if self._scanner:
                self._scanner.stop()

    def shutdown(self, timeout: float = 30) -> None:
        self.pause()
        thread = self._thread
        if thread:
            thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None

    def state(self) -> dict:
        scanner = self._scanner
        live = [progress_dict(p) for p in scanner.progress.values()] if scanner else []
        return {"running": self.running, "pausing": self._pause_requested and self.running,
                "targets": live or self.last, "notes": list(self.notes), "version": self.version}

    # -- runner ------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._pause_requested:
                    self._wanted.clear()
                if not self._wanted:
                    # Decided under the lock, so a concurrent start() either sees this
                    # thread still registered (and its request is picked up above) or none.
                    self._thread = None
                    self._scanner = None
                    self.version += 1
                    return
                batch = list(self._wanted)
            try:
                self._run_batch(batch)
            except Exception as e:  # keep the server alive; report and drop the batch
                self.notes = [f"Scan stopped by an unexpected error: {type(e).__name__}: {e}"]
                with self._lock:
                    for req in batch:
                        self._wanted.pop(req, None)
                    self._scanner = None

    def _run_batch(self, batch: list[tuple[str, str]]) -> None:
        cat = Catalog.shared(self.catalog_dir)
        online = self.online.get(force=True)
        scanner = Scanner(cat, PreviewStore.shared(cat.previews_path), workers=self.workers)
        targets, notes = [], []
        for volume_id, subpath in batch:
            row = cat.get_volume(volume_id)
            if row is None or volume_id not in online:
                label = row["label"] if row else volume_id[:8]
                notes.append(f"Drive '{label}' is not connected; its scan resumes when it is.")
                with self._lock:
                    self._wanted.pop((volume_id, subpath), None)
                continue
            targets.append(scanner.prepare(volume_id, row["label"], online[volume_id], subpath))
        self.notes = notes
        if not targets:
            return
        with self._lock:
            self._scanner = scanner
            self.version += 1
            if self._pause_requested:
                scanner.stop()
        results = scanner.run(targets)
        self.last = [progress_dict(p) for p in results.values()]
        with self._lock:
            for t in targets:
                if results[t.scan_id].state in FINISHED:
                    self._wanted.pop((t.volume_id, t.subpath), None)
            self._scanner = None
            self.version += 1
        cat.close()
