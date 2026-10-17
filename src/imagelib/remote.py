"""Remote access: using the GUI from another device, through Tailscale or Cloudflare.

The server only ever listens on 127.0.0.1. Remote traffic reaches it through a proxy
running on this machine — ``tailscale serve`` or ``cloudflared`` — so arriving on the
loopback interface proves nothing. A request is *local* only when it is addressed to a
local host name **and** carries none of the headers proxies add. Anything else is
*remote* and must pass two independent checks:

1. the tunnel's own identity: the Tailscale login attached by ``tailscale serve``
   (by default only this machine's owner), or a Cloudflare Access token (JWT) signed
   for this application;
2. the app's login: a password, giving a long-lived, revocable session cookie.

Remote-access settings and the password can only be changed from this machine, so a
stolen session cannot lock the owner out or widen access.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from .catalog import Catalog

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
# Headers that only appear when a request came through a proxy or tunnel.
PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "cf-access-jwt-assertion", "tailscale-user-login",
                 "x-forwarded-for", "x-forwarded-host", "forwarded")
SESSION_COOKIE = "imagelib_session"
SESSION_DAYS = 90


# -- settings ----------------------------------------------------------------------

@dataclass
class RemoteSettings:
    tailscale_enabled: bool = False
    tailscale_hosts: list[str] = field(default_factory=list)      # e.g. ["desktop.tail1234.ts.net"]
    tailscale_users: list[str] = field(default_factory=list)      # allowed logins; empty = this machine's owner
    cloudflare_enabled: bool = False
    cloudflare_hosts: list[str] = field(default_factory=list)     # e.g. ["photos.example.com"]
    cloudflare_team: str = ""                                      # e.g. "myteam" (myteam.cloudflareaccess.com)
    cloudflare_aud: str = ""                                       # Access application audience (AUD) tag
    cloudflare_tunnel_token: str = ""                              # optional: let the app run cloudflared

    @property
    def any_enabled(self) -> bool:
        return self.tailscale_enabled or self.cloudflare_enabled

    def public(self) -> dict:
        d = dict(vars(self))
        d["cloudflare_tunnel_token"] = "set" if self.cloudflare_tunnel_token else ""
        return d


def load_settings(cat: Catalog) -> RemoteSettings:
    row = cat.conn.execute("SELECT value FROM settings WHERE key = 'remote'").fetchone()
    s = RemoteSettings()
    if row:
        for k, v in json.loads(row[0]).items():
            if hasattr(s, k):
                setattr(s, k, v)
    return s


def save_settings(cat: Catalog, s: RemoteSettings) -> None:
    for name in ("tailscale_hosts", "cloudflare_hosts"):
        setattr(s, name, sorted({h.strip().lower().rstrip(".") for h in getattr(s, name) if h.strip()}))
    s.tailscale_users = sorted({u.strip().lower() for u in s.tailscale_users if u.strip()})
    s.cloudflare_team = s.cloudflare_team.strip().removesuffix(".cloudflareaccess.com").removeprefix("https://")
    cat.conn.execute("INSERT INTO settings(key, value) VALUES('remote', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(vars(s)),))


# -- password & sessions ------------------------------------------------------------

def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def set_password(cat: Catalog, password: str) -> None:
    if len(password) < 10:
        raise ValueError("use at least 10 characters")
    salt = secrets.token_bytes(16)
    value = json.dumps({"salt": salt.hex(), "hash": _scrypt(password, salt).hex()})
    cat.conn.execute("INSERT INTO settings(key, value) VALUES('password', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (value,))
    cat.conn.execute("DELETE FROM sessions")  # a new password signs every device out


def has_password(cat: Catalog) -> bool:
    return cat.conn.execute("SELECT 1 FROM settings WHERE key = 'password'").fetchone() is not None


def check_password(cat: Catalog, password: str) -> bool:
    row = cat.conn.execute("SELECT value FROM settings WHERE key = 'password'").fetchone()
    if row is None:
        return False
    stored = json.loads(row[0])
    return hmac.compare_digest(_scrypt(password, bytes.fromhex(stored["salt"])), bytes.fromhex(stored["hash"]))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(cat: Catalog, via: str, client: str, user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    cat.conn.execute("INSERT INTO sessions(token_hash, created_at, last_seen_at, expires_at, via, client, user_agent) "
                     "VALUES(?,?,?,?,?,?,?)", (_token_hash(token), now, now, now + SESSION_DAYS * 86400,
                                               via, client[:200], user_agent[:300]))
    return token


def valid_session(cat: Catalog, token: str | None) -> bool:
    if not token:
        return False
    h = _token_hash(token)
    row = cat.conn.execute("SELECT expires_at, last_seen_at FROM sessions WHERE token_hash = ?", (h,)).fetchone()
    if row is None or row[0] < time.time():
        return False
    if time.time() - row[1] > 300:  # don't write on every request
        cat.conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (time.time(), h))
    return True


def end_session(cat: Catalog, token: str | None) -> None:
    if token:
        cat.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def list_sessions(cat: Catalog) -> list[dict]:
    return [{"id": r["token_hash"][:16], "created_at": r["created_at"], "last_seen_at": r["last_seen_at"],
             "via": r["via"], "client": r["client"], "user_agent": r["user_agent"]}
            for r in cat.conn.execute("SELECT * FROM sessions WHERE expires_at > ? ORDER BY last_seen_at DESC",
                                      (time.time(),))]


def revoke_session(cat: Catalog, session_id: str) -> int:
    return cat.conn.execute("DELETE FROM sessions WHERE substr(token_hash, 1, 16) = ?", (session_id,)).rowcount


class LoginThrottle:
    """Slows down password guessing: per client, and for everyone after many failures."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def delay(self, client: str) -> float:
        now = time.time()
        with self._lock:
            mine = [t for t in self._fails.get(client, []) if now - t < 900]
            total = sum(1 for ts in self._fails.values() for t in ts if now - t < 60)
        if total >= 20:
            return 30.0
        return min(30.0, 0.5 * (2 ** max(0, len(mine) - 2))) if len(mine) > 2 else 0.0

    def failed(self, client: str) -> None:
        with self._lock:
            self._fails.setdefault(client, []).append(time.time())

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._fails.pop(client, None)


