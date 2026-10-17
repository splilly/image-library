"""Scan orchestration: discover, then read + hash + analyze every pending file.

Threads and processes
---------------------
* Each volume is walked, then its pending files are read in path order by **one or
  more reader threads**. How many is found per drive while scanning (ReadController):
  a spinning disk gets slower with parallel reads (seeking) and stays at one; an SSD,
  especially behind USB or an antivirus that inspects each file as it is opened, gets
  faster and is given more. Different drives always run in parallel.
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
MAX_READERS = 8                      # parallel reads per drive, at most
READ_WINDOW = 12.0                   # seconds of reading per measurement when tuning readers
READ_GAIN = 1.15                     # more readers must bring at least this much more throughput
REPROBE_AFTER = 300.0                # re-check the best reader count this often (seconds)


class ReadController:
    """Finds how many parallel reads suit a drive, by trying more and keeping what helps.

    Only adjusts while the drive is the bottleneck (the readers are not waiting for
    analysis). Doubles the readers while each step brings at least READ_GAIN more
    throughput, steps back as soon as one does not, and re-checks every few minutes
    because folders differ (big RAW files, tiny thumbnails).
    """

    def __init__(self, start: int = 1):
        self.readers = max(1, min(MAX_READERS, start))
        self.settled = False
        self._last_rate: float | None = None
        self._window: tuple[float, int, float] | None = None
        self._settled_at = 0.0

    def tick(self, now: float, prog: "TargetProgress") -> None:
        if self._window is None:
            self._window = (now, prog._read_bytes, prog._wait_s)
            return
        t0, b0, w0 = self._window
        span = now - t0
        if span < READ_WINDOW:
            return
        self._window = (now, prog._read_bytes, prog._wait_s)
        rate = (prog._read_bytes - b0) / span
        drive_bound = (prog._wait_s - w0) < 0.25 * span * self.readers
        if self.settled:
            if now - self._settled_at > REPROBE_AFTER:
                self.settled, self._last_rate = False, None
            return
        if not drive_bound or rate <= 0:
            self._last_rate = None  # analysis is the limit: more readers would not help
            return
        if self._last_rate is None or rate > self._last_rate * READ_GAIN:
            self._last_rate = rate
            if self.readers < MAX_READERS:
                self.readers = min(MAX_READERS, self.readers * 2)
            else:
                self._settle(now)
        else:
            self.readers = max(1, self.readers // 2)  # the last step did not help: undo it
            self._settle(now)

    def _settle(self, now: float) -> None:
        self.settled, self._settled_at = True, now


class _RowFeed:
    """Pending files of a volume in path order, handed out to several reader threads."""

    def __init__(self, conn, volume_id: str, prefix: str):
        self.conn = conn
        self.volume_id = volume_id
        self.prefix = prefix
        self.lock = threading.Lock()
        self.buffer: deque = deque()
        self.last = ""
        self.exhausted = False

    def next(self):
        with self.lock:
            if not self.buffer and not self.exhausted:
                rows = self.conn.execute(
                    "SELECT id, rel_path, rel_path_raw, kind, size FROM files WHERE volume_id=? "
                    "AND rel_path > ? AND state='pending' AND substr(rel_path,1,?)=? "
                    "ORDER BY rel_path LIMIT 500",
                    (self.volume_id, self.last, len(self.prefix), self.prefix)).fetchall()
                if rows:
                    self.buffer.extend(rows)
                    self.last = rows[-1]["rel_path"]
                else:
                    self.exhausted = True
            return self.buffer.popleft() if self.buffer else None


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
    # Where the time goes, over the last 30 s: is the drive or the checking the limit?
    workers: int = 0
    readers: int = 1                  # parallel reads on this drive right now
    bottleneck: str = ""              # "drive" | "checking" | ""
    disk_rate_bps: float = 0.0        # what the drive delivers while files are being read
    analysis_ms: dict = field(default_factory=dict)   # average checking time per file, by kind
    _read_s: float = field(default=0.0, repr=False)    # reader: opening, reading, hashing
    _read_bytes: int = field(default=0, repr=False)
    _wait_s: float = field(default=0.0, repr=False)    # reader: blocked because checking is behind
    _analysis: dict = field(default_factory=dict, repr=False)   # kind -> [files, seconds]
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _samples: deque = field(default_factory=lambda: deque(maxlen=64), repr=False)

    def sample(self, now: float) -> None:
        self._samples.append((now, self.bytes_done, self._read_s, self._read_bytes, self._wait_s,
                              {k: tuple(v) for k, v in self._analysis.items()}))
        while len(self._samples) > 2 and now - self._samples[0][0] > 30:
            self._samples.popleft()
        t0, b0, r0, rb0, w0, a0 = self._samples[0]
        span = now - t0
        if span > 0.5:
            self.rate_bps = (self.bytes_done - b0) / span
            read_s, wait_s = self._read_s - r0, self._wait_s - w0
            # Reader time is summed over the parallel readers.
            busy = span * self.readers
            if read_s > 0:
                self.disk_rate_bps = (self._read_bytes - rb0) / read_s * self.readers
            self.bottleneck = ("checking" if wait_s > 0.5 * busy else
                               "drive" if read_s > 0.5 * busy else "")
            ms = {}
            for kind, (n, secs) in self._analysis.items():
                n0, s0 = a0.get(kind, (0, 0.0))
                if n - n0 > 0:
                    ms[kind] = round(1000 * (secs - s0) / (n - n0))
            self.analysis_ms = ms
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
        self.controllers: dict[int, ReadController] = {}
        for t in targets:
            self.controllers[t.scan_id] = ReadController(self._remembered_readers(t.volume_id))
            self.progress[t.scan_id] = TargetProgress(t.volume_id, t.label, t.scan_id, workers=self.workers,
                                                      readers=self.controllers[t.scan_id].readers)
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
            for t in targets:
                ctrl = self.controllers[t.scan_id]
                if ctrl.settled:
                    self._remember_readers(t.volume_id, ctrl.readers)
        return self.progress

    # Each drive's best reader count is remembered, so the next scan starts there.
    def _remembered_readers(self, volume_id: str) -> int:
        row = self.catalog.conn.execute("SELECT value FROM settings WHERE key = ?",
                                        (f"readers:{volume_id}",)).fetchone()
        return int(row[0]) if row else 1

    def _remember_readers(self, volume_id: str, readers: int) -> None:
        self.catalog.conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE "
                                  "SET value = excluded.value", (f"readers:{volume_id}", str(readers)))

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
            conn.close()

            feed_conn = self.catalog.connect(shared=True)
            feed = _RowFeed(feed_conn, t.volume_id, prefix)
            ctrl = self.controllers[t.scan_id]
            problems: list[Exception] = []

            def read_loop(index: int) -> None:
                while not self.stop_event.is_set() and not problems:
                    if index >= ctrl.readers:  # not needed right now
                        if feed.exhausted:
                            return
                        time.sleep(0.2)
                        continue
                    row = feed.next()
                    if row is None:
                        return
                    try:
                        self._read_one(t, row)
                    except Exception as e:  # VolumeOffline or unexpected: stop this volume
                        problems.append(e)
                        return

            threads = [threading.Thread(target=read_loop, args=(i,), name=f"reader-{t.label}-{i}", daemon=True)
                       for i in range(MAX_READERS)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            feed_conn.close()
            if problems:
                raise problems[0]
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
        waited = time.monotonic()
        if not self.budget.acquire(reserve, self.stop_event.is_set):
            return
        started = time.monotonic()
        with prog._lock:
            prog._wait_s += started - waited
        try:
            st, data, h = self._read_file(path, inline)
        except OSError as e:
            self.budget.release(reserve)
            if not t.present():
                raise discover.VolumeOffline(str(e)) from e
            state = "missing" if isinstance(e, FileNotFoundError) else "read_error"
            self._post(_Failed(t, row["id"], size_hint, state, f"{type(e).__name__}: {e}"))
            return
        with prog._lock:
            prog._read_s += time.monotonic() - started
            prog._read_bytes += st.st_size
        if data is not None and len(data) != reserve:  # file changed size since discovery
            self.budget.release(reserve)
            self.budget.acquire(len(data), lambda: False)
        waited = time.monotonic()
        self._post(_Read(t, row["id"], row["rel_path"], row["kind"], st.st_size, st.st_mtime_ns, h, data,
                         path if isinstance(path, str) else os.fsdecode(path)))
        with prog._lock:
            prog._wait_s += time.monotonic() - waited

    @staticmethod
    def _read_file(path, inline: bool):
        """Stat, read and hash one file: (stat, bytes or None, BLAKE3 digest)."""
        st = os.stat(path)
        if inline and st.st_size <= INLINE_LIMIT:
            with open(path, "rb") as f:
                data = f.read()
            return st, data, blake3.blake3(data, max_threads=blake3.blake3.AUTO).digest()
        hasher = blake3.blake3(max_threads=blake3.blake3.AUTO)
        with open(path, "rb") as f:
            while chunk := f.read(READ_CHUNK):
                hasher.update(chunk)
        return st, None, hasher.digest()

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
        if entry.waiters and r.seconds:
            stats = self.progress[entry.waiters[0].target.scan_id]._analysis.setdefault(entry.job.kind, [0, 0.0])
            stats[0] += 1
            stats[1] += r.seconds
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
        for scan_id, prog in self.progress.items():
            if prog.state == "analyzing":
                ctrl = self.controllers[scan_id]
                ctrl.tick(now, prog)
                prog.readers = ctrl.readers
            prog.sample(now)
        if self.progress_cb:
            self.progress_cb(list(self.progress.values()))
