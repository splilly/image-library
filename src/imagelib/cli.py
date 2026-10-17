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
    if args.read_only:
        return _volume_add_read_only(cat, root, args.label)
    label = args.label or (volumes.read_marker(root).label if volumes.read_marker(root) else None) \
        or root.resolve().name or str(root)
    try:
        marker = volumes.init_volume(root, label, cat.machine_id)
    except volumes.VolumeError as e:
        print(f"error: {e}", file=sys.stderr)
        if root.exists() and not os.access(root, os.W_OK):
            print("The drive is not writable. To catalog it without writing anything, use --read-only.",
                  file=sys.stderr)
        return 1
    cat.upsert_volume(marker.id, marker.label, None, volumes.total_bytes(marker.root))
    cat.mark_volume_seen(marker.id, str(marker.root))
    print(f"Registered volume '{marker.label}' ({marker.id[:8]}) at {marker.root}")
    print(f"Scan it with:  imagelib scan \"{marker.root}\"")
    return 0


def _volume_add_read_only(cat: Catalog, path: Path, label: str | None) -> int:
    try:
        ref = volumes.register_read_only(cat, path, label)
    except volumes.VolumeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    root = cat.get_volume(ref.id)["last_seen_path"]
    ids = ", ".join(f"{kind} {value}" for kind, value in sorted(ref.fingerprints))
    print(f"Registered read-only volume '{ref.label}' ({ref.id[:8]}) at {root}")
    print(f"Nothing was written to it. It is recognised by: {ids}, capacity {human_bytes(ref.total_bytes or 0)}.")
    print("Note: reformatting the drive changes this identity; a cloned drive would share it.")
    print(f"Scan it with:  imagelib scan \"{root}\"")
    return 0


def cmd_volume_list(args) -> int:
    cat = open_catalog(args)
    vols = cat.list_volumes()
    if not vols:
        print("No volumes registered. Use: imagelib volume add <drive root> --label \"My drive\"")
        return 0
    online = volumes.locate_online(cat)
    print(f"{'':2}{'LABEL':24} {'ID':9} {'FILES':>10} {'SIZE':>10} {'PENDING':>9}  LOCATION")
    for v in vols:
        st = cat.volume_stats(v["id"])
        where = str(online[v["id"]]) if v["id"] in online else \
            f"offline (last seen {time.strftime('%Y-%m-%d', time.localtime(v['last_seen_at'] or 0))})"
        dot = "●" if v["id"] in online else "○"
        where += "  [read-only]" if v["read_only"] else ""
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
        if p.bottleneck == "drive":
            text += f"  [limited by the drive: {human_bytes(p.disk_rate_bps)}/s]"
        elif p.bottleneck == "checking":
            per = ", ".join(f"{k} {v} ms" for k, v in sorted(p.analysis_ms.items()))
            text += f"  [limited by checking, {p.workers} workers: {per}]"
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
            found = volumes.resolve_path(cat, path)
            if found is None:
                raise SystemExit(f"error: {path} is not on a registered volume. Register the drive first:\n"
                                 f"  imagelib volume add <drive root> --label \"Name\"\n"
                                 f"  (add --read-only to catalog it without writing anything to it)")
            ref, root = found
            sub = path.relative_to(root).as_posix()
            targets.append(scanner.prepare(ref.id, ref.label, root, "" if sub == "." else sub))
        return targets

    vols = {v["id"]: v for v in cat.list_volumes()}
    online = volumes.locate_online(cat)
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
    online = volumes.locate_online(cat)
    for loc in found:
        match = "identical" if loc.exact else f"similar (distance {loc.distance})"
        status = f"online at {online[loc.volume_id]}" if loc.volume_id in online else "drive offline"
        print(f"{match:22} [{loc.volume_label}] {loc.rel_path}   ({status}{', ' + loc.state if loc.state != 'done' else ''})")
    return 0


def cmd_relate(args) -> int:
    from . import relate

    cat = open_catalog(args)
    tty = sys.stdout.isatty()

    def show(p) -> None:
        pct = f"{100 * p.done / p.total:5.1f}%" if p.total else "     "
        line = f"Relating images: {p.phase:10} {pct}  {p.pairs:,} relations found"
        print(("\r" + line) if tty else line, end="" if tty else "\n", flush=True)

    stop = {"flag": False}

    def on_sigint(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, on_sigint)
    finished = relate.run(cat, lambda: stop["flag"], show)
    print()
    print("Done." if finished else "Paused; run `imagelib relate` again to continue.")
    return 0 if finished else 2


