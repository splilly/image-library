"""Hub & agents: one catalog for drives spread over several machines.

Every machine runs the same app with its own catalog. An *agent* scans the drives
attached to it and sends what changed to the *hub*, where the library is browsed,
reviewed and planned. Rows are merged by identifiers that are the same everywhere —
volume UUID + path for files, content hash for analysis results — and when two
machines disagree about a file, the most recent change wins (``files.changed_at``).

Transport is the remote access already in place (Tailscale, or Cloudflare with a
service token) plus a per-agent token issued by the hub that opens only the sync
endpoints. Machines that never connect can carry the same data in a bundle file.

Plan items for drives that only an agent can reach are handed to that agent after
the hub's last-copy check; the agent re-checks the file itself, moves it into
quarantine with the same journaling as local plans, and reports back.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import plans
from .catalog import Catalog, bump_files_version
from .discover import mtime_equivalent, to_os_path
from .previews import PreviewStore
from .volumes import VolumeRef, locate_online, volume_ref

BATCH = 2000
PREVIEW_BATCH = 200
PRESENCE_FRESH = 15 * 60          # seconds an agent's "drive connected" report stays valid
CONTENT_COLUMNS = ("hash", "size", "kind", "format", "width", "height", "status", "problems", "taken_at",
                   "camera_make", "camera_model", "software", "orientation", "has_exif", "duration_s",
                   "phash", "dhash", "pixel_hash", "has_preview", "metadata_json", "analyzer_version", "analyzed_at")
BLOB_COLUMNS = ("hash", "pixel_hash")


def _b(data: bytes | None) -> str | None:
    return data.hex() if data is not None else None


def _unb(text: str | None) -> bytes | None:
    return bytes.fromhex(text) if text is not None else None


# -- agent tokens (hub side) -------------------------------------------------------------

def add_agent(cat: Catalog, name: str) -> dict:
    token = "ilag_" + secrets.token_urlsafe(32)
    agent_id = str(uuid.uuid4())
    cat.conn.execute("INSERT INTO agents(id, name, token_hash, created_at) VALUES(?,?,?,?)",
                     (agent_id, name.strip() or "agent", _hash(token), time.time()))
    return {"id": agent_id, "token": token}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def agent_for_token(cat: Catalog, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    return cat.conn.execute("SELECT * FROM agents WHERE token_hash = ? AND revoked = 0", (_hash(token),)).fetchone()


def list_agents(cat: Catalog) -> list[dict]:
    out = []
    for a in cat.conn.execute("SELECT a.*, m.name AS machine_name, m.os FROM agents a "
                              "LEFT JOIN machines m ON m.id = a.machine_id WHERE a.revoked = 0 ORDER BY a.name"):
        vols = [r[0] for r in cat.conn.execute(
            "SELECT v.label FROM volume_presence p JOIN volumes v ON v.id = p.volume_id "
            "WHERE p.machine_id = ? AND p.seen_at > ? ORDER BY v.label", (a["machine_id"], time.time() - PRESENCE_FRESH))]
        out.append({"id": a["id"], "name": a["name"], "created_at": a["created_at"], "last_sync_at": a["last_sync_at"],
                    "machine": a["machine_name"], "os": a["os"], "connected_volumes": vols})
    return out


def revoke_agent(cat: Catalog, agent_id: str) -> None:
    cat.conn.execute("UPDATE agents SET revoked = 1 WHERE id = ?", (agent_id,))


def agent_presence(cat: Catalog) -> dict[str, dict]:
    """volume id -> {machine, name, root} for drives an agent reported connected recently."""
    out = {}
    for r in cat.conn.execute(
            "SELECT p.volume_id, p.machine_id, p.root, m.name FROM volume_presence p "
            "LEFT JOIN machines m ON m.id = p.machine_id WHERE p.seen_at > ? AND p.machine_id != ?",
            (time.time() - PRESENCE_FRESH, cat.machine_id)):
        out[r[0]] = {"machine_id": r[1], "root": r[2], "machine": r[3]}
    return out


# -- building payloads (agent side) --------------------------------------------------------

@dataclass
class Cursor:
    files: tuple[float, int] = (0.0, 0)
    contents: float = 0.0


def _volumes_payload(cat: Catalog, online: dict[str, Path]) -> list[dict]:
    out = []
    for v in cat.list_volumes():
        out.append({"id": v["id"], "label": v["label"], "read_only": bool(v["read_only"]), "fs_type": v["fs_type"],
                    "total_bytes": v["total_bytes"], "online_root": str(online[v["id"]]) if v["id"] in online else None,
                    "fingerprints": sorted([k, val] for k, val in cat.fingerprints(v["id"]))})
    return out


def build_batch(cat: Catalog, cursor: Cursor, online: dict[str, Path]) -> tuple[dict, Cursor, bool]:
    """The next batch of changes after ``cursor``; returns (payload, new cursor, more to come)."""
    c = cat.conn
    after_t, after_id = cursor.files
    rows = c.execute(
        "SELECT * FROM files WHERE (changed_at, id) > (?, ?) ORDER BY changed_at, id LIMIT ?",
        (after_t, after_id, BATCH)).fetchall()
    contents = c.execute(f"SELECT {', '.join(CONTENT_COLUMNS)} FROM contents WHERE analyzed_at > ? "
                         f"ORDER BY analyzed_at LIMIT ?", (cursor.contents, BATCH)).fetchall()
    content_cursor = contents[-1]["analyzed_at"] if contents else cursor.contents
    # Files reference contents, so every content a file in this batch needs travels with it
    # (the two cursors move independently).
    have = {r["hash"] for r in contents}
    needed = list({r["content_hash"] for r in rows if r["content_hash"] is not None} - have)
    for i in range(0, len(needed), 500):
        chunk = needed[i:i + 500]
        contents += c.execute(f"SELECT {', '.join(CONTENT_COLUMNS)} FROM contents "
                              f"WHERE hash IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    files = [{"volume_id": r["volume_id"], "rel_path": r["rel_path"], "rel_path_raw": _b(r["rel_path_raw"]),
              "kind": r["kind"], "size": r["size"], "mtime_ns": r["mtime_ns"], "state": r["state"],
              "error": r["error"], "content_hash": _b(r["content_hash"]), "first_seen_at": r["first_seen_at"],
              "analyzed_at": r["analyzed_at"], "changed_at": r["changed_at"]} for r in rows]
    payload = {
        "agent": {"machine_id": cat.machine_id, **_machine(cat)},
        "volumes": _volumes_payload(cat, online),
        "files": files,
        "contents": [{k: (_b(r[k]) if k in BLOB_COLUMNS else r[k]) for k in CONTENT_COLUMNS} for r in contents],
    }
    new = Cursor((rows[-1]["changed_at"], rows[-1]["id"]) if rows else cursor.files, content_cursor)
    return payload, new, len(rows) == BATCH or len(have) == BATCH


def _machine(cat: Catalog) -> dict:
    row = cat.conn.execute("SELECT name, os FROM machines WHERE id = ?", (cat.machine_id,)).fetchone()
    return {"name": row["name"] if row else "agent", "os": row["os"] if row else ""}


# -- merging (hub side) ---------------------------------------------------------------------

_FILE_UPSERT = """
INSERT INTO files(volume_id, rel_path, rel_path_raw, path_key, kind, size, mtime_ns, state, error, content_hash,
                  first_seen_at, last_seen_scan, analyzed_at, changed_at)
VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)
ON CONFLICT(volume_id, rel_path) DO UPDATE SET
    rel_path_raw = excluded.rel_path_raw, kind = excluded.kind, size = excluded.size, mtime_ns = excluded.mtime_ns,
    state = excluded.state, error = excluded.error, content_hash = excluded.content_hash,
    first_seen_at = min(files.first_seen_at, excluded.first_seen_at), analyzed_at = excluded.analyzed_at,
    changed_at = excluded.changed_at
WHERE excluded.changed_at > files.changed_at
"""


def merge(cat: Catalog, payload: dict, previews: PreviewStore | None = None) -> dict:
    """Apply an agent's batch (from the network or a bundle). Returns the previews still needed."""
    from .catalog import path_key

    c = cat.conn
    agent = payload["agent"]
    now = time.time()
    received_contents: list[bytes] = []
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("INSERT INTO machines(id, name, os, last_seen) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                  "name = excluded.name, os = excluded.os, last_seen = excluded.last_seen",
                  (agent["machine_id"], agent.get("name", "agent"), agent.get("os", ""), now))
        for v in payload.get("volumes", []):
            c.execute("INSERT INTO volumes(id, label, fs_type, total_bytes, created_at, read_only) VALUES(?,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET fs_type = coalesce(excluded.fs_type, fs_type), "
                      "total_bytes = coalesce(excluded.total_bytes, total_bytes)",
                      (v["id"], v["label"], v.get("fs_type"), v.get("total_bytes"), now, int(v.get("read_only", False))))
            c.executemany("INSERT OR IGNORE INTO volume_fingerprints(kind, value, volume_id) VALUES(?,?,?)",
                          [(k, val, v["id"]) for k, val in v.get("fingerprints", [])])
            if v.get("online_root"):
                c.execute("INSERT INTO volume_presence(volume_id, machine_id, root, seen_at) VALUES(?,?,?,?) "
                          "ON CONFLICT(volume_id, machine_id) DO UPDATE SET root = excluded.root, seen_at = excluded.seen_at",
                          (v["id"], agent["machine_id"], v["online_root"], now))
            else:
                c.execute("DELETE FROM volume_presence WHERE volume_id = ? AND machine_id = ?",
                          (v["id"], agent["machine_id"]))
        # Contents first: files reference them.
        for row in payload.get("contents", []):
            values = [(_unb(row[k]) if k in BLOB_COLUMNS else row[k]) for k in CONTENT_COLUMNS]
            c.execute(f"INSERT INTO contents({', '.join(CONTENT_COLUMNS)}) VALUES({','.join('?' * len(CONTENT_COLUMNS))}) "
                      f"ON CONFLICT(hash) DO UPDATE SET {', '.join(f'{k} = excluded.{k}' for k in CONTENT_COLUMNS[1:])} "
                      f"WHERE excluded.analyzer_version > contents.analyzer_version OR "
                      f"(excluded.analyzer_version = contents.analyzer_version AND excluded.analyzed_at > contents.analyzed_at)",
                      values)
            if row.get("has_preview"):
                received_contents.append(_unb(row["hash"]))
        files = payload.get("files", [])
        c.executemany(_FILE_UPSERT, [
            (f["volume_id"], f["rel_path"], _unb(f["rel_path_raw"]), path_key(f["rel_path"]), f["kind"], f["size"],
             f["mtime_ns"], f["state"], f["error"], _unb(f["content_hash"]), f["first_seen_at"], f["analyzed_at"],
             f["changed_at"]) for f in files])
        for r in payload.get("results", []):
            _apply_result(c, agent["machine_id"], r)
        bump_files_version(c, contents=bool(payload.get("contents")))
        c.execute("UPDATE agents SET last_sync_at = ?, machine_id = ? WHERE id = ?",
                  (now, agent["machine_id"], payload.get("_agent_id")))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    need = [h.hex() for h in received_contents if previews is None or previews.get(h, "s") is None]
    return {"need_previews": need, "accepted": {"files": len(payload.get("files", [])),
                                                "contents": len(payload.get("contents", []))}}


