import multiprocessing
import time

from conftest import file_row, register, scan

from imagelib import analyze


def _hold(gate, units_wanted, active, peak, lock):
    n = gate.reserve(units_wanted * analyze.DecodeGate.UNIT)
    with lock:
        active.value += 1
        peak.value = max(peak.value, active.value)
    time.sleep(0.3)
    with lock:
        active.value -= 1
    gate.release(n)


def test_decode_gate_limits_concurrent_large_decodes_across_processes():
    ctx = multiprocessing.get_context("spawn")
    gate = analyze.DecodeGate(ctx, 5 * analyze.DecodeGate.UNIT)  # room for one 3-unit job at a time
    active, peak, lock = ctx.Value("i", 0), ctx.Value("i", 0), ctx.Lock()
    procs = [ctx.Process(target=_hold, args=(gate, 3, active, peak, lock)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    assert all(p.exitcode == 0 for p in procs)
    assert peak.value == 1


def test_decode_gate_lets_small_decodes_through_and_caps_oversized_requests():
    ctx = multiprocessing.get_context("spawn")
    gate = analyze.DecodeGate(ctx, 4 * analyze.DecodeGate.UNIT)
    assert gate.reserve(100 * 1024 * 1024) == 0          # ordinary photo: no reservation
    n = gate.reserve(100 * analyze.DecodeGate.UNIT)      # bigger than the whole budget
    assert n == 4                                        # runs alone instead of waiting forever
    gate.release(n)


def test_workers_that_cannot_start_fail_the_scan_without_blaming_files(drive, catalog, monkeypatch):
    monkeypatch.setenv("IMAGELIB_TEST_BROKEN_WORKERS", "1")
    root, rel = drive
    vid = register(catalog, root)
    prog = scan(catalog, root, vid)
    assert prog.state == "failed" and "no files were marked corrupt" in prog.message
    assert catalog.conn.execute("SELECT count(*) FROM contents WHERE status != 'other'").fetchone()[0] == 0
    assert file_row(catalog, vid, rel["photo1"])["state"] == "pending"