def cmd_plan(args) -> int:
    from . import plans

    cat = open_catalog(args)
    online = volumes.locate_online(cat)
    if args.plan_cmd == "create":
        p = plans.create(cat)
        print(f"Created plan {p['id']} with {p['items']:,} files." if p["id"] else "No new discard decisions to plan.")
        return 0
    if args.plan_cmd == "list":
        for p in plans.list_plans(cat):
            counts = ", ".join(f"{k} {v['files']:,}" for k, v in sorted(p["counts"].items()))
            print(f"plan {p['id']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(p['created_at']))}  {counts}")
        return 0
    if args.plan_cmd == "run":
        refs = {r["id"]: volumes.volume_ref(cat, r) for r in cat.list_volumes()}
        opts = plans.RunOptions(verify_hash=not args.no_verify, require_connected_copy=not args.trust_catalog)
        prog = plans.run(cat, args.id, online, refs, opts)
        print(f"Moved {prog.moved:,} files ({human_bytes(prog.moved_bytes)}) to quarantine; "
              f"{prog.blocked:,} blocked, {prog.skipped:,} skipped, {prog.failed:,} failed. {prog.message}")
        for v in prog.waiting_volumes:
            print(f"  waiting for drive '{refs[v].label}' to be connected")
        return 0 if prog.state == "done" else 2
    if args.plan_cmd == "restore":
        r = plans.restore(cat, args.id, online)
        print(f"Restored {r['restored']:,} files.")
        for p in r["problems"]:
            print("  " + p)
        return 0
    if args.plan_cmd == "purge":
        if args.confirm != "delete":
            print('Purging deletes the quarantined files permanently. Re-run with --confirm delete.')
            return 1
        r = plans.purge(cat, args.id, online)
        print(f"Deleted {r['purged']:,} files ({human_bytes(r['bytes'])}).")
        for p in r["problems"]:
            print("  " + p)
        return 0
    return 1


def cmd_agent(args) -> int:
    from . import sync

    cat = open_catalog(args)
    if args.agent_cmd == "add":
        a = sync.add_agent(cat, args.name)
        print(f"Agent '{args.name}' added. Its token (shown only once):\n\n  {a['token']}\n")
        print("On the agent machine run:  imagelib hub connect <this hub's https address> <token>")
        return 0
    if args.agent_cmd == "list":
        for a in sync.list_agents(cat):
            seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(a["last_sync_at"])) if a["last_sync_at"] else "never"
            print(f"{a['name']:20} {a['machine'] or '?':16} last sync {seen}   drives: {', '.join(a['connected_volumes']) or '-'}")
        return 0
    return 1


def cmd_hub(args) -> int:
    from . import sync
    from .previews import PreviewStore

    cat = open_catalog(args)
    if args.hub_cmd == "connect":
        cfg = sync.load_hub(cat)
        cfg.url, cfg.token = args.url, args.token
        cfg.cf_client_id, cfg.cf_client_secret = args.cf_client_id or "", args.cf_client_secret or ""
        sync.save_hub(cat, cfg)
        print(f"This computer will send its scans to {args.url}. Run `imagelib hub sync` to sync now.")
        return 0
    if args.hub_cmd == "sync":
        cfg = sync.load_hub(cat)
        if not cfg.url:
            print("Not connected to a hub. Use: imagelib hub connect <url> <token>")
            return 1
        sent = sync.sync_with_hub(cat, PreviewStore(cat.previews_path), sync.HubClient(cfg).post)
        print(f"Sent {sent['files']:,} file changes, {sent['contents']:,} analyses, {sent['previews']:,} previews; "
              f"carried out {sent['actions']:,} actions from the hub.")
        return 0
    return 1


def cmd_bundle(args) -> int:
    from . import sync
    from .previews import PreviewStore

    cat = open_catalog(args)
    previews = PreviewStore(cat.previews_path)
    if args.bundle_cmd == "export":
        counts = sync.export_bundle(cat, previews, Path(args.file), full=args.full)
        print(f"Wrote {args.file}: {counts['files']:,} file changes, {counts['contents']:,} analyses.")
    else:
        counts = sync.import_bundle(cat, previews, Path(args.file))
        print(f"Imported {counts['files']:,} file changes, {counts['contents']:,} analyses, "
              f"{counts['previews']:,} previews.")
    return 0


def cmd_organize(args) -> int:
    from . import organize, relate

    cat = open_catalog(args)
    if args.resume:
        export_id = args.resume
    else:
        if not args.target:
            print("Give a target folder, or --resume ID.")
            return 1
        if not relate.is_current(cat):
            print("Finding thumbnails and similar images first...", flush=True)
            relate.run(cat)
        e = organize.create(cat, Path(args.target), organize.Options(
            include_videos=not args.no_videos, include_raw=not args.no_raw, include_damaged=args.include_damaged))
        export_id = e["id"]
        print(f"Export {export_id}: {e['items']:,} pictures, {human_bytes(e['bytes'])}.", flush=True)
    prog = organize.run(cat, export_id, volumes.locate_online(cat))
    print(f"Copied {prog.copied:,} files ({human_bytes(prog.copied_bytes)}); {prog.failed:,} failed; "
          f"{prog.waiting:,} waiting for drives: {', '.join(prog.waiting_volumes) or '-'}.")
    if prog.waiting:
        print(f"Connect them and run `imagelib organize --resume {export_id}`.")
    return 0 if prog.failed == 0 else 2