def store_previews(previews: PreviewStore, items: list[dict]) -> int:
    previews.put_many([(bytes.fromhex(p["hash"]), p["size"], base64.b64decode(p["data"])) for p in items])
    return len(items)


# -- relaying plan items (hub side) -----------------------------------------------------------

def actions_for(cat: Catalog, machine_id: str, local_online: dict[str, Path]) -> list[dict]:
    """Plan items this agent should carry out now; moves pass the hub's last-copy check first."""
    c = cat.conn
    presence = agent_presence(cat)
    # Drives the hub reaches itself are handled by its own plan runs, never twice.
    here = {vid for vid, p in presence.items() if p["machine_id"] == machine_id and vid not in local_online}
    if not here:
        return []
    trusted = set(presence)  # drives other agents reported connected count as checked
    out = []
    pending = c.execute(
        f"""SELECT i.* FROM plan_items i JOIN volumes v ON v.id = i.volume_id
            WHERE v.read_only = 0 AND i.volume_id IN ({','.join('?' * len(here))})
            AND ((i.status IN ('pending', 'failed', 'blocked') AND i.relay_request IS NULL)
                 OR (i.relay_request IS NOT NULL AND i.relay_machine = ?))""",
        (*here, machine_id)).fetchall()
    for item in pending:
        kind = item["relay_request"] or "move"
        if kind == "move" and item["status"] != "relayed":
            problem = plans.survivor_problem(c, item, local_online, True, trusted=trusted)
            if problem:
                c.execute("UPDATE plan_items SET status = 'blocked', error = ?, updated_at = ? WHERE id = ?",
                          (problem, time.time(), item["id"]))
                continue
            c.execute("UPDATE plan_items SET status = 'relayed', error = NULL, relay_machine = ?, relay_request = 'move', "
                      "updated_at = ? WHERE id = ?", (machine_id, time.time(), item["id"]))
        out.append({"hub_item_id": item["id"], "kind": kind, "plan_id": item["plan_id"],
                    "volume_id": item["volume_id"], "rel_path": item["rel_path"],
                    "rel_path_raw": _b(item["rel_path_raw"]), "content_hash": _b(item["content_hash"]),
                    "size": item["size"], "mtime_ns": item["mtime_ns"], "quarantine_path": item["quarantine_path"]})
    return out


