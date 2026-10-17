"""Scan orchestration: discover, then read + hash + analyze every pending file.

Threads and processes
---------------------
* One **reader thread per volume** walks the volume and then reads pending files
  sequentially in path order. Spinning disks are slow at parallel random reads, so
  each disk gets exactly one reader while different disks run in parallel.
  Each file is read once: the bytes are hashed with BLAKE3 and handed to analysis.
* A **process pool** decodes, verifies, hashes perceptually and renders previews.
  Content already in the catalog (another copy of the same file) is not analyzed again.
* The **main thread** is the only writer of analysis results. It buffers rows and
  commits roughly once a second, so a crash loses at most a second of work, and
  everything resumes from ``files.state = 'pending'``.

Failure handling
----------------
* Drive unplugged: detected via the volume marker; the volume's scan becomes
  ``offline`` and resumes from where it was next time. Other volumes carry on.
* Memory: the bytes waiting between readers and workers are capped, large decodes
  reserve memory from a gate shared by all workers, and no new work is handed out
  while the machine is low on free memory. A huge panorama or TIFF must never be
  able to exhaust RAM, however many of them happen to arrive together.
* A decoder crashing (segfault) on a malformed file takes its whole process pool
  down with it, and the culprit is unknown. The pool is rebuilt and every file that
  was in flight is re-run alone ("isolation"); one that crashes alone is recorded as
  corrupt. A decoder that hangs past ANALYSIS_TIMEOUT is killed and recorded as
  corrupt. Either way the scan keeps going.
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path

import blake3

from . import analyze, discover, formats, sysmem, volumes
from .catalog import Catalog, bump_files_version, to_signed64
from .previews import PreviewStore

GB = 1024 ** 3
INLINE_LIMIT = 256 * 1024 * 1024     # files up to this size are read into memory once
READ_CHUNK = 8 * 1024 * 1024
WORKER_RAM = int(1.5 * GB)           # default worker count allows this much RAM per worker
LOW_MEMORY = 0.10                    # pause handing out work below this fraction free
COMMIT_INTERVAL = 1.0
ANALYSIS_TIMEOUT = 600.0             # seconds before a stuck decoder is killed


@dataclass
class Target:
    """One volume (or sub-folder of it) being scanned."""
    volume_id: str
    label: str
    root: Path
    subpath: str = ""
    scan_id: int = 0
    ref: volumes.VolumeRef | None = None

    def present(self) -> bool:
        """Is this volume (not some other drive) still mounted at ``root``?"""
        return self.ref.present_at(self.root)


@dataclass
class TargetProgress:
    volume_id: str
    label: str
    scan_id: int
    state: str = "starting"   # discovering | analyzing | paused | offline | done | failed
    message: str = ""
    discovered: int = 0
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    read_errors: int = 0
    corrupt: int = 0
    warnings: int = 0
    reused: int = 0           # copies of content already analyzed
    current: str = ""
    rate_bps: float = 0.0
    eta_s: float | None = None
    _samples: deque = field(default_factory=lambda: deque(maxlen=64), repr=False)

    def sample(self, now: float) -> None:
        self._samples.append((now, self.bytes_done))
        while len(self._samples) > 2 and now - self._samples[0][0] > 30:
            self._samples.popleft()
        t0, b0 = self._samples[0]
        if now - t0 > 0.5:
            self.rate_bps = (self.bytes_done - b0) / (now - t0)
        remaining = self.bytes_total - self.bytes_done
        self.eta_s = remaining / self.rate_bps if self.rate_bps > 0 else None


@dataclass
class _Read:
    target: Target
    file_id: int
    rel_path: str
    kind: str
    size: int
    mtime_ns: int
    hash: bytes
    data: bytes | None
    path: str


@dataclass
class _Failed:
    target: Target
    file_id: int
    size: int
    state: str
    error: str


@dataclass
class _ReaderDone:
    target: Target
    state: str
    message: str = ""


@dataclass
class _Inflight:
    job: analyze.Job
    future: Future
    submitted: float
    waiters: list[_Read]
    isolated: bool = False


class WorkersBroken(RuntimeError):
    pass


class ByteBudget:
    """Caps the bytes held in memory between the readers and the analyzers."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.cond = threading.Condition()

    def acquire(self, n: int, should_stop) -> bool:
        with self.cond:
            while self.used > 0 and self.used + n > self.limit:
                if should_stop():
                    return False
                self.cond.wait(0.2)
            self.used += n
            return True

    def release(self, n: int) -> None:
        with self.cond:
            self.used -= n
            self.cond.notify_all()


