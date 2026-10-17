"""Command line interface. The GUI (``imagelib serve``) arrives in milestone 2."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from . import volumes
from .appdirs import default_catalog_dir
from .catalog import Catalog
from .previews import PreviewStore
from .scanner import Scanner, TargetProgress


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(s: float | None) -> str:
    if s is None:
        return "--"
    s = int(s)
    if s >= 86400:
        return f"{s // 86400}d{s % 86400 // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s"


def open_catalog(args) -> Catalog:
    return Catalog(Path(args.catalog) if args.catalog else default_catalog_dir())


# -- volume ------------------------------------------------------------------

def cmd_volume_add(args) -> int:
    cat = open_catalog(args)
    root = Path(args.path)
    label = args.label or (volumes.read_marker(root).label if volumes.read_marker(root) else None) \
        or root.resolve().name or str(root)
    try:
        marker = volumes.init_volume(root, label, cat.machine_id)
    except volumes.VolumeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    cat.upsert_volume(marker.id, marker.label, None, volumes.total_bytes(marker.root))
    cat.mark_volume_seen(marker.id, str(marker.root))
    print(f"Registered volume '{marker.label}' ({marker.id[:8]}) at {marker.root}")
    print(f"Scan it with:  imagelib scan \"{marker.root}\"")
    return 0


def cmd_volume_list(args) -> int:
    cat = open_catalog(args)
    vols = cat.list_volumes()
    if not vols:
        print("No volumes registered. Use: imagelib volume add <drive root> --label \"My drive\"")
        return 0
    online = volumes.locate_online({v["id"]: v["last_seen_path"] for v in vols})
    print(f"{'':2}{'LABEL':24} {'ID':9} {'FILES':>10} {'SIZE':>10} {'PENDING':>9}  LOCATION")
    for v in vols:
        st = cat.volume_stats(v["id"])
        where = str(online[v["id"]]) if v["id"] in online else \
            f"offline (last seen {time.strftime('%Y-%m-%d', time.localtime(v['last_seen_at'] or 0))})"
        dot = "●" if v["id"] in online else "○"
        print(f"{dot} {v['label'][:24]:24} {v['id'][:8]:9} {st['files']:>10,} {human_bytes(st['bytes']):>10} "
              f"{st['pending']:>9,}  {where}")
    return 0


# -- scan --------------------------------------------------------------------

class ProgressPrinter:
    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.lines = 0
        self.last_plain = 0.0

    def line(self, p: TargetProgress) -> str:
        name = f"[{p.label[:18]}]"
        if p.state == "discovering":
            return f"{name:20} discovering   found {p.discovered:,} media files"
        pct = 100.0 * p.bytes_done / p.bytes_total if p.bytes_total else 100.0
        text = (f"{name:20} {p.state:11} {pct:5.1f}%  {p.files_done:,}/{p.files_total:,} files  "
                f"{human_bytes(p.bytes_done)}/{human_bytes(p.bytes_total)}  "
                f"{human_bytes(p.rate_bps)}/s  ETA {human_duration(p.eta_s)}  "
                f"corrupt {p.corrupt}  warn {p.warnings}  copies {p.reused}  read-err {p.read_errors}")
        if p.message:
            text += f"  ({p.message})"
        return text

    def __call__(self, progress: list[TargetProgress], final: bool = False) -> None:
        if self.tty:
            out = "".join(f"\x1b[2K{self.line(p)}\n" for p in progress)
            sys.stdout.write((f"\x1b[{self.lines}F" if self.lines else "") + out)
            sys.stdout.flush()
            self.lines = len(progress)
        elif final or time.monotonic() - self.last_plain > 10:
            self.last_plain = time.monotonic()
            for p in progress:
                print(self.line(p), flush=True)


def _resolve_targets(cat: Catalog, scanner: Scanner, args) -> list:
    targets = []
    if args.paths:
        for raw in args.paths:
            path = Path(raw).resolve()
            marker = volumes.find_volume_root(path)
            if marker is None:
                raise SystemExit(f"error: {path} is not on a registered volume. Register the drive first:\n"
                                 f"  imagelib volume add <drive root> --label \"Name\"")
            if cat.get_volume(marker.id) is None:  # registered by another machine's catalog
                cat.upsert_volume(marker.id, marker.label, None, volumes.total_bytes(marker.root))
            sub = path.relative_to(marker.root).as_posix()
            targets.append(scanner.prepare(marker.id, cat.get_volume(marker.id)["label"], marker.root,
                                           "" if sub == "." else sub))
        return targets

    vols = {v["id"]: v for v in cat.list_volumes()}
    online = volumes.locate_online({vid: v["last_seen_path"] for vid, v in vols.items()})
    if args.all:
        for vid, root in online.items():
            targets.append(scanner.prepare(vid, vols[vid]["label"], root))
    else:
        for s in cat.unfinished_scans():
            if s["volume_id"] in online:
                targets.append(scanner.prepare(s["volume_id"], s["label"], online[s["volume_id"]],
                                               s["root_subpath"]))
            else:
                print(f"Waiting for drive '{s['label']}' to be connected to resume its scan.")
    return targets


def cmd_scan(args) -> int:
    cat = open_catalog(args)
    previews = PreviewStore(cat.previews_path)
    printer = ProgressPrinter()
    crash_after = os.environ.get("IMAGELIB_TEST_CRASH_AFTER")  # tests: simulate power loss
    scanner = Scanner(cat, previews, workers=args.workers, make_medium_previews=not args.no_medium_previews,
                      progress_cb=printer, crash_after_files=int(crash_after) if crash_after else None)
    targets = _resolve_targets(cat, scanner, args)
    if not targets:
        print("Nothing to scan. Give a path, use --all for every connected volume, "
              "or run `imagelib volume list`.")
        return 0

    interrupts = 0

    def on_sigint(signum, frame):
        nonlocal interrupts
        interrupts += 1
        if interrupts == 1:
            print("\nPausing: finishing files in progress (Ctrl+C again to stop immediately; "
                  "nothing is lost either way)...", flush=True)
            scanner.stop()
        else:
            raise SystemExit(130)

    signal.signal(signal.SIGINT, on_sigint)
    started = time.monotonic()
    progress = scanner.run(targets)
    printer(list(progress.values()), final=True)
    print(f"\nFinished in {human_duration(time.monotonic() - started)}.")
    for p in progress.values():
        if p.state == "paused":
            print(f"  {p.label}: paused. Run `imagelib scan` to resume.")
        elif p.state == "offline":
            print(f"  {p.label}: drive disconnected. Reconnect it and run `imagelib scan` to resume.")
        elif p.state == "failed":
            print(f"  {p.label}: FAILED: {p.message}")
    return 0 if all(p.state == "done" for p in progress.values()) else 2


# -- reports -----------------------------------------------------------------

def cmd_status(args) -> int:
    cat = open_catalog(args)
    s = cat.summary()
    print(f"Catalog:          {cat.dir}")
    print(f"Files:            {s['files']:,} ({human_bytes(s['bytes'])})")
    print(f"  pending:        {s['pending']:,}")
    print(f"  read errors:    {s['read_errors']:,}")
    print(f"  missing:        {s['missing']:,}")
    print(f"Unique contents:  {s['unique_contents']:,}")
    print(f"  corrupt:        {s['corrupt']:,}")
    print(f"  warnings:       {s['warnings']:,}")
    print(f"  unsupported:    {s['unsupported']:,}")
    print(f"Exact duplicates: {s['duplicate_groups']:,} groups, {s['redundant_copies']:,} extra copies "
          f"({human_bytes(s['redundant_bytes'])})")
    scans = cat.unfinished_scans()
    if scans:
        print("Unfinished scans:")
        for sc in scans:
            print(f"  {sc['label']}{'/' + sc['root_subpath'] if sc['root_subpath'] else ''}: {sc['state']}"
                  f"{' - ' + sc['message'] if sc['message'] else ''}")
    return 0


def cmd_problems(args) -> int:
    cat = open_catalog(args)
    statuses = ("corrupt", "warning", "unsupported") if args.status == "all" else (args.status,)
    rows = cat.conn.execute(
        f"SELECT c.status, c.problems, v.label, f.rel_path FROM contents c "
        f"JOIN files f ON f.content_hash=c.hash JOIN volumes v ON v.id=f.volume_id "
        f"WHERE c.status IN ({','.join('?' * len(statuses))}) ORDER BY c.status, v.label, f.rel_path LIMIT ?",
        (*statuses, args.limit)).fetchall()
    rows += cat.conn.execute(
        "SELECT 'read_error', f.error, v.label, f.rel_path FROM files f JOIN volumes v ON v.id=f.volume_id "
        "WHERE f.state='read_error' LIMIT ?", (args.limit,)).fetchall() if args.status == "all" else []
    for status, problems, label, rel in rows:
        print(f"{status:11} [{label}] {rel}\n{'':12}{problems}")
    if not rows:
        print("No problems found.")
    return 0


def cmd_where(args) -> int:
    from .search import where

    cat = open_catalog(args)
    found = where(cat, Path(args.file), args.distance)
    if not found:
        print("Not found in the catalog.")
        return 1
    vols = {v["id"]: v for v in cat.list_volumes()}
    online = volumes.locate_online({vid: v["last_seen_path"] for vid, v in vols.items()})
    for loc in found:
        match = "identical" if loc.exact else f"similar (distance {loc.distance})"
        status = f"online at {online[loc.volume_id]}" if loc.volume_id in online else "drive offline"
        print(f"{match:22} [{loc.volume_label}] {loc.rel_path}   ({status}{', ' + loc.state if loc.state != 'done' else ''})")
    return 0


# -- entry point ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="imagelib", description="Catalog, verify and de-duplicate photo libraries "
                                                             "spread over many drives.")
    p.add_argument("--catalog", help=f"catalog directory (default: {default_catalog_dir()})")
    sub = p.add_subparsers(dest="command", required=True)

    vol = sub.add_parser("volume", help="register and list drives").add_subparsers(dest="volume_cmd", required=True)
    add = vol.add_parser("add", help="register a drive (writes a small marker file at its root)")
    add.add_argument("path")
    add.add_argument("--label", help="a name you will recognise, e.g. 'WD 2TB blue sticker'")
    add.set_defaults(func=cmd_volume_add)
    vol.add_parser("list", help="list volumes and whether they are connected").set_defaults(func=cmd_volume_list)

    scan = sub.add_parser("scan", help="scan volumes; resumes unfinished scans when run without arguments")
    scan.add_argument("paths", nargs="*", help="volume roots or folders inside registered volumes")
    scan.add_argument("--all", action="store_true", help="scan every connected volume")
    scan.add_argument("--workers", type=int, help="analysis processes (default: CPU count - 1)")
    scan.add_argument("--no-medium-previews", action="store_true",
                      help="only store small thumbnails (saves ~70%% of preview space)")
    scan.set_defaults(func=cmd_scan)

    sub.add_parser("status", help="catalog summary").set_defaults(func=cmd_status)

    prob = sub.add_parser("problems", help="list corrupt or suspicious files")
    prob.add_argument("--status", choices=("corrupt", "warning", "unsupported", "all"), default="all")
    prob.add_argument("--limit", type=int, default=200)
    prob.set_defaults(func=cmd_problems)

    wh = sub.add_parser("where", help="find which drive(s) hold a copy of an image")
    wh.add_argument("file")
    wh.add_argument("--distance", type=int, default=8, help="max perceptual difference for look-alikes")
    wh.set_defaults(func=cmd_where)
    return p


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        os.system("")  # enable ANSI escape handling in the Windows console
    args = build_parser().parse_args(argv)
    return args.func(args)