def _apply_result(c: sqlite3.Connection, machine_id: str, r: dict) -> None:
    item = c.execute("SELECT * FROM plan_items WHERE id = ? AND relay_machine = ?",
                     (r["hub_item_id"], machine_id)).fetchone()
    if item is None:
        return
    status = r["status"]
    c.execute("UPDATE plan_items SET status = ?, error = ?, quarantine_path = coalesce(?, quarantine_path), "
              "relay_request = NULL, updated_at = ? WHERE id = ?",
              (status, r.get("error"), r.get("quarantine_path"), time.time(), item["id"]))
    if status == "quarantined":
        c.execute("UPDATE files SET state = 'missing', error = ?, changed_at = ? WHERE id = ?",
                  (f"moved to quarantine (plan {item['plan_id']})", time.time(), item["file_id"]))
    elif status == "restored":
        c.execute("UPDATE files SET state = 'done', error = NULL, changed_at = ? WHERE id = ?",
                  (time.time(), item["file_id"]))
        c.execute("DELETE FROM decisions WHERE file_id = ?", (item["file_id"],))
    elif status == "purged":
        c.execute("UPDATE files SET error = ? WHERE id = ?",
                  (f"deleted permanently (plan {item['plan_id']})", item["file_id"]))


# -- carrying out relayed actions (agent side) ------------------------------------------------