# -- GUI ------------------------------------------------------------------------

def cmd_serve(args) -> int:
    import threading
    import webbrowser

    import uvicorn

    from .server import create_app

    cat = open_catalog(args)
    app = create_app(cat.dir, workers=args.workers, port=args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Image Library is running at {url}", flush=True)
    print(f"Catalog: {cat.dir}")
    print("Keep this window open while you use it. To stop: close this window or press Ctrl+C;")
    print("a running scan is paused and resumes next time.", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # 127.0.0.1 only: the API can read your drives, so it must not be reachable from the network.
    # The live-update stream never ends on its own, so without a timeout an open browser
    # tab would keep the server from ever shutting down (Ctrl+C would hang).
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", timeout_graceful_shutdown=3)
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
    add.add_argument("--read-only", action="store_true",
                     help="never write to this drive; recognise it by its filesystem serial number instead "
                          "of a marker file")
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

    plan = sub.add_parser("plan", help="carry out discard decisions (quarantine, restore, purge)") \
        .add_subparsers(dest="plan_cmd", required=True)
    plan.add_parser("create", help="snapshot current discard decisions into a new plan").set_defaults(func=cmd_plan)
    plan.add_parser("list", help="list plans").set_defaults(func=cmd_plan)
    prun = plan.add_parser("run", help="move a plan's files to quarantine on connected drives")
    prun.add_argument("id", type=int)
    prun.add_argument("--no-verify", action="store_true", help="skip re-hashing files before moving them")
    prun.add_argument("--trust-catalog", action="store_true",
                      help="accept a kept copy on a disconnected drive as proof it still exists")
    prun.set_defaults(func=cmd_plan)
    pres = plan.add_parser("restore", help="move a plan's quarantined files back")
    pres.add_argument("id", type=int)
    pres.set_defaults(func=cmd_plan)
    ppur = plan.add_parser("purge", help="permanently delete a plan's quarantined files")
    ppur.add_argument("id", type=int)
    ppur.add_argument("--confirm", default="", help='must be "delete"')
    ppur.set_defaults(func=cmd_plan)

    agent = sub.add_parser("agent", help="(on the hub) let other computers send their scans here") \
        .add_subparsers(dest="agent_cmd", required=True)
    aadd = agent.add_parser("add", help="create a token for a new agent")
    aadd.add_argument("name")
    aadd.set_defaults(func=cmd_agent)
    agent.add_parser("list", help="list agents").set_defaults(func=cmd_agent)

    hub = sub.add_parser("hub", help="(on an agent) send this computer's scans to a hub") \
        .add_subparsers(dest="hub_cmd", required=True)
    hcon = hub.add_parser("connect", help="set the hub's address and this agent's token")
    hcon.add_argument("url", help="the hub's https:// address (Tailscale or Cloudflare)")
    hcon.add_argument("token")
    hcon.add_argument("--cf-client-id", help="Cloudflare Access service token id, if the hub is behind Cloudflare")
    hcon.add_argument("--cf-client-secret")
    hcon.set_defaults(func=cmd_hub)
    hub.add_parser("sync", help="sync with the hub now").set_defaults(func=cmd_hub)

    bundle = sub.add_parser("bundle", help="carry scans between computers on a USB stick") \
        .add_subparsers(dest="bundle_cmd", required=True)
    bexp = bundle.add_parser("export", help="write changes since the last export to a file")
    bexp.add_argument("file")
    bexp.add_argument("--full", action="store_true", help="everything, not just changes")
    bexp.set_defaults(func=cmd_bundle)
    bimp = bundle.add_parser("import", help="merge a bundle from another computer")
    bimp.add_argument("file")
    bimp.set_defaults(func=cmd_bundle)

    org = sub.add_parser("organize", help="copy one of each picture into a new library sorted by date")
    org.add_argument("target", nargs="?", help="folder for the new library")
    org.add_argument("--resume", type=int, metavar="ID", help="continue an earlier export")
    org.add_argument("--no-videos", action="store_true")
    org.add_argument("--no-raw", action="store_true")
    org.add_argument("--include-damaged", action="store_true")
    org.set_defaults(func=cmd_organize)

    sub.add_parser("relate", help="find thumbnails, resized and near-identical copies (resumable)") \
        .set_defaults(func=cmd_relate)

    serve = sub.add_parser("gui", aliases=["serve"], help="open the graphical interface in your browser")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    serve.add_argument("--workers", type=int, help="analysis processes for scans (default: automatic)")
    serve.set_defaults(func=cmd_serve)

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
        import ctypes
        # Probing empty card readers must not pop up "There is no disk in the drive" dialogs.
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x8000)
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[:1] == ["--catalog"] and len(argv) == 2:
        # Double-clicked (or run without a command): open the graphical interface.
        argv = argv + ["gui"]
    args = build_parser().parse_args(argv)
    return args.func(args)
