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

from . import plans, relate, volumes
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


class RelateManager:
    """Runs the relate job (relate.py) in the background; it is resumable, so stopping is cheap."""

    def __init__(self, catalog_dir: Path):
        self.catalog_dir = catalog_dir
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.progress = relate.RelateProgress()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="relate", daemon=True)
            self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if wait and thread:
            thread.join(60)

    def _run(self) -> None:
        try:
            def on_progress(p: relate.RelateProgress) -> None:
                self.progress = relate.RelateProgress(**vars(p))

            relate.run(Catalog.shared(self.catalog_dir), self._stop.is_set, on_progress)
        except Exception as e:
            self.progress = relate.RelateProgress(state="failed", message=f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._thread = None

    def state(self) -> dict:
        p = self.progress
        current = relate.is_current(Catalog.shared(self.catalog_dir))
        return {**vars(p), "running": self._thread is not None, "current": current}


class Busy(Exception):
    pass


class PlanManager:
    """Runs one action plan at a time in the background (never alongside a scan)."""

    def __init__(self, catalog_dir: Path, online: OnlineVolumes, relate_manager: RelateManager):
        self.catalog_dir = catalog_dir
        self.online = online
        self.relate = relate_manager
        self.scans: ScanManager | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.progress: plans.RunProgress | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, plan_id: int, options: plans.RunOptions) -> None:
        with self._lock:
            if self._thread is not None:
                raise Busy("an action plan is already running")
            if self.scans and self.scans.running:
                raise Busy("a scan is running; pause it or wait for it to finish")
            self._stop.clear()
            self.progress = plans.RunProgress(plan_id, state="starting")
            self._thread = threading.Thread(target=self._run, args=(plan_id, options), name="plan", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, plan_id: int, options: plans.RunOptions) -> None:
        try:
            self.relate.stop(wait=True)
            cat = Catalog.shared(self.catalog_dir)
            online = self.online.get(force=True)
            refs = {r["id"]: volumes.volume_ref(cat, r) for r in cat.list_volumes()}

            def on_progress(p: plans.RunProgress) -> None:
                self.progress = plans.RunProgress(**{**vars(p), "waiting_volumes": list(p.waiting_volumes)})

            plans.run(cat, plan_id, online, refs, options, self._stop.is_set, on_progress)
        except Exception as e:
            self.progress = plans.RunProgress(plan_id, state="failed", message=f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._thread = None
            self.relate.start()

    def state(self) -> dict | None:
        p = self.progress
        return None if p is None else {**vars(p), "running": self.running}


class ScanManager:
    def __init__(self, catalog_dir: Path, online: OnlineVolumes, workers: int | None = None,
                 relate_manager: RelateManager | None = None):
        self.catalog_dir = catalog_dir
        self.relate = relate_manager  # paused while scanning, run when scanning ends
        self.plans: PlanManager | None = None
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
        if self.plans and self.plans.running:
            raise Busy("an action plan is running; wait for it to finish or pause it")
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

    def _before_scanning(self) -> None:
        if self.relate:
            self.relate.stop(wait=True)  # one CPU-heavy job at a time; relate resumes later

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
        state = {"running": self.running, "pausing": self._pause_requested and self.running,
                 "targets": live or self.last, "notes": list(self.notes), "version": self.version}
        if self.relate:
            state["relate"] = self.relate.state()
        if self.plans:
            state["plan"] = self.plans.state()
        return state

    # -- runner ------------------------------------------------------------

    def _run(self) -> None:
        self._before_scanning()
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
                    if self.relate:
                        self.relate.start()
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