def execute_actions(cat: Catalog, actions: list[dict], verify_hash: bool = True) -> int:
    """Store the hub's actions, then carry out whatever is possible now. Returns how many ran."""
    c = cat.conn
    for a in actions:
        c.execute("INSERT INTO relayed_actions(hub_item_id, kind, plan_id, volume_id, rel_path, rel_path_raw, "
                  "content_hash, size, mtime_ns, quarantine_path, status) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending') "
                  "ON CONFLICT(hub_item_id) DO UPDATE SET kind = excluded.kind, status = 'pending', reported = 0, "
                  "quarantine_path = coalesce(excluded.quarantine_path, quarantine_path) "
                  "WHERE relayed_actions.kind != excluded.kind",
                  (a["hub_item_id"], a["kind"], a["plan_id"], a["volume_id"], a["rel_path"], _unb(a["rel_path_raw"]),
                   _unb(a["content_hash"]), a["size"], a["mtime_ns"], a["quarantine_path"]))
    online = locate_online(cat)
    refs = {r["id"]: volume_ref(cat, r) for r in cat.list_volumes()}
    ran = 0
    for a in c.execute("SELECT * FROM relayed_actions WHERE status IN ('pending', 'moving')").fetchall():
        root = online.get(a["volume_id"])
        ref = refs.get(a["volume_id"])
        if root is None or ref is None or ref.read_only:
            continue
        status, error, qpath = _execute_one(cat, a, root, ref, verify_hash)
        if status is None:
            continue  # drive went away: try again next time
        c.execute("UPDATE relayed_actions SET status = ?, error = ?, quarantine_path = coalesce(?, quarantine_path), "
                  "reported = 0 WHERE hub_item_id = ?", (status, error, qpath, a["hub_item_id"]))
        ran += 1
    return ran


def _execute_one(cat: Catalog, a, root: Path, ref: VolumeRef, verify_hash: bool):
    src = to_os_path(root, a["rel_path"], a["rel_path_raw"])
    if a["kind"] == "move":
        if a["status"] == "moving":  # interrupted: settle from what is on disk
            dst_exists = a["quarantine_path"] and os.path.lexists(to_os_path(root, a["quarantine_path"], None))
            if dst_exists and not os.path.lexists(src):
                _mark_local_missing(cat, a)
                return "quarantined", None, a["quarantine_path"]
        try:
            st = os.stat(src)
        except OSError as e:
            if not ref.present_at(root):
                return None, None, None
            return ("skipped", "the file is no longer there", None) if isinstance(e, FileNotFoundError) \
                else ("failed", str(e), None)
        if st.st_size != a["size"] or not mtime_equivalent(st.st_mtime_ns, a["mtime_ns"]):
            return "skipped", "the file changed since it was scanned", None
        if verify_hash and a["content_hash"] is not None and plans._hash_file(src) != a["content_hash"]:
            return "skipped", "the file's contents changed since it was scanned", None
        qrel = plans._quarantine_rel(root, a["plan_id"], a["rel_path"])
        cat.conn.execute("UPDATE relayed_actions SET status = 'moving', quarantine_path = ? WHERE hub_item_id = ?",
                         (qrel, a["hub_item_id"]))
        dst = to_os_path(root, qrel, None)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
        except OSError as e:
            if not ref.present_at(root):
                return None, None, None
            return "failed", f"could not move the file: {e}", None
        plans._write_readme(root)
        _mark_local_missing(cat, a)
        return "quarantined", None, qrel
    qsrc = to_os_path(root, a["quarantine_path"], None) if a["quarantine_path"] else None
    if a["kind"] == "restore":
        if not qsrc or not os.path.lexists(qsrc):
            return "failed", "the quarantined file is not there", None
        if os.path.lexists(src):
            return "failed", "something else is now at the original location", None
        os.makedirs(os.path.dirname(src), exist_ok=True)
        os.rename(qsrc, src)
        cat.conn.execute("UPDATE files SET state = 'done', error = NULL WHERE volume_id = ? AND rel_path = ?",
                         (a["volume_id"], a["rel_path"]))
        return "restored", None, None
    if a["kind"] == "purge":
        if qsrc and os.path.lexists(qsrc):
            os.remove(qsrc)
        return "purged", None, None
    return "failed", f"unknown action {a['kind']}", None


