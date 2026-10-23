"""Check a packaged build end to end: scan the sample library with the frozen binary.

    uv run python packaging/smoke_test.py dist/imagelib/imagelib[.exe]

Runs the binary with a clean environment (so nothing comes from the development
virtualenv), then checks the catalog it wrote.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
import samplelib  # noqa: E402

binary = Path(sys.argv[1]).resolve()
work = Path(tempfile.mkdtemp(prefix="imagelib-smoke-"))
drive, catalog = work / "drive", work / "catalog"
samplelib.build(drive)
env = {k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE")}
env["IMAGELIB_HOME"] = str(catalog)
env["PATH"] = os.pathsep.join(p for p in (os.environ.get("SYSTEMROOT", ""), "/usr/bin", "/bin") if p)


def run(*args: str) -> str:
    out = subprocess.run([str(binary), *args], env=env, capture_output=True, text=True, timeout=600)
    print(f"$ imagelib {' '.join(args)}\n{out.stdout}{out.stderr}", flush=True)
    if out.returncode != 0:
        sys.exit(f"exit code {out.returncode}")
    return out.stdout


run("--help")
run("volume", "add", str(drive), "--label", "Smoke")
run("scan", str(drive), "--workers", "2")
run("relate")
db = sqlite3.connect(catalog / "catalog.db")
statuses = dict(db.execute("SELECT status, count(*) FROM contents GROUP BY status").fetchall())
kinds = dict(db.execute("SELECT kind, count(*) FROM contents WHERE status = 'ok' GROUP BY kind").fetchall())
derived = db.execute("SELECT count(*) FROM similar_pairs WHERE kind = 'derived'").fetchone()[0]
print("statuses", statuses, "ok kinds", kinds, "derived", derived)
assert statuses.get("corrupt") == 4 and statuses.get("warning") == 1, statuses
assert kinds.get("video") == 1 and kinds.get("psd") == 1 and kinds.get("image", 0) >= 12, kinds
assert derived == 2, derived

# The GUI: start the server and fetch the page and the API.
server = subprocess.Popen([str(binary), "gui", "--no-browser", "--port", "8799"], env=env)
try:
    for _ in range(60):
        try:
            page = urllib.request.urlopen("http://127.0.0.1:8799/", timeout=2).read()
            summary = urllib.request.urlopen("http://127.0.0.1:8799/api/summary", timeout=5).read()
            break
        except OSError:
            time.sleep(0.5)
    else:
        sys.exit("the GUI server did not start")
    assert b'<div id="root">' in page and b'"files":22' in summary.replace(b" ", b"")
finally:
    server.terminate()
    server.wait(30)
print("smoke test passed")