class Scanner:
    def __init__(self, catalog: Catalog, previews: PreviewStore, workers: int | None = None,
                 make_medium_previews: bool = True, progress_cb=None, crash_after_files: int | None = None,
                 analysis_timeout: float = ANALYSIS_TIMEOUT):
        self.catalog = catalog
        self.previews = previews
        total = sysmem.total_bytes() or 8 * GB
        self.total_memory = total
        self.workers = workers or max(1, min((os.cpu_count() or 2) - 1, total // WORKER_RAM))
        self.make_medium = make_medium_previews
        self.progress_cb = progress_cb
        self.analysis_timeout = analysis_timeout
        self.stop_event = threading.Event()
        # File bytes queued for analysis: ~5% of RAM, between 512 MB and 1.5 GB.
        self.budget = ByteBudget(min(int(1.5 * GB), max(GB // 2, total // 20)))
        self.queue: queue.Queue = queue.Queue(maxsize=self.workers * 4)
        self.progress: dict[int, TargetProgress] = {}
        self._crash_after = crash_after_files  # test hook: simulate a hard crash
        self._committed_files = 0

    # -- public ------------------------------------------------------------

    def stop(self) -> None:
        """Request a graceful pause; in-flight work is finished and saved."""
        self.stop_event.set()

    def prepare(self, volume_id: str, label: str, root: Path, subpath: str = "") -> Target:
        """Create or resume the scan record for a volume."""
        c = self.catalog
        c.mark_volume_seen(volume_id, str(root))
        existing = c.resumable_scan(volume_id, subpath)
        if existing:
            scan_id = existing["id"]
        else:
            scan_id = c.create_scan(volume_id, subpath)
            # A fresh scan retries files that previously failed to read (loose cables happen),
            # and re-checks content analyzed by an older, less thorough analyzer.
            c.conn.execute("UPDATE files SET state='pending' WHERE volume_id=? AND state='read_error'",
                           (volume_id,))
            c.conn.execute(
                "UPDATE files SET state='pending' WHERE volume_id=? AND state='done' AND content_hash IN "
                f"(SELECT hash FROM contents WHERE {analyze.STALE_CONTENT})", (volume_id,))
        ref = volumes.volume_ref(c, c.get_volume(volume_id))
        return Target(volume_id, label, Path(root), subpath, scan_id, ref)

    def run(self, targets: list[Target]) -> dict[int, TargetProgress]:
        for t in targets:
            self.progress[t.scan_id] = TargetProgress(t.volume_id, t.label, t.scan_id)
        self._pending_writes: list[tuple[str, tuple]] = []
        self._pending_previews: list[tuple[bytes, str, bytes]] = []
        self._inflight: dict[bytes, _Inflight] = {}
        self._isolate: deque[tuple[bytes, _Inflight]] = deque()  # suspects after a pool crash
        self._held: list[_Read] = []  # reads needing analysis, parked while isolating
        self._known: set[bytes] = set()
        self._outstanding = {t.scan_id: 0 for t in targets}
        self._reader_state: dict[int, _ReaderDone] = {}

        readers = [threading.Thread(target=self._reader, args=(t,), name=f"reader-{t.label}", daemon=True)
                   for t in targets]
        self._pool = self._new_pool()
        try:
            for r in readers:
                r.start()
            self._dispatch_loop(targets)
        except WorkersBroken as e:
            self.stop_event.set()
            for t in targets:
                prog = self.progress[t.scan_id]
                if prog.state not in ("done", "paused", "offline", "failed"):
                    prog.state, prog.message = "failed", str(e)
                    self.catalog.set_scan_state(t.scan_id, "failed", str(e))
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._flush()
        return self.progress

    # -- reader threads ----------------------------------------------------

    def _reader(self, t: Target) -> None:
        prog = self.progress[t.scan_id]
        try:
            conn = self.catalog.connect()
            discover.register_functions(conn)
            scan = conn.execute("SELECT discovered FROM scans WHERE id=?", (t.scan_id,)).fetchone()
            if not scan["discovered"]:
                prog.state = "discovering"
                self.catalog.set_scan_state(t.scan_id, "discovering")

                def on_progress(stats: discover.DiscoverStats) -> None:
                    prog.discovered = stats.found

                complete = discover.discover(conn, t.volume_id, t.root, t.subpath, t.scan_id,
                                             self.stop_event.is_set, on_progress, t.present)
                if not complete:
                    self._post(_ReaderDone(t, "paused", "paused during discovery"))
                    return
                conn.execute("UPDATE scans SET discovered=1 WHERE id=?", (t.scan_id,))

            prog.state = "analyzing"
            self.catalog.set_scan_state(t.scan_id, "analyzing")
            prefix = t.subpath + "/" if t.subpath else ""
            row = conn.execute(
                "SELECT count(*), coalesce(sum(size),0) FROM files WHERE volume_id=? AND state='pending' "
                "AND substr(rel_path,1,?)=?", (t.volume_id, len(prefix), prefix)).fetchone()
            prog.files_total, prog.bytes_total = row[0], row[1]

            last = ""
            while not self.stop_event.is_set():
                rows = conn.execute(
                    "SELECT id, rel_path, rel_path_raw, kind, size FROM files WHERE volume_id=? "
                    "AND rel_path > ? AND state='pending' AND substr(rel_path,1,?)=? "
                    "ORDER BY rel_path LIMIT 500",
                    (t.volume_id, last, len(prefix), prefix)).fetchall()
                if not rows:
                    break
                for row in rows:
                    if self.stop_event.is_set():
                        break
                    last = row["rel_path"]
                    self._read_one(t, row)
            conn.close()
            self._post(_ReaderDone(t, "paused" if self.stop_event.is_set() else "done"))
        except discover.VolumeOffline as e:
            self._post(_ReaderDone(t, "offline", f"drive disconnected: {e}"))
        except Exception as e:  # unexpected: report, keep other volumes going
            self._post(_ReaderDone(t, "failed", f"{type(e).__name__}: {e}"))

    def _read_one(self, t: Target, row) -> None:
        prog = self.progress[t.scan_id]
        path = discover.to_os_path(t.root, row["rel_path"], row["rel_path_raw"])
        prog.current = row["rel_path"]
        size_hint = row["size"]
        inline = row["kind"] != formats.VIDEO and size_hint <= INLINE_LIMIT
        reserve = size_hint if inline else 0
        if not self.budget.acquire(reserve, self.stop_event.is_set):
            return
        try:
            st = os.stat(path)
            if inline and st.st_size <= INLINE_LIMIT:
                with open(path, "rb") as f:
                    data = f.read()
                h = blake3.blake3(data, max_threads=blake3.blake3.AUTO).digest()
            else:
                data = None
                hasher = blake3.blake3(max_threads=blake3.blake3.AUTO)
                with open(path, "rb") as f:
                    while chunk := f.read(READ_CHUNK):
                        hasher.update(chunk)
                h = hasher.digest()
        except OSError as e:
            self.budget.release(reserve)
            if not t.present():
                raise discover.VolumeOffline(str(e)) from e
            state = "missing" if isinstance(e, FileNotFoundError) else "read_error"
            self._post(_Failed(t, row["id"], size_hint, state, f"{type(e).__name__}: {e}"))
            return
        if data is not None and len(data) != reserve:  # file changed size since discovery
            self.budget.release(reserve)
            self.budget.acquire(len(data), lambda: False)
        self._post(_Read(t, row["id"], row["rel_path"], row["kind"], st.st_size, st.st_mtime_ns, h, data,
                         path if isinstance(path, str) else os.fsdecode(path)))

    def _post(self, msg) -> None:
        self.queue.put(msg)  # the dispatcher always drains until every reader reports done

    # -- dispatcher (main thread) -----------------------------------------

    def _new_pool(self) -> ProcessPoolExecutor:
        # Parallelism comes from the processes; numpy's BLAS spawning a thread per core in
        # every worker would oversubscribe the CPU several times over. Spawned children
        # inherit the environment, so this takes effect before they import numpy.
        for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ.setdefault(var, "1")
        # "spawn" everywhere: it is the only option on Windows, and fork with threads is unsafe.
        ctx = multiprocessing.get_context("spawn")
        # A fresh gate per pool: a killed worker may have died holding reservations.
        gate = analyze.DecodeGate(ctx, int(self.total_memory * 0.4))
        return ProcessPoolExecutor(max_workers=self.workers, initializer=analyze.init_worker,
                                   initargs=(gate, str(self.catalog.dir / "worker.log")), mp_context=ctx)

    def _dispatch_loop(self, targets: list[Target]) -> None:
        last_commit = last_progress = time.monotonic()
        while True:
            readers_active = len(self._reader_state) < len(targets)
            if (not readers_active and not self._inflight and not self._isolate and not self._held
                    and self.queue.empty()):
                break
            self._advance_isolation()
            # Keep the pool just busy enough; reads back up into the queue, then the readers wait.
            low_memory = self._low_memory()
            for _ in range(256):
                if len(self._inflight) >= self.workers * 2 or (low_memory and self._inflight):
                    time.sleep(0.01)
                    break
                try:
                    msg = self.queue.get(timeout=0.02)
                except queue.Empty:
                    break
                self._handle(msg)
            self._collect()
            now = time.monotonic()
            if now - last_commit >= COMMIT_INTERVAL:
                self._flush()
                last_commit = now
            if now - last_progress >= 0.25:
                self._update_progress()
                last_progress = now
        self._flush()
        self._update_progress()

    def _low_memory(self) -> bool:
        """True while free RAM is scarce; checked a few times a second."""
        now = time.monotonic()
        if now - getattr(self, "_mem_checked", 0.0) > 0.25:
            self._mem_checked = now
            avail = sysmem.available_bytes()
            self._mem_low = avail is not None and avail < self.total_memory * LOW_MEMORY
        return self._mem_low

    def _handle(self, msg) -> None:
        if isinstance(msg, _Read):
            self._outstanding[msg.target.scan_id] += 1
            if msg.hash in self._known or self._content_known(msg.hash):
                self.budget.release(len(msg.data) if msg.data is not None else 0)
                self.progress[msg.target.scan_id].reused += 1
                self._file_done(msg)
            elif msg.hash in self._inflight:
                self.budget.release(len(msg.data) if msg.data is not None else 0)
                self._inflight[msg.hash].waiters.append(msg)
            elif self._isolate:
                self._held.append(msg)
            else:
                job = analyze.Job(kind=msg.kind, ext=formats.extension(msg.rel_path), size=msg.size,
                                  data=msg.data, path=None if msg.data is not None else msg.path,
                                  make_medium=self.make_medium)
                self._submit(msg.hash, _Inflight(job, None, 0.0, [msg]))
        elif isinstance(msg, _Failed):
            prog = self.progress[msg.target.scan_id]
            prog.files_done += 1
            prog.bytes_done += msg.size
            if msg.state == "read_error":
                prog.read_errors += 1
            self._write("UPDATE files SET state=?, error=?, analyzed_at=? WHERE id=?",
                        (msg.state, msg.error, time.time(), msg.file_id))
        elif isinstance(msg, _ReaderDone):
            self._reader_state[msg.target.scan_id] = msg
            self._maybe_finish(msg.target)

    def _content_known(self, h: bytes) -> bool:
        row = self.catalog.conn.execute(
            f"SELECT {analyze.STALE_CONTENT} FROM contents WHERE hash=?", (h,)).fetchone()
        if row and not row[0]:
            self._known.add(h)
            return True
        return False

    def _submit(self, h: bytes, entry: _Inflight) -> None:
        try:
            entry.future = self._pool.submit(analyze.analyze, entry.job)
        except BrokenProcessPool:
            self._rebuild_pool()
            entry.future = self._pool.submit(analyze.analyze, entry.job)
        entry.submitted = time.monotonic()
        self._inflight[h] = entry

    def _collect(self) -> None:
        now = time.monotonic()
        crashed = False
        culprits: list[bytes] = []
        hung: list[bytes] = []
        for h, entry in list(self._inflight.items()):
            fut = entry.future
            if fut.done():
                try:
                    result = fut.result()
                except BrokenProcessPool:
                    if entry.isolated:  # crashed while running alone: it is the culprit...
                        culprits.append(h)
                    crashed = True
                    continue
                except Exception as e:
                    result = self._failure(f"analysis failed: {e}")
                self._content_done(h, entry, result)
            elif now - entry.submitted > self.analysis_timeout:
                hung.append(h)
        if hung:
            self._kill_pool()
            for h in hung:
                self._content_done(h, self._inflight[h], self._failure("decoder hung on this file"))
        if crashed or hung:
            self._rebuild_pool()
            if culprits:
                # ...unless workers cannot run anything at all, which is not the file's fault.
                if not self._pool_works():
                    raise WorkersBroken("analysis worker processes keep dying even on an empty job; "
                                        "no files were marked corrupt")
                for h in culprits:
                    self._content_done(h, self._inflight[h], self._failure("decoder crashed on this file"))
            survivors = list(self._inflight.items())
            self._inflight.clear()
            for h, entry in survivors:
                if hung and not crashed:
                    self._submit(h, entry)  # we killed them; they did nothing wrong
                else:
                    entry.isolated = True
                    self._isolate.append((h, entry))

    def _advance_isolation(self) -> None:
        """While suspects remain, run them one at a time; afterwards release held reads."""
        if self._inflight:
            return
        if self._isolate:
            h, entry = self._isolate.popleft()
            self._submit(h, entry)
        elif self._held:
            held, self._held = self._held, []
            for msg in held:
                self._outstanding[msg.target.scan_id] -= 1  # _handle counts it again
                self._handle(msg)

    def _pool_works(self) -> bool:
        try:
            return self._pool.submit(analyze.ping).result(timeout=120) is True
        except Exception:
            return False

    @staticmethod
    def _failure(problem: str) -> analyze.Result:
        result = analyze.Result()
        result.flag(analyze.CORRUPT, f"{problem} (likely corrupt)")
        return result

    def _kill_pool(self) -> None:
        # ProcessPoolExecutor cannot cancel a running task, so stop its processes directly.
        procs = getattr(self._pool, "_processes", None) or {}
        for proc in list(procs.values()):
            try:
                proc.kill()
            except Exception:
                pass

    def _rebuild_pool(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._pool = self._new_pool()

    def _content_done(self, h: bytes, entry: _Inflight, r: analyze.Result) -> None:
        del self._inflight[h]
        if entry.job.data is not None:
            self.budget.release(len(entry.job.data))
            entry.job.data = None
        for size_name, data in r.previews.items():
            self._pending_previews.append((h, size_name, data))
        self._write(
            "INSERT OR REPLACE INTO contents(hash, size, kind, format, width, height, status, problems, "
            "taken_at, camera_make, camera_model, software, orientation, has_exif, duration_s, phash, dhash, "
            "pixel_hash, has_preview, metadata_json, analyzer_version, analyzed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, entry.job.size, entry.job.kind, r.format, r.width, r.height, r.status,
             "; ".join(r.problems) or None, r.taken_at, r.camera_make, r.camera_model, r.software,
             r.orientation, int(r.has_exif), r.duration_s, to_signed64(r.phash), to_signed64(r.dhash),
             r.pixel_hash, int(bool(r.previews)), r.metadata_json(), analyze.ANALYZER_VERSION, time.time()))
        self._known.add(h)
        for i, msg in enumerate(entry.waiters):
            prog = self.progress[msg.target.scan_id]
            if i == 0:
                if r.status == analyze.CORRUPT:
                    prog.corrupt += 1
                elif r.status == analyze.WARNING:
                    prog.warnings += 1
            else:
                prog.reused += 1
            self._file_done(msg)

    def _file_done(self, msg: _Read) -> None:
        prog = self.progress[msg.target.scan_id]
        prog.files_done += 1
        prog.bytes_done += msg.size
        self._write(
            "UPDATE files SET state='done', content_hash=?, size=?, mtime_ns=?, error=NULL, analyzed_at=? "
            "WHERE id=?", (msg.hash, msg.size, msg.mtime_ns, time.time(), msg.file_id))
        self._outstanding[msg.target.scan_id] -= 1
        self._maybe_finish(msg.target)

    def _maybe_finish(self, t: Target) -> None:
        done = self._reader_state.get(t.scan_id)
        if done is None or self._outstanding[t.scan_id] > 0:
            return
        prog = self.progress[t.scan_id]
        if prog.state in ("done", "paused", "offline", "failed"):
            return
        prog.state, prog.message = done.state, done.message
        self._flush()
        self.catalog.set_scan_state(t.scan_id, done.state, done.message or None)

    # -- writes ------------------------------------------------------------

    def _write(self, sql: str, args: tuple) -> None:
        self._pending_writes.append((sql, args))

    def _flush(self) -> None:
        if not self._pending_writes and not self._pending_previews:
            return
        # Previews first: a preview without a catalog row is harmless, the reverse is not.
        self.previews.put_many(self._pending_previews)
        self._pending_previews = []
        conn = self.catalog.conn
        conn.execute("BEGIN IMMEDIATE")
        bump_files_version(conn, contents=any(sql.startswith("INSERT OR REPLACE INTO contents")
                                              for sql, _ in self._pending_writes))
        for sql, args in self._pending_writes:
            conn.execute(sql, args)
            if self._crash_after is not None and sql.startswith("UPDATE files"):
                self._committed_files += 1
                if self._committed_files >= self._crash_after:  # test hook: die like a power cut
                    conn.execute("COMMIT")
                    os._exit(17)
        conn.execute("COMMIT")
        self._pending_writes = []

    def _update_progress(self) -> None:
        now = time.monotonic()
        for prog in self.progress.values():
            prog.sample(now)
        if self.progress_cb:
            self.progress_cb(list(self.progress.values()))