def _mark_local_missing(cat: Catalog, a) -> None:
    cat.conn.execute("UPDATE files SET state = 'missing', error = ? WHERE volume_id = ? AND rel_path = ?",
                     (f"moved to quarantine (hub plan {a['plan_id']})", a["volume_id"], a["rel_path"]))


def unreported_results(cat: Catalog) -> list[dict]:
    return [{"hub_item_id": r["hub_item_id"], "status": r["status"], "error": r["error"],
             "quarantine_path": r["quarantine_path"]}
            for r in cat.conn.execute("SELECT * FROM relayed_actions WHERE reported = 0 AND status IN "
                                      "('quarantined', 'restored', 'purged', 'skipped', 'failed')")]


def mark_reported(cat: Catalog, results: list[dict]) -> None:
    cat.conn.executemany("UPDATE relayed_actions SET reported = 1 WHERE hub_item_id = ? AND status = ?",
                         [(r["hub_item_id"], r["status"]) for r in results])


# -- the agent's side of a sync ----------------------------------------------------------------

@dataclass
class HubConfig:
    url: str = ""
    token: str = ""
    cf_client_id: str = ""        # Cloudflare Access service token, when the hub is behind Cloudflare
    cf_client_secret: str = ""
    files_cursor: tuple[float, int] = (0.0, 0)
    contents_cursor: float = 0.0
    last_sync_at: float | None = None
    last_error: str | None = None

    def public(self) -> dict:
        d = dict(vars(self))
        d["token"] = "set" if self.token else ""
        d["cf_client_secret"] = "set" if self.cf_client_secret else ""
        return d


def load_hub(cat: Catalog) -> HubConfig:
    row = cat.conn.execute("SELECT value FROM settings WHERE key = 'hub'").fetchone()
    cfg = HubConfig()
    if row:
        for k, v in json.loads(row[0]).items():
            if hasattr(cfg, k):
                setattr(cfg, k, tuple(v) if k == "files_cursor" else v)
    return cfg