# -- classifying requests ----------------------------------------------------------------

@dataclass
class Verdict:
    kind: str                     # local | remote | forbidden
    via: str = ""                 # tailscale | cloudflare
    identity: str = ""            # who the tunnel says this is
    problem: str = ""             # why it is forbidden


def classify(headers: dict[str, str], host: str | None, settings: RemoteSettings, verifier) -> Verdict:
    """Decide where a request comes from and whether its tunnel identity is acceptable."""
    host = (host or "").lower().rstrip(".")
    proxied = any(h in headers for h in PROXY_HEADERS)
    if host in LOCAL_HOSTS and not proxied:
        return Verdict("local")
    if settings.tailscale_enabled and host in settings.tailscale_hosts:
        login = headers.get("tailscale-user-login", "").lower()
        if not login:
            return Verdict("forbidden", problem="Tailscale identity missing (use `tailscale serve`)")
        allowed = settings.tailscale_users or owner_logins()
        if login not in allowed:
            return Verdict("forbidden", problem=f"Tailscale user {login} is not allowed")
        return Verdict("remote", "tailscale", login)
    if settings.cloudflare_enabled and host in settings.cloudflare_hosts:
        token = headers.get("cf-access-jwt-assertion", "")
        if not token:
            return Verdict("forbidden", problem="Cloudflare Access login missing")
        try:
            claims = verifier.verify(token, settings.cloudflare_team, settings.cloudflare_aud)
        except Exception as e:
            return Verdict("forbidden", problem=f"Cloudflare Access token rejected: {e}")
        return Verdict("remote", "cloudflare", claims.get("email") or claims.get("sub", ""))
    return Verdict("forbidden", problem="unknown host")


def client_label(headers: dict[str, str], verdict: Verdict) -> str:
    where = headers.get("cf-connecting-ip") or headers.get("x-forwarded-for", "").split(",")[0].strip()
    return " · ".join(x for x in (verdict.identity, where) if x)


# -- Cloudflare Access tokens --------------------------------------------------------------

