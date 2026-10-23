"""HTTP API and static GUI, served on 127.0.0.1 only.

Security: the API can list folders, stream photos and move or delete files.
* Requests are classified by ``remote.classify``: local (this machine), remote
  (through Tailscale or Cloudflare, see remote.py) or forbidden (anything else,
  including DNS-rebinding attempts with foreign Host headers).
* Remote requests need the tunnel's identity *and* an app session; remote-access
  settings and the password can only be changed locally.
* Every state-changing request needs a custom header, which a cross-site page cannot
  send without a CORS preflight (never granted); session cookies are SameSite=Strict.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analyze, discover, formats, merges, organize, plans, queries, remote, review, sync, trees, volumes
from .catalog import Catalog
from .jobs import (Busy, ExportManager, MergeManager, OnlineVolumes, PlanManager, RelateManager, ScanManager,
                   SyncManager)
from .previews import PreviewStore
from .search import hash_file

UI_DIR = Path(__file__).parent / "ui"
CSRF_HEADER = "x-imagelib"
# Reachable without an app session when remote (the login screen needs them).
REMOTE_PUBLIC = ("/api/auth/status", "/api/auth/login")
# Never reachable remotely, even signed in.
LOCAL_ONLY = ("/api/remote",)
BROWSER_FORMATS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "jpe": "image/jpeg", "jfif": "image/jpeg",
                   "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}


class AccessControl:
    """Pure ASGI middleware (BaseHTTPMiddleware interferes with streaming responses)."""

    def __init__(self, app, access: "AccessState"):
        self.app = app
        self.access = access

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        headers = {k.lower(): v for k, v in request.headers.items()}
        verdict = remote.classify(headers, request.url.hostname, self.access.settings(), self.access.verifier)
        scope.setdefault("state", {})["access"] = verdict
        path = request.url.path
        problem, status = None, 403
        if verdict.kind == "forbidden":
            problem = verdict.problem
        elif path.startswith("/api/sync/"):
            # Agents: their token opens the sync endpoints and nothing else.
            auth = request.headers.get("authorization", "")
            agent = sync.agent_for_token(self.access.catalog(), auth[7:] if auth.lower().startswith("bearer ") else None)
            if agent is None:
                problem, status = "invalid or revoked agent token", 401
            else:
                scope["state"]["agent"] = dict(agent)
        elif request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
            problem = f"missing {CSRF_HEADER} header"
        elif verdict.kind == "remote" and path.startswith("/api/") and not path.startswith("/api/sync/"):
            if path.startswith(LOCAL_ONLY):
                problem = "this can only be changed on the computer running the app"
            elif not path.startswith(REMOTE_PUBLIC) and not await asyncio.to_thread(
                    remote.valid_session, self.access.catalog(), request.cookies.get(remote.SESSION_COOKIE)):
                problem, status = "login required", 401
        if problem:
            if verdict.kind == "forbidden" and not path.startswith("/api/") \
                    and "text/html" in request.headers.get("accept", ""):
                # Someone opened the address in a browser: explain, in a page.
                page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                        f"<title>Image Library</title><body style='font:16px system-ui;max-width:34em;margin:4em auto;"
                        f"padding:0 1em'><h1 style='font-size:20px'>Image Library</h1><p>{html.escape(problem)}</p>")
                await Response(page, status_code=status, media_type="text/html")(scope, receive, send)
                return
            body = {"detail": problem, "login": status == 401}
            await JSONResponse(body, status_code=status)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class AccessState:
    """Settings for access control, cached between requests and reloaded after changes."""

    def __init__(self, catalog_dir: Path):
        self.catalog_dir = catalog_dir
        self.verifier = remote.AccessVerifier()
        self.throttle = remote.LoginThrottle()
        self._settings: remote.RemoteSettings | None = None

    def catalog(self) -> Catalog:
        return Catalog.shared(self.catalog_dir)

    def settings(self) -> remote.RemoteSettings:
        if self._settings is None:
            self._settings = remote.load_settings(self.catalog())
        return self._settings

    def invalidate(self) -> None:
        self._settings = None


class LoginRequest(BaseModel):
    password: str


class PasswordRequest(BaseModel):
    password: str


class RemoteUpdate(BaseModel):
    tailscale_enabled: bool | None = None
    tailscale_hosts: list[str] | None = None
    tailscale_users: list[str] | None = None
    cloudflare_enabled: bool | None = None
    cloudflare_hosts: list[str] | None = None
    cloudflare_team: str | None = None
    cloudflare_aud: str | None = None
    cloudflare_tunnel_token: str | None = None


class AddVolume(BaseModel):
    path: str
    label: str | None = None
    read_only: bool = False


class EditVolume(BaseModel):
    label: str | None = None
    notes: str | None = None


class MergeRequest(BaseModel):
    keep_volume: str
    keep_root: str
    drop_volume: str
    drop_root: str


class DecisionRequest(BaseModel):
    file_ids: list[int]
    decision: str | None          # keep | discard | None to clear
    reason: str = "manual"


class PlanRun(BaseModel):
    verify_hash: bool = True
    require_connected_copy: bool = True


class PlanRestore(BaseModel):
    item_ids: list[int] | None = None


class PlanPurge(BaseModel):
    confirm: str


class AgentCreate(BaseModel):
    name: str


class HubUpdate(BaseModel):
    url: str | None = None
    token: str | None = None
    cf_client_id: str | None = None
    cf_client_secret: str | None = None


class OrganizeRequest(BaseModel):
    target: str
    include_videos: bool = True
    include_raw: bool = True
    include_damaged: bool = False
    mode: str = Field("copy", pattern="^(copy|move)$")


class HashList(BaseModel):
    hashes: list[str]


class ScanRequest(BaseModel):
    volume_ids: list[str] = []
    path: str | None = None        # a folder on a registered volume
    all_online: bool = False
    resume: bool = False           # unfinished scans on connected volumes


def create_app(catalog_dir: Path, workers: int | None = None, auto_relate: bool = True,
               port: int = 8765) -> FastAPI:
    catalog_dir = Path(catalog_dir)
    Catalog.shared(catalog_dir)  # create / migrate up front
    online = OnlineVolumes(catalog_dir)
    relater = RelateManager(catalog_dir)
    scans = ScanManager(catalog_dir, online, workers=workers, relate_manager=relater)
    plan_runner = PlanManager(catalog_dir, online, relater)
    plan_runner.scans, scans.plans = scans, plan_runner
    exporter = ExportManager(catalog_dir, online)
    exporter.scans, exporter.plans, scans.exports = scans, plan_runner, exporter
    merger = MergeManager(catalog_dir, online)
    merger.others = [scans, plan_runner, exporter]
    scans.merges = plan_runner.merges = exporter.merges = merger
    syncer = SyncManager(catalog_dir)
    syncer.scans, syncer.plans, scans.sync = scans, plan_runner, syncer
    access = AccessState(catalog_dir)
    tunnel = remote.CloudflaredProcess()
    phash_index = queries.PhashIndex()
    facet_index = queries.FacetIndex()
    tree_index = trees.TreeIndex()

    @asynccontextmanager
    async def lifespan(app):
        if auto_relate:
            relater.start()  # catches up after scans made by the command line; no-op when current
            syncer.start_background()
        s = access.settings()
        if s.cloudflare_enabled and s.cloudflare_tunnel_token and tunnel.binary():
            try:
                tunnel.start(s.cloudflare_tunnel_token)
            except RuntimeError:
                pass
        yield
        tunnel.stop()
        syncer.stop()
        relater.stop(wait=False)
        plan_runner.stop()
        exporter.stop()
        merger.stop()
        await asyncio.to_thread(scans.shutdown)

    app = FastAPI(title="imagelib", lifespan=lifespan)
    app.state.scans = scans
    app.state.access = access
    app.add_middleware(AccessControl, access=access)

    def cat() -> Catalog:
        return Catalog.shared(catalog_dir)

    def previews() -> PreviewStore:
        return PreviewStore.shared(catalog_dir / "previews.db")

    # -- overview ----------------------------------------------------------

    storage_cache: dict = {}
    summary_cache: dict = {}
    DUP_KEYS = ("duplicate_groups", "redundant_copies", "redundant_bytes")

    def busy() -> bool:
        """A scan, plan or merge is changing files right now."""
        return scans.running or plan_runner.running or merger.running

    @app.get("/api/storage")
    def storage():
        # Counting previews scans their table: refresh that part at most once a minute.
        now = time.monotonic()
        stale = "at" not in storage_cache or now - storage_cache["at"] > 60
        fresh = cat().storage(count_previews=stale)
        if fresh["preview_counts"] is None:
            fresh["preview_counts"] = storage_cache.get("counts")
        else:
            storage_cache.update(at=now, counts=fresh["preview_counts"])
        return fresh

    @app.get("/api/summary")
    def summary():
        c = cat()
        # Counting duplicates groups every file; while a scan or plan runs it is refreshed
        # at most every 30 s rather than on every poll of the Overview.
        now = time.monotonic()
        recount = not busy() or "dup" not in summary_cache or now - summary_cache["at"] > 30
        base = c.summary(include_duplicates=recount)
        if recount:
            summary_cache.update(at=now, dup={k: base[k] for k in DUP_KEYS})
        return {**base, **summary_cache["dup"], "catalog_dir": str(catalog_dir),
                "unfinished_scans": [dict(volume_id=s["volume_id"], label=s["label"], subpath=s["root_subpath"],
                                          state=s["state"], message=s["message"]) for s in c.unfinished_scans()]}

    @app.get("/api/volumes")
    def list_volumes(refresh: bool = False):
        c = cat()
        up = online.get(force=refresh)
        at_agents = sync.agent_presence(c)
        out = []
        for v in c.list_volumes():
            out.append({
                "id": v["id"], "label": v["label"], "notes": v["notes"], "read_only": bool(v["read_only"]),
                "total_bytes": v["total_bytes"], "online": v["id"] in up,
                "agent": at_agents.get(v["id"]) if v["id"] not in up else None,
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
        try:
            scans.start(wanted)
        except Busy as e:
            raise HTTPException(409, str(e))
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

    def file_filter(volume: str | None = None, folder: str = "", recursive: bool = True,
                    status: str | None = None, kind: str | None = None, camera: str | None = None,
                    year: str | None = None, q: str | None = None, unique: bool = False,
                    month: str | None = Query(None, pattern=r"^(0?[1-9]|1[0-2]|unknown)$"),
                    size: str | None = Query(None, pattern="^(" + "|".join([*queries.SIZES, queries.UNKNOWN]) + ")$"),
                    ) -> queries.FileFilter:
        """The library's filters, shared by the listing, its count and the filter counts."""
        return queries.FileFilter(volume_id=volume or None, folder=folder, recursive=recursive,
                                  status=status or None, kind=kind or None, camera=camera or None,
                                  year=_valid_year(year), month=month or None, text=q or None, unique=unique,
                                  size=size or None)

    @app.get("/api/files")
    def list_files(flt: queries.FileFilter = Depends(file_filter), sort: str = "path",
                   cursor: str | None = None, limit: int = Query(200, le=1000)):
        return queries.list_files(cat(), flt, sort, cursor, limit)

    @app.get("/api/files/count")
    def count_files(flt: queries.FileFilter = Depends(file_filter)):
        return queries.count_files(cat(), flt)

    @app.get("/api/folders")
    def list_folders(volume: str, flt: queries.FileFilter = Depends(file_filter)):
        return queries.folders(cat(), volume, flt.folder, flt, facet_index)

    @app.get("/api/facets")
    def facets(flt: queries.FileFilter = Depends(file_filter)):
        return facet_index.facets(cat(), flt)

    def _valid_year(year: str | None) -> str | None:
        if year and year != queries.UNKNOWN and not (len(year) == 4 and year.isdigit()):
            raise HTTPException(422, "year must be like 2004, or 'unknown'")
        return year or None

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
        decided = review.file_decisions(c, [loc["file_id"] for loc in detail["locations"]])
        for loc in detail["locations"]:
            loc["online"] = loc["volume_id"] in up
            loc["decision"] = decided.get(loc["file_id"])
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

    # -- review & decisions ------------------------------------------------------

    def refused(e: review.Refused):
        return JSONResponse({"detail": str(e), "refused": e.details}, status_code=409)

    @app.post("/api/decisions")
    def set_decisions(req: DecisionRequest):
        try:
            return review.decide(cat(), req.file_ids, req.decision, req.reason)
        except review.Refused as e:
            return refused(e)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/decisions/summary")
    def decisions_summary():
        return review.summary(cat())

    @app.get("/api/decisions")
    def decisions_list(decision: str | None = None, after: int = 0, limit: int = Query(200, le=1000)):
        return review.list_decisions(cat(), decision, after, limit)

    @app.get("/api/review/problems")
    def review_problems(status: str | None = None, volume: str | None = None, undecided: bool = False,
                        after: int = 0, limit: int = Query(200, le=1000)):
        return review.problems(cat(), status or None, volume or None, undecided, after, limit)

    @app.get("/api/review/intact/{hex_hash}")
    def review_intact(hex_hash: str):
        return review.intact_versions(cat(), previews(), phash_index, parse_hash(hex_hash))

    @app.get("/api/review/duplicates")
    def review_duplicates(scope: str = "any", volume: str | None = None, undecided: bool = False,
                          offset: int = 0, limit: int = Query(100, le=500)):
        return review.duplicate_groups(cat(), scope, volume or None, undecided, offset, limit, busy=busy())

    @app.get("/api/review/duplicates/{hex_hash}")
    def review_duplicate(hex_hash: str):
        return review.duplicate_group(cat(), parse_hash(hex_hash), set(online.get()))

    @app.post("/api/review/duplicates/apply-suggestions")
    def review_apply(req: HashList):
        try:
            return review.apply_suggestions(cat(), [parse_hash(h) for h in req.hashes])
        except review.Refused as e:
            return refused(e)

    @app.get("/api/review/trees")
    def review_trees(volume: str | None = None, sort: str = "shared", offset: int = 0,
                     limit: int = Query(50, le=500)):
        return tree_index.list(cat(), volume or None, sort, offset, limit)

    @app.get("/api/review/trees/children")
    def review_tree_children(a_volume: str, a_root: str, b_volume: str, b_root: str):
        return trees.children(cat().conn, a_volume, a_root, b_volume, b_root)

    # -- relations: thumbnails & similar images ---------------------------------

    @app.post("/api/relate")
    def start_relate():
        if not scans.running:
            relater.start()
        return relater.state()

    @app.get("/api/review/thumbnails")
    def review_thumbnails(cls: str | None = None, kind: str | None = None, undecided: bool = False,
                          offset: int = 0, limit: int = Query(100, le=500)):
        return review.thumbnails(cat(), cls or None, kind or None, undecided, offset, limit)

    @app.get("/api/review/thumbnails/{hex_hash}")
    def review_thumbnail(hex_hash: str):
        detail = review.thumbnail_detail(cat(), parse_hash(hex_hash), set(online.get()))
        if detail is None:
            raise HTTPException(404, "not a thumbnail or reduced copy")
        return detail

    @app.post("/api/review/thumbnails/discard-verified")
    def review_discard_thumbnails(kind: str | None = "thumbnail"):
        try:
            return review.discard_verified_thumbnails(cat(), kind or None)
        except review.Refused as e:
            return refused(e)

    @app.get("/api/review/similar")
    def review_similar(undecided: bool = False, offset: int = 0, limit: int = Query(100, le=500)):
        return review.similar_clusters(cat(), undecided, offset, limit)

    @app.get("/api/review/similar/{cluster}")
    def review_similar_cluster(cluster: int):
        return review.similar_cluster(cat(), cluster, set(online.get()))

    @app.post("/api/review/similar/{cluster}/keep/{hex_hash}")
    def review_similar_keep(cluster: int, hex_hash: str):
        try:
            return review.keep_cluster_member(cat(), cluster, parse_hash(hex_hash))
        except review.Refused as e:
            return refused(e)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # -- sign-in (remote devices) ---------------------------------------------------

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        v: remote.Verdict = request.scope["state"]["access"]
        c = cat()
        return {"local": v.kind == "local", "via": v.via, "identity": v.identity,
                "signed_in": v.kind == "local" or remote.valid_session(c, request.cookies.get(remote.SESSION_COOKIE)),
                "password_set": remote.has_password(c)}

    @app.post("/api/auth/login")
    async def auth_login(request: Request, req: LoginRequest):
        v: remote.Verdict = request.scope["state"]["access"]
        if v.kind == "local":
            return {"signed_in": True}
        client = remote.client_label({k.lower(): x for k, x in request.headers.items()}, v) or "unknown"
        delay = access.throttle.delay(client)
        if delay:
            await asyncio.sleep(delay)
        c = cat()
        if not await asyncio.to_thread(remote.check_password, c, req.password):
            access.throttle.failed(client)
            raise HTTPException(401, "wrong password")
        access.throttle.succeeded(client)
        token = remote.create_session(c, v.via, client, request.headers.get("user-agent", ""))
        response = JSONResponse({"signed_in": True})
        response.set_cookie(remote.SESSION_COOKIE, token, max_age=remote.SESSION_DAYS * 86400,
                            httponly=True, secure=True, samesite="strict", path="/")
        return response

    @app.post("/api/auth/logout")
    def auth_logout(request: Request):
        remote.end_session(cat(), request.cookies.get(remote.SESSION_COOKIE))
        response = JSONResponse({"signed_in": False})
        response.delete_cookie(remote.SESSION_COOKIE, path="/")
        return response

    # -- remote access settings (local only; enforced by AccessControl) -------------

    def remote_state() -> dict:
        c = cat()
        return {"settings": access.settings().public(), "password_set": remote.has_password(c),
                "sessions": remote.list_sessions(c), "tailscale": remote.tailscale_status(),
                "tailscale_command": remote.tailscale_serve_command(port), "cloudflared": tunnel.status(),
                "port": port}

    @app.get("/api/remote")
    def get_remote():
        return remote_state()

    @app.put("/api/remote")
    def put_remote(req: RemoteUpdate):
        c = cat()
        s = remote.load_settings(c)
        for key, value in req.model_dump(exclude_none=True).items():
            setattr(s, key, value)
        if s.any_enabled and not remote.has_password(c):
            raise HTTPException(400, "set a password before turning on remote access")
        if s.tailscale_enabled and not s.tailscale_hosts:
            raise HTTPException(400, "enter this computer's Tailscale name (or use Detect)")
        if s.cloudflare_enabled and not (s.cloudflare_hosts and s.cloudflare_team and s.cloudflare_aud):
            raise HTTPException(400, "Cloudflare needs the public hostname, team name and application AUD tag")
        remote.save_settings(c, s)
        access.invalidate()
        if not s.cloudflare_enabled:
            tunnel.stop()
        return remote_state()

    @app.post("/api/remote/password")
    def set_remote_password(req: PasswordRequest):
        try:
            remote.set_password(cat(), req.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return remote_state()

    @app.post("/api/remote/sessions/{session_id}/revoke")
    def revoke_remote_session(session_id: str):
        remote.revoke_session(cat(), session_id)
        return remote_state()

    @app.post("/api/remote/cloudflared/{action}")
    def cloudflared_action(action: str):
        if action == "start":
            try:
                tunnel.start(access.settings().cloudflare_tunnel_token)
            except RuntimeError as e:
                raise HTTPException(400, str(e))
        elif action == "stop":
            tunnel.stop()
        else:
            raise HTTPException(404, "unknown action")
        return remote_state()

    # -- hub & agents ------------------------------------------------------------

    @app.post("/api/sync/exchange")
    def sync_exchange(request: Request, payload: dict):
        agent = request.scope["state"]["agent"]
        payload["_agent_id"] = agent["id"]
        c = cat()
        result = sync.merge(c, payload, previews())
        if not payload.get("more"):
            result["actions"] = sync.actions_for(c, payload["agent"]["machine_id"], online.get())
        return result

    @app.post("/api/sync/previews")
    def sync_previews(body: dict):
        return {"stored": sync.store_previews(previews(), body.get("previews", []))}

    @app.get("/api/remote/agents")
    def get_agents():
        return sync.list_agents(cat())

    @app.post("/api/remote/agents")
    def create_agent(req: AgentCreate):
        return sync.add_agent(cat(), req.name)

    @app.post("/api/remote/agents/{agent_id}/revoke")
    def revoke_agent(agent_id: str):
        sync.revoke_agent(cat(), agent_id)
        return sync.list_agents(cat())

    @app.get("/api/remote/hub")
    def get_hub():
        return syncer.state()

    @app.put("/api/remote/hub")
    def put_hub(req: HubUpdate):
        c = cat()
        cfg = sync.load_hub(c)
        for key, value in req.model_dump(exclude_none=True).items():
            setattr(cfg, key, value.strip())
        if req.url is not None and req.url and not req.url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise HTTPException(400, "use the hub's https:// address (Tailscale or Cloudflare)")
        sync.save_hub(c, cfg)
        return syncer.state()

    @app.post("/api/remote/hub/sync")
    async def hub_sync_now():
        await asyncio.to_thread(syncer.run_once)
        return syncer.state()

    @app.get("/api/remote/bundle/export")
    def bundle_export(full: bool = False):
        out = Path(tempfile.mkstemp(suffix=".imagelib-bundle", dir=catalog_dir)[1])
        sync.export_bundle(cat(), previews(), out, full=full)
        from starlette.background import BackgroundTask

        name = f"imagelib-{time.strftime('%Y%m%d-%H%M')}.imagelib-bundle"
        return FileResponse(out, media_type="application/octet-stream", filename=name,
                            background=BackgroundTask(out.unlink, missing_ok=True))

    @app.post("/api/remote/bundle/import")
    def bundle_import(file: UploadFile = File(...)):
        with tempfile.NamedTemporaryFile(suffix=".imagelib-bundle", dir=catalog_dir, delete=False) as tmp:
            while chunk := file.file.read(8 << 20):
                tmp.write(chunk)
            path = Path(tmp.name)
        try:
            return sync.import_bundle(cat(), previews(), path)
        except (ValueError, sqlite3.DatabaseError) as e:
            raise HTTPException(400, f"not a valid bundle: {e}")
        finally:
            path.unlink(missing_ok=True)

    # -- organize ----------------------------------------------------------------

    @app.post("/api/organize")
    def organize_create(req: OrganizeRequest):
        c = cat()
        target = Path(req.target)
        if not target.is_absolute():
            raise HTTPException(400, "choose a folder")
        probe = next((p for p in [target, *target.parents] if p.exists()), target)
        found = volumes.resolve_path(c, probe, adopt=False)
        if found and found[0].read_only:
            raise HTTPException(400, f"'{found[0].label}' is registered read-only; choose another drive")
        opts = organize.Options(req.include_videos, req.include_raw, req.include_damaged, req.mode)
        target_volume = target_rel = None
        if found:
            target_volume = found[0].id
            rel = target.resolve().relative_to(found[1].resolve()).as_posix()
            target_rel = "" if rel == "." else rel
        try:
            return organize.create(c, target, opts, target_volume, target_rel)
        except organize.Refused as e:
            raise HTTPException(400, str(e))

    @app.get("/api/organize/{export_id}")
    def organize_summary(export_id: int):
        try:
            return organize.summary(cat(), export_id, online.get())
        except KeyError:
            raise HTTPException(404, "no such library")

    @app.get("/api/organize/{export_id}/browse")
    def organize_browse(export_id: int, folder: str = "", offset: int = 0, limit: int = Query(200, le=1000)):
        return organize.browse(cat(), export_id, folder, offset, limit)

    def organize_action(export_id: int, action) -> dict:
        if exporter.running:
            raise HTTPException(409, "the library is being organized right now; pause it first")
        try:
            return action(cat(), export_id, online.get(force=True))
        except organize.Refused as e:
            raise HTTPException(409, str(e))

    @app.post("/api/organize/{export_id}/undo")
    def organize_undo(export_id: int):
        return organize_action(export_id, organize.undo)

    @app.post("/api/organize/{export_id}/purge")
    def organize_purge(export_id: int):
        return organize_action(export_id, organize.purge)

    @app.delete("/api/organize/{export_id}")
    def organize_discard(export_id: int):
        try:
            organize.discard(cat(), export_id)
        except organize.Refused as e:
            raise HTTPException(409, str(e))
        return {"discarded": export_id}

    @app.get("/api/organize")
    def organize_list():
        return organize.list_exports(cat())

    @app.post("/api/organize/{export_id}/run")
    def organize_run(export_id: int):
        try:
            exporter.start(export_id)
        except Busy as e:
            raise HTTPException(409, str(e))
        return exporter.state()

    @app.post("/api/organize/{export_id}/pause")
    def organize_pause(export_id: int):
        exporter.stop()
        return exporter.state()

    # -- merging folder trees ----------------------------------------------------

    def merge_refused(e: merges.MergeRefused):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.post("/api/merges")
    def prepare_merge(req: MergeRequest):
        try:
            return merges.prepare(cat(), req.keep_volume, req.keep_root, req.drop_volume, req.drop_root)
        except merges.MergeRefused as e:
            return merge_refused(e)

    @app.get("/api/merges")
    def list_merges():
        return merges.list_merges(cat())

    def merge_or_404(merge_id: int) -> dict:
        try:
            d = merges.detail(cat(), merge_id)
        except KeyError:
            raise HTTPException(404, "no such merge")
        p = merger.state()
        d["progress"] = p if p and p["merge_id"] == merge_id else None
        return d

    @app.get("/api/merges/{merge_id}")
    def merge_detail(merge_id: int):
        return merge_or_404(merge_id)

    @app.post("/api/merges/{merge_id}/run")
    def run_merge(merge_id: int):
        merge_or_404(merge_id)
        try:
            merger.start(merge_id)
        except Busy as e:
            raise HTTPException(409, str(e))
        return merge_or_404(merge_id)

    @app.post("/api/merges/{merge_id}/pause")
    def pause_merge(merge_id: int):
        merger.stop()
        return merge_or_404(merge_id)

    def merge_action(merge_id: int, action) -> dict:
        merge_or_404(merge_id)
        if merger.running:
            raise HTTPException(409, "a merge is running; wait for it to finish")
        try:
            return {**action(cat(), merge_id, online.get(force=True)), "merge": merge_or_404(merge_id)}
        except merges.MergeRefused as e:
            return merge_refused(e)

    @app.post("/api/merges/{merge_id}/undo")
    def undo_merge(merge_id: int):
        return merge_action(merge_id, merges.undo)

    @app.post("/api/merges/{merge_id}/purge")
    def purge_merge(merge_id: int):
        return merge_action(merge_id, merges.purge)

    @app.delete("/api/merges/{merge_id}")
    def cancel_merge(merge_id: int):
        merge_or_404(merge_id)
        try:
            merges.cancel(cat(), merge_id)
        except merges.MergeRefused as e:
            return merge_refused(e)
        return {"cancelled": merge_id}

    # -- action plans ------------------------------------------------------------

    def agent_relay() -> dict[str, str]:
        return {vid: p["machine_id"] for vid, p in sync.agent_presence(cat()).items()}

    @app.post("/api/plans")
    def create_plan():
        return plans.create(cat())

    @app.get("/api/plans")
    def list_plans():
        return plans.list_plans(cat())

    @app.get("/api/plans/{plan_id}")
    def plan_detail(plan_id: int):
        c = cat()
        d = plans.detail(c, plan_id, online.get())
        if d is None:
            raise HTTPException(404, "no such plan")
        at_agents = sync.agent_presence(c)
        for v in d["volumes"]:
            v["agent"] = (at_agents.get(v["volume_id"]) or {}).get("machine") if not v["online"] else None
        return d

    @app.get("/api/plans/{plan_id}/items")
    def plan_items(plan_id: int, status: str | None = None, volume: str | None = None, after: int = 0,
                   limit: int = Query(200, le=1000)):
        return plans.items(cat(), plan_id, status or None, volume or None, after, limit)

    @app.post("/api/plans/{plan_id}/run")
    def plan_run(plan_id: int, req: PlanRun):
        try:
            plan_runner.start(plan_id, plans.RunOptions(req.verify_hash, req.require_connected_copy))
        except Busy as e:
            raise HTTPException(409, str(e))
        return plan_runner.state()

    @app.post("/api/plans/{plan_id}/pause")
    def plan_pause(plan_id: int):
        plan_runner.stop()
        return plan_runner.state()

    @app.post("/api/plans/{plan_id}/restore")
    def plan_restore(plan_id: int, req: PlanRestore):
        if plan_runner.running:
            raise HTTPException(409, "wait for the running plan to finish")
        return plans.restore(cat(), plan_id, online.get(force=True), req.item_ids, relay=agent_relay())

    @app.post("/api/plans/{plan_id}/purge")
    def plan_purge(plan_id: int, req: PlanPurge):
        if req.confirm != "delete":
            raise HTTPException(400, 'type "delete" to confirm')
        if plan_runner.running:
            raise HTTPException(409, "wait for the running plan to finish")
        return plans.purge(cat(), plan_id, online.get(force=True), relay=agent_relay())

    @app.get("/api/plans/{plan_id}/manual-list/{volume_id}")
    def plan_manual_list(plan_id: int, volume_id: str):
        root = online.get().get(volume_id)
        text = plans.manual_list(cat(), plan_id, volume_id, str(root) if root else None)
        return Response(text, media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="imagelib-plan-{plan_id}-to-delete.txt"'})

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