def save_hub(cat: Catalog, cfg: HubConfig) -> None:
    cat.conn.execute("INSERT INTO settings(key, value) VALUES('hub', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (json.dumps(vars(cfg)),))


class HubClient:
    """HTTP client for the hub's sync endpoints (through Tailscale or Cloudflare)."""

    def __init__(self, cfg: HubConfig):
        self.cfg = cfg

    def post(self, path: str, body: dict) -> dict:
        import urllib.error
        import urllib.request

        headers = {"Content-Type": "application/json", "X-Imagelib": "1", "Authorization": f"Bearer {self.cfg.token}"}
        if self.cfg.cf_client_id:
            headers["CF-Access-Client-Id"] = self.cfg.cf_client_id
            headers["CF-Access-Client-Secret"] = self.cfg.cf_client_secret
        req = urllib.request.Request(self.cfg.url.rstrip("/") + path, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", e.reason)
            except Exception:
                detail = e.reason
            raise RuntimeError(f"hub refused the request ({e.code}): {detail}") from None


def sync_with_hub(cat: Catalog, previews: PreviewStore, post, run_actions: bool = True) -> dict:
    """One full exchange with the hub: send changes and results, receive and carry out actions.

    ``post(path, body) -> dict`` talks to the hub (HubClient.post, or a test client).
    """
    cfg = load_hub(cat)
    online = locate_online(cat)
    cursor = Cursor(tuple(cfg.files_cursor), cfg.contents_cursor)
    sent = {"files": 0, "contents": 0, "previews": 0, "actions": 0, "results": 0}
    for round_ in range(2):  # a second round reports results of actions carried out in the first
        while True:
            payload, new_cursor, more = build_batch(cat, cursor, online)
            results = unreported_results(cat)
            payload["results"] = results
            payload["more"] = more
            resp = post("/api/sync/exchange", payload)
            for i in range(0, len(resp["need_previews"]), PREVIEW_BATCH):
                chunk = resp["need_previews"][i:i + PREVIEW_BATCH]
                items = [{"hash": h, "size": size, "data": base64.b64encode(data).decode()}
                         for h in chunk for size in ("s", "m")
                         if (data := previews.get(bytes.fromhex(h), size)) is not None]
                post("/api/sync/previews", {"previews": items})
                sent["previews"] += len(chunk)
            mark_reported(cat, results)
            sent["files"] += len(payload["files"])
            sent["contents"] += len(payload["contents"])
            sent["results"] += len(results)
            cursor = new_cursor
            cfg.files_cursor, cfg.contents_cursor = cursor.files, cursor.contents
            save_hub(cat, cfg)
            if not more:
                break
        actions = resp.get("actions", [])
        if not (run_actions and actions) or round_ == 1:
            break
        sent["actions"] += execute_actions(cat, actions)
        if not unreported_results(cat):
            break
    cfg = load_hub(cat)
    cfg.last_sync_at, cfg.last_error = time.time(), None
    save_hub(cat, cfg)
    return sent


# -- bundles: the same exchange, carried on a USB stick ------------------------------------------

def export_bundle(cat: Catalog, previews: PreviewStore, path: Path, full: bool = False) -> dict:
    """Write everything changed since the last bundle (or everything) into a single file."""
    key = "bundle_cursor"
    row = cat.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    saved = json.loads(row[0]) if row and not full else {"files": [0.0, 0], "contents": 0.0}
    cursor = Cursor(tuple(saved["files"]), saved["contents"])
    online = locate_online(cat)
    path = Path(path)
    if path.exists():
        path.unlink()
    out = sqlite3.connect(path)
    out.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
                      "CREATE TABLE batches(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL);"
                      "CREATE TABLE previews(hash BLOB, size TEXT, data BLOB, PRIMARY KEY(hash, size));")
    counts = {"files": 0, "contents": 0, "results": 0}
    seq = 0
    results = unreported_results(cat)
    while True:
        payload, cursor_next, more = build_batch(cat, cursor, online)
        if seq == 0:
            payload["results"] = results
        out.execute("INSERT INTO batches(seq, payload) VALUES(?, ?)", (seq, json.dumps(payload)))
        for row_ in payload["contents"]:
            if row_["has_preview"]:
                h = bytes.fromhex(row_["hash"])
                for size in ("s", "m"):
                    data = previews.get(h, size)
                    if data is not None:
                        out.execute("INSERT OR REPLACE INTO previews VALUES(?,?,?)", (h, size, data))
        counts["files"] += len(payload["files"])
        counts["contents"] += len(payload["contents"])
        cursor, seq = cursor_next, seq + 1
        if not more:
            break
    counts["results"] = len(results)
    out.execute("INSERT INTO meta VALUES('format', 'imagelib-bundle-1')")
    out.execute("INSERT INTO meta VALUES('machine_id', ?)", (cat.machine_id,))
    out.commit()
    out.close()
    cat.conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps({"files": list(cursor.files), "contents": cursor.contents})))
    mark_reported(cat, results)
    return counts


def import_bundle(cat: Catalog, previews: PreviewStore, path: Path) -> dict:
    src = sqlite3.connect(path)
    try:
        fmt = src.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
        if not fmt or fmt[0] != "imagelib-bundle-1":
            raise ValueError("not an imagelib bundle")
        counts = {"files": 0, "contents": 0, "previews": 0}
        for (payload,) in src.execute("SELECT payload FROM batches ORDER BY seq"):
            result = merge(cat, json.loads(payload), None)
            counts["files"] += result["accepted"]["files"]
            counts["contents"] += result["accepted"]["contents"]
        rows = src.execute("SELECT hash, size, data FROM previews").fetchall()
        previews.put_many(rows)
        counts["previews"] = len(rows)
        return counts
    finally:
        src.close()