class AccessVerifier:
    """Verifies Cloudflare Access JWTs against the team's published signing keys."""

    def __init__(self, fetch=None, ttl: float = 3600):
        self._fetch = fetch or _fetch_json
        self._ttl = ttl
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[float, dict]] = {}

    def _jwks(self, team: str, refresh: bool = False) -> dict:
        with self._lock:
            cached = self._keys.get(team)
            if cached and not refresh and time.time() - cached[0] < self._ttl:
                return cached[1]
        jwks = self._fetch(f"https://{team}.cloudflareaccess.com/cdn-cgi/access/certs")
        with self._lock:
            self._keys[team] = (time.time(), jwks)
        return jwks

    def verify(self, token: str, team: str, aud: str) -> dict:
        import jwt

        if not team or not aud:
            raise ValueError("Cloudflare team and application AUD tag are not configured")
        kid = jwt.get_unverified_header(token).get("kid")
        for refresh in (False, True):  # keys rotate: refetch once if the key id is unknown
            keys = {k["kid"]: k for k in self._jwks(team, refresh).get("keys", [])}
            if kid in keys:
                key = jwt.PyJWK(keys[kid]).key
                return jwt.decode(token, key, algorithms=["RS256"], audience=aud,
                                  issuer=f"https://{team}.cloudflareaccess.com",
                                  options={"require": ["exp", "iat", "aud", "iss"]}, leeway=30)
        raise ValueError("token signed with an unknown key")


def _fetch_json(url: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


# -- Tailscale ----------------------------------------------------------------------------

def _tailscale_cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in (r"C:\Program Files\Tailscale\tailscale.exe",
                      "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                      "/mnt/c/Program Files/Tailscale/tailscale.exe"):
        if os.path.exists(candidate):
            return candidate
    return None


def tailscale_status() -> dict:
    """What this machine looks like on the tailnet: installed? its name? its owner?"""
    cli = _tailscale_cli()
    if not cli:
        return {"installed": False}
    try:
        out = subprocess.run([cli, "status", "--json"], capture_output=True, timeout=10).stdout
        status = json.loads(out)
    except Exception as e:
        return {"installed": True, "running": False, "error": str(e)}
    me = status.get("Self") or {}
    user = (status.get("User") or {}).get(str(me.get("UserID")), {})
    return {"installed": True, "running": status.get("BackendState") == "Running",
            "host": (me.get("DNSName") or "").rstrip("."), "owner": (user.get("LoginName") or "").lower()}


_owner_cache: tuple[float, list[str]] = (0.0, [])


def owner_logins() -> list[str]:
    global _owner_cache
    if time.time() - _owner_cache[0] > 60:
        owner = tailscale_status().get("owner")
        _owner_cache = (time.time(), [owner] if owner else [])
    return _owner_cache[1]


def tailscale_serve_command(port: int) -> str:
    return f"tailscale serve --bg {port}"


# -- cloudflared ---------------------------------------------------------------------------

class CloudflaredProcess:
    """Runs ``cloudflared tunnel run --token ...`` for a tunnel managed in the Cloudflare dashboard."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.log: list[str] = []

    @staticmethod
    def binary() -> str | None:
        if getattr(sys, "frozen", False):  # bundled next to the packaged app
            bundled = os.path.join(os.path.dirname(sys.executable),
                                   "cloudflared.exe" if sys.platform == "win32" else "cloudflared")
            if os.path.exists(bundled):
                return bundled
        found = shutil.which("cloudflared")
        if found:
            return found
        for candidate in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", "/usr/local/bin/cloudflared",
                          "/opt/homebrew/bin/cloudflared"):
            if os.path.exists(candidate):
                return candidate
        return None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, token: str) -> None:
        if self.running:
            return
        binary = self.binary()
        if not binary:
            raise RuntimeError("cloudflared is not installed")
        if not token:
            raise RuntimeError("no tunnel token configured")
        # The token goes through the environment, not the command line (visible to other users).
        env = {**os.environ, "TUNNEL_TOKEN": token}
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen([binary, "tunnel", "--no-autoupdate", "run"], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     creationflags=flags)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        proc = self.proc
        for line in proc.stdout if proc and proc.stdout else []:
            self.log = (self.log + [line.rstrip()])[-50:]

    def stop(self) -> None:
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def status(self) -> dict:
        return {"installed": self.binary() is not None, "running": self.running, "log": self.log[-8:]}
