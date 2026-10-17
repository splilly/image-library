"""HTTP API and static GUI, served on localhost only.

Security: the API can list folders and stream photos, so besides binding to
127.0.0.1 it rejects requests whose Host header is not local (DNS rebinding) and
requires a custom header on every state-changing request (a cross-site page cannot
send one without a CORS preflight, which is never granted).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analyze, discover, formats, queries, volumes
from .catalog import Catalog
from .jobs import OnlineVolumes, ScanManager
from .previews import PreviewStore
from .search import hash_file

UI_DIR = Path(__file__).parent / "ui"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
CSRF_HEADER = "x-imagelib"
BROWSER_FORMATS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "jpe": "image/jpeg", "jfif": "image/jpeg",
                   "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}


class LocalOnly:
    """Pure ASGI middleware (BaseHTTPMiddleware interferes with streaming responses)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            problem = None
            if request.url.hostname not in LOCAL_HOSTS:
                problem = "forbidden host"
            elif request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
                problem = f"missing {CSRF_HEADER} header"
            if problem:
                await JSONResponse({"detail": problem}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class AddVolume(BaseModel):
    path: str
    label: str | None = None
    read_only: bool = False


class EditVolume(BaseModel):
    label: str | None = None
    notes: str | None = None


class ScanRequest(BaseModel):
    volume_ids: list[str] = []
    path: str | None = None        # a folder on a registered volume
    all_online: bool = False
    resume: bool = False           # unfinished scans on connected volumes


def create_app(catalog_dir: Path, workers: int | None = None) -> FastAPI:
    catalog_dir = Path(catalog_dir)
    Catalog.shared(catalog_dir)  # create / migrate up front
    online = OnlineVolumes(catalog_dir)
    scans = ScanManager(catalog_dir, online, workers=workers)
    phash_index = queries.PhashIndex()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(scans.shutdown)

    app = FastAPI(title="imagelib", lifespan=lifespan)
    app.state.scans = scans
    app.add_middleware(LocalOnly)

    def cat() -> Catalog:
        return Catalog.shared(catalog_dir)

    def previews() -> PreviewStore:
        return PreviewStore.shared(catalog_dir / "previews.db")

    # -- overview ----------------------------------------------------------

    @app.get("/api/summary")
    def summary():
        c = cat()
        return {**c.summary(), "catalog_dir": str(catalog_dir),
                "unfinished_scans": [dict(volume_id=s["volume_id"], label=s["label"], subpath=s["root_subpath"],
                                          state=s["state"], message=s["message"]) for s in c.unfinished_scans()]}

    @app.get("/api/volumes")
    def list_volumes(refresh: bool = False):
        c = cat()
        up = online.get(force=refresh)
        out = []
        for v in c.list_volumes():
            out.append({
                "id": v["id"], "label": v["label"], "notes": v["notes"], "read_only": bool(v["read_only"]),
                "total_bytes": v["total_bytes"], "online": v["id"] in up,
                "root": str(up[v["id"]]) if v["id"] in up else None,
                "last_seen_at": v["last_seen_at"], "last_seen_path": v["last_seen_path"],
                "fingerprints": sorted(f"{k} {val}" for k, val in c.fingerprints(v["id"])),
                "stats": c.volume_stats(v["id"]),
            })
        return out

    @app.post("/api/volumes")
    def add_volume(req: AddVolume):
        c = cat()
        path = Path(req.path)
        if not path.is_dir():
            raise HTTPException(400, f"{path} is not a folder")
        try:
            if req.read_only:
                ref = volumes.register_read_only(c, path, req.label)
                volume_id = ref.id
            else:
                existing = volumes.read_marker(path)
                label = req.label or (existing.label if existing else None) or path.resolve().name or str(path)
                marker = volumes.init_volume(path, label, c.machine_id)
                c.upsert_volume(marker.id, marker.label, None, volumes.total_bytes(marker.root))
                c.mark_volume_seen(marker.id, str(marker.root))
                volume_id = marker.id
        except volumes.VolumeError as e:
            hint = "" if req.read_only or os.access(path, os.W_OK) else \
                " The drive is not writable; choose 'never write to this drive'."
            raise HTTPException(400, str(e) + hint)
        online.invalidate()
        return {"id": volume_id}

    @app.patch("/api/volumes/{volume_id}")
    def edit_volume(volume_id: str, req: EditVolume):
        c = cat()
        if c.get_volume(volume_id) is None:
            raise HTTPException(404, "no such volume")
        if req.label is not None and req.label.strip():
            c.conn.execute("UPDATE volumes SET label=? WHERE id=?", (req.label.strip(), volume_id))
        if req.notes is not None:
            c.conn.execute("UPDATE volumes SET notes=? WHERE id=?", (req.notes, volume_id))
        return {"ok": True}

    # -- scanning ----------------------------------------------------------

    @app.post("/api/scan")
    def start_scan(req: ScanRequest):
        c = cat()
        wanted: list[tuple[str, str]] = [(vid, "") for vid in req.volume_ids]
        if req.path:
            found = volumes.resolve_path(c, Path(req.path))
            if found is None:
                raise HTTPException(400, f"{req.path} is not on a registered drive")
            ref, root = found
            sub = Path(req.path).resolve().relative_to(root).as_posix()
            wanted.append((ref.id, "" if sub == "." else sub))
        if req.all_online:
            wanted += [(vid, "") for vid in online.get(force=True)]
        if req.resume:
            wanted += [(s["volume_id"], s["root_subpath"]) for s in c.unfinished_scans()]
        if not wanted:
            raise HTTPException(400, "nothing to scan")
        scans.start(wanted)
        return scans.state()

    @app.post("/api/scan/pause")
    def pause_scan():
        scans.pause()
        return scans.state()

    @app.get("/api/scan")
    def scan_state():
        return scans.state()

    @app.get("/api/events")
    async def events():
        """Server-sent events: scan progress, pushed whenever it changes.

        Starlette cancels this generator when the client goes away.
        """
        async def stream():
            yield "retry: 2000\n\n"
            last, last_sent = None, 0.0
            while True:
                state = json.dumps(scans.state())
                now = time.monotonic()
                if state != last:
                    yield f"event: scan\ndata: {state}\n\n"
                    last, last_sent = state, now
                elif now - last_sent > 15:
                    yield ": keep-alive\n\n"
                    last_sent = now
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- library -----------------------------------------------------------

    def file_filter(volume: str | None, folder: str, recursive: bool, status: str | None, kind: str | None,
                    camera: str | None, year: str | None, q: str | None, unique: bool) -> queries.FileFilter:
        return queries.FileFilter(volume_id=volume or None, folder=folder, recursive=recursive,
                                  status=status or None, kind=kind or None, camera=camera or None,
                                  year=year or None, text=q or None, unique=unique)

    @app.get("/api/files")
    def list_files(volume: str | None = None, folder: str = "", recursive: bool = True,
                   status: str | None = None, kind: str | None = None, camera: str | None = None,
                   year: str | None = None, q: str | None = None, unique: bool = False,
                   sort: str = "path", cursor: str | None = None, limit: int = Query(200, le=1000)):
        flt = file_filter(volume, folder, recursive, status, kind, camera, year, q, unique)
        return queries.list_files(cat(), flt, sort, cursor, limit)

    @app.get("/api/files/count")
    def count_files(volume: str | None = None, folder: str = "", recursive: bool = True,
                    status: str | None = None, kind: str | None = None, camera: str | None = None,
                    year: str | None = None, q: str | None = None, unique: bool = False):
        flt = file_filter(volume, folder, recursive, status, kind, camera, year, q, unique)
        return queries.count_files(cat(), flt)

    @app.get("/api/folders")
    def list_folders(volume: str, folder: str = ""):
        return queries.folders(cat(), volume, folder)

    @app.get("/api/facets")
    def facets():
        return queries.facets(cat())

    def parse_hash(hex_hash: str) -> bytes:
        try:
            h = bytes.fromhex(hex_hash)
        except ValueError:
            raise HTTPException(400, "bad hash")
        if len(h) != 32:
            raise HTTPException(400, "bad hash")
        return h

    @app.get("/api/contents/{hex_hash}")
    def content(hex_hash: str):
        c = cat()
        h = parse_hash(hex_hash)
        detail = queries.content_detail(c, h)
        if detail is None:
            raise HTTPException(404, "unknown content")
        up = online.get()
        for loc in detail["locations"]:
            loc["online"] = loc["volume_id"] in up
        similar = []
        if detail["phash"] is not None:
            matches = phash_index.similar(c, detail["phash"], max_distance=8, limit=24, exclude=h)
            briefs = queries.contents_brief(c, [m[0] for m in matches])
            similar = [{**briefs[m[0]], "distance": m[1]} for m in matches if m[0] in briefs]
        detail["similar"] = similar
        del detail["phash"]
        return detail

    @app.get("/api/preview/{hex_hash}")
    def preview(hex_hash: str, size: str = "s"):
        h = parse_hash(hex_hash)
        data = previews().get(h, "m" if size == "m" else "s")
        if data is None and size == "m":
            data = previews().get(h, "s")  # medium previews are optional
        if data is None:
            raise HTTPException(404, "no preview")
        return Response(data, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/api/files/{file_id}/original")
    def original(file_id: int):
        c = cat()
        row = c.conn.execute("SELECT volume_id, rel_path, rel_path_raw FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such file")
        mime = BROWSER_FORMATS.get(formats.extension(row["rel_path"]))
        if mime is None:
            raise HTTPException(415, "the browser cannot display this format; use the preview")
        root = online.get().get(row["volume_id"])
        if root is None:
            raise HTTPException(409, "the drive holding this file is not connected")
        path = discover.to_os_path(root, row["rel_path"], row["rel_path_raw"])
        if not os.path.isfile(path):
            raise HTTPException(404, "file not found on the drive")
        return FileResponse(path, media_type=mime)

    # -- "where is this image?" ------------------------------------------------

    @app.post("/api/where")
    def where(file: UploadFile = File(...), distance: int = 8):
        c = cat()
        suffix = "." + formats.extension(file.filename or "") if file.filename else ""
        with tempfile.NamedTemporaryFile(suffix=suffix, dir=catalog_dir, delete=False) as tmp:
            while chunk := file.file.read(8 << 20):
                tmp.write(chunk)
            tmp_path = Path(tmp.name)
        try:
            h = hash_file(tmp_path)
            matches: list[tuple[bytes, int, bool]] = []
            if c.conn.execute("SELECT 1 FROM contents WHERE hash=?", (h,)).fetchone():
                matches.append((h, 0, True))
            kind = formats.kind_for(tmp_path.name)
            if kind is not None:
                result = analyze.analyze(analyze.Job(kind=kind, ext=formats.extension(tmp_path.name),
                                                     size=tmp_path.stat().st_size, path=str(tmp_path),
                                                     make_medium=False))
                if result.phash is not None:
                    matches += [(mh, d, False) for mh, d in
                                phash_index.similar(c, result.phash, max_distance=distance, limit=40, exclude=h)]
            elif not matches:
                raise HTTPException(400, "unsupported file type")
        finally:
            tmp_path.unlink(missing_ok=True)
        up = online.get()
        briefs = queries.contents_brief(c, [m[0] for m in matches])
        out = []
        for mh, d, exact in matches:
            detail = queries.content_detail(c, mh)
            if detail is None or mh not in briefs:
                continue
            locs = [{**loc, "online": loc["volume_id"] in up} for loc in detail["locations"]]
            out.append({**briefs[mh], "exact": exact, "distance": d, "locations": locs})
        return {"matches": out}

    # -- filesystem browsing (to pick drives and folders) ------------------------

    @app.get("/api/fs")
    def browse(path: str | None = None):
        if not path:
            roots = [str(p) for p in volumes.candidate_mount_roots() if os.path.isdir(p)]
            home = str(Path.home())
            return {"path": None, "parent": None, "dirs": [{"name": r, "path": r} for r in roots + [home]],
                    "volume": None}
        p = Path(path)
        try:
            entries = sorted((e for e in os.scandir(p) if e.is_dir(follow_symlinks=False)),
                             key=lambda e: e.name.lower())
        except OSError as e:
            raise HTTPException(400, f"cannot open {p}: {e.strerror or e}")
        found = volumes.resolve_path(cat(), p, adopt=False)  # browsing must not change the catalog
        volume = {"id": found[0].id, "label": found[0].label, "root": str(found[1])} if found else None
        parent = str(p.parent) if p.parent != p else None
        return {"path": str(p), "parent": parent, "volume": volume,
                "dirs": [{"name": e.name, "path": e.path} for e in entries[:2000]]}

    # -- GUI ------------------------------------------------------------------

    if (UI_DIR / "index.html").is_file():
        app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
    else:
        @app.get("/")
        def ui_missing():
            return Response("<h1>imagelib</h1><p>The GUI has not been built. Run <code>npm run build</code> in "
                            "<code>web/</code>.</p>", media_type="text/html")

    return app
