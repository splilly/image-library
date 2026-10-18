"""Parallel reads per drive: more for drives that benefit, one for drives that suffer."""

import random
import threading
import time

import pytest
from conftest import register

from imagelib import scanner
from imagelib.previews import PreviewStore
from imagelib.scanner import Scanner


@pytest.fixture
def many_files(tmp_path):
    root = tmp_path / "drive"
    rng = random.Random(3)
    for i in range(500):
        p = root / f"d{i // 100}" / f"IMG_{i:04d}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(rng.randbytes(20_000))  # not decodable: checking takes ~nothing
    return root


@pytest.fixture
def fast_tuning(monkeypatch):
    monkeypatch.setattr(scanner, "READ_WINDOW", 0.4)
    monkeypatch.setattr(scanner, "REPROBE_AFTER", 3600)


def scan_with(catalog, root, read_file):
    vid = register(catalog, root)
    s = Scanner(catalog, PreviewStore(catalog.previews_path), workers=2)
    s._read_file = read_file
    target = s.prepare(vid, "Sim", root)
    prog = s.run([target])[target.scan_id]
    return prog, s.controllers[target.scan_id], vid


def test_drive_with_per_file_latency_gets_more_readers(many_files, catalog, fast_tuning):
    """Like a USB SSD whose files are inspected by an antivirus as they are opened."""
    def read_file(path, inline):
        time.sleep(0.02)  # latency that overlaps when several reads are in flight
        return Scanner._read_file(path, inline)

    prog, ctrl, vid = scan_with(catalog, many_files, read_file)
    assert prog.state == "done" and prog.files_done == 500
    assert ctrl.readers >= 4
    remembered = catalog.conn.execute("SELECT value FROM settings WHERE key = ?", (f"readers:{vid}",)).fetchone()
    assert remembered is None or int(remembered[0]) == ctrl.readers


def test_seeking_drive_stays_at_one_reader(many_files, catalog, fast_tuning):
    """Like a spinning disk: every extra read in flight makes all of them slower."""
    active = [0]
    lock = threading.Lock()

    def read_file(path, inline):
        with lock:
            active[0] += 1
            n = active[0]
        try:
            time.sleep(0.01 * n ** 1.5)  # throughput falls as reads compete (seeking)
            return Scanner._read_file(path, inline)
        finally:
            with lock:
                active[0] -= 1

    prog, ctrl, vid = scan_with(catalog, many_files, read_file)
    assert prog.state == "done" and prog.files_done == 500
    # On a slow machine the scan can end while a second reader is still being tried.
    assert ctrl.readers == 1 or (ctrl.readers == 2 and not ctrl.settled)


def test_controller_steps_back_when_more_readers_do_not_help():
    class P:  # just the counters the controller reads
        _read_bytes = 0
        _wait_s = 0.0

    ctrl, p, now = scanner.ReadController(), P(), 0.0
    rates = {1: 100, 2: 190, 4: 200}  # 1 -> 2 helps a lot, 2 -> 4 barely
    ctrl.tick(now, p)
    for _ in range(4):
        now += scanner.READ_WINDOW
        p._read_bytes += rates[ctrl.readers] * scanner.READ_WINDOW
        ctrl.tick(now, p)
    assert ctrl.readers == 2 and ctrl.settled
