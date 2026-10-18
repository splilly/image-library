"""Remote access through Tailscale and Cloudflare: both layers must hold, and nothing else gets in."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from imagelib import remote
from imagelib.server import create_app

TS_HOST = "desk.tail1234.ts.net"
CF_HOST = "photos.example.com"
OWNER = "me@example.com"
PASSWORD = "correct horse battery"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "owner_logins", lambda: [OWNER])
    monkeypatch.setattr(remote, "tailscale_status", lambda: {"installed": True, "running": True,
                                                             "host": TS_HOST, "owner": OWNER})
    return create_app(tmp_path / "catalog", auto_relate=False)


@pytest.fixture
def local(app):
    with TestClient(app, headers={"x-imagelib": "1"}) as c:
        yield c


def remote_client(app, host, headers):
    return TestClient(app, base_url=f"https://{host}", headers={"x-imagelib": "1", **headers})


@pytest.fixture
def signing(app):
    """A Cloudflare-like signing key, published through the app's verifier."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid="k1", alg="RS256", use="sig")
    app.state.access.verifier = remote.AccessVerifier(fetch=lambda url: {"keys": [jwk]})

    def token(aud="AUD123", exp=None, kid="k1", team="myteam", email=OWNER):
        now = int(time.time())
        claims = {"aud": [aud], "iss": f"https://{team}.cloudflareaccess.com", "email": email,
                  "iat": now, "exp": exp if exp is not None else now + 3600, "sub": "u1"}
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})
    return token


def enable(local, **settings):
    assert local.post("/api/remote/password", json={"password": PASSWORD}).status_code == 200
    r = local.put("/api/remote", json=settings)
    assert r.status_code == 200, r.text
    return r.json()


def test_local_needs_no_login_and_foreign_or_proxied_requests_are_refused(local):
    assert local.get("/api/summary").status_code == 200
    assert local.get("/api/summary", headers={"host": "evil.example"}).status_code == 403
    # Arriving on localhost proves nothing: a proxy header makes it a remote request.
    assert local.get("/api/summary", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 403
    assert local.get("/api/summary", headers={"cf-connecting-ip": "203.0.113.9"}).status_code == 403


def test_remote_access_requires_a_password_first(local):
    r = local.put("/api/remote", json={"tailscale_enabled": True, "tailscale_hosts": [TS_HOST]})
    assert r.status_code == 400 and "password" in r.json()["detail"]
    assert local.post("/api/remote/password", json={"password": "short"}).status_code == 400


def test_tailscale_identity_and_app_login_are_both_required(app, local):
    enable(local, tailscale_enabled=True, tailscale_hosts=[TS_HOST])
    with remote_client(app, TS_HOST, {}) as anon:                      # not via tailscale serve
        assert anon.get("/api/summary").status_code == 403
    with remote_client(app, TS_HOST, {"tailscale-user-login": "stranger@example.com"}) as other:
        assert other.get("/api/summary").status_code == 403
    with remote_client(app, TS_HOST, {"tailscale-user-login": OWNER}) as me:
        r = me.get("/api/summary")
        assert r.status_code == 401 and r.json()["login"] is True
        status = me.get("/api/auth/status").json()
        assert status == {"local": False, "via": "tailscale", "identity": OWNER, "signed_in": False,
                          "password_set": True}
        assert me.post("/api/auth/login", json={"password": "wrong password!"}).status_code == 401
        r = me.post("/api/auth/login", json={"password": PASSWORD})
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
        assert me.get("/api/summary").status_code == 200
        # Settings stay local-only, even signed in.
        assert me.get("/api/remote").status_code == 403
        assert me.post("/api/remote/password", json={"password": "x" * 12}).status_code == 403
        # The cross-site guard still applies.
        assert me.post("/api/scan/pause", headers={"x-imagelib": ""}).status_code == 403
        me.post("/api/auth/logout")
        assert me.get("/api/summary").status_code == 401


def test_cloudflare_access_token_is_verified(app, local, signing):
    enable(local, cloudflare_enabled=True, cloudflare_hosts=[CF_HOST], cloudflare_team="myteam",
           cloudflare_aud="AUD123")

    def status(token):
        with remote_client(app, CF_HOST, {"cf-access-jwt-assertion": token} if token else {}) as c:
            return c.get("/api/auth/status").status_code

    assert status(signing()) == 200
    assert status(None) == 403                              # not through Access
    assert status(signing(aud="OTHER")) == 403              # token for another application
    assert status(signing(exp=int(time.time()) - 3600)) == 403  # expired
    assert status(signing(team="otherteam")) == 403         # issued by another team
    assert status(signing(kid="unknown")) == 403            # signed with a key not published

    with remote_client(app, CF_HOST, {"cf-access-jwt-assertion": signing()}) as c:
        assert c.get("/api/summary").status_code == 401     # Access alone is not enough
        c.post("/api/auth/login", json={"password": PASSWORD})
        assert c.get("/api/summary").status_code == 200


def test_sessions_can_be_listed_and_revoked_locally(app, local):
    enable(local, tailscale_enabled=True, tailscale_hosts=[TS_HOST])
    with remote_client(app, TS_HOST, {"tailscale-user-login": OWNER, "user-agent": "Phone"}) as me:
        me.post("/api/auth/login", json={"password": PASSWORD})
        sessions = local.get("/api/remote").json()["sessions"]
        assert len(sessions) == 1 and sessions[0]["via"] == "tailscale" and sessions[0]["user_agent"] == "Phone"
        local.post(f"/api/remote/sessions/{sessions[0]['id']}/revoke")
        assert me.get("/api/summary").status_code == 401


def test_changing_the_password_signs_every_device_out(app, local):
    enable(local, tailscale_enabled=True, tailscale_hosts=[TS_HOST])
    with remote_client(app, TS_HOST, {"tailscale-user-login": OWNER}) as me:
        me.post("/api/auth/login", json={"password": PASSWORD})
        local.post("/api/remote/password", json={"password": "another long password"})
        assert me.get("/api/summary").status_code == 401


def test_disabled_methods_let_nothing_through(app, local, signing):
    enable(local, tailscale_enabled=True, tailscale_hosts=[TS_HOST])
    local.put("/api/remote", json={"tailscale_enabled": False})
    with remote_client(app, TS_HOST, {"tailscale-user-login": OWNER}) as me:
        assert me.get("/api/auth/status").status_code == 403


def test_login_throttle_slows_repeated_failures():
    t = remote.LoginThrottle()
    assert t.delay("a") == 0
    for _ in range(5):
        t.failed("a")
    assert t.delay("a") >= 2
    assert t.delay("b") == 0                  # other clients unaffected...
    for i in range(20):
        t.failed(f"x{i}")
    assert t.delay("b") == 30                 # ...until there is a flood of failures
    t.succeeded("a")


def test_refusals_explain_what_to_change(app, local):
    with remote_client(app, TS_HOST, {"tailscale-user-login": OWNER, "accept": "text/html"}) as c:
        r = c.get("/")
        assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")
        assert "Tailscale access is turned off" in r.text and "Settings" in r.text
    enable(local, tailscale_enabled=True, tailscale_hosts=["other.tail1234.ts.net"])
    with remote_client(app, TS_HOST, {"tailscale-user-login": OWNER}) as c:
        assert "not one of the allowed Tailscale names" in c.get("/api/summary").json()["detail"]


def test_remote_requests_never_wait_for_a_busy_catalog(app, local):
    """A long write elsewhere (a scan) must not stall or fail signed-in requests."""
    import sqlite3
    import threading

    enable(local, tailscale_enabled=True, tailscale_hosts=[TS_HOST])
    with remote_client(app, TS_HOST, {"tailscale-user-login": OWNER}) as me:
        me.post("/api/auth/login", json={"password": PASSWORD})
        db = app.state.access.catalog().db_path
        # Make the next request want to record "last seen" (a write).
        app.state.access.catalog().conn.execute("UPDATE sessions SET last_seen_at = 0")
        remote._seen_saved_at = 0
        locked, release = threading.Event(), threading.Event()

        def hold_write_lock():
            c = sqlite3.connect(db, isolation_level=None)
            c.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(10)
            c.execute("ROLLBACK")
            c.close()

        holder = threading.Thread(target=hold_write_lock)
        holder.start()
        locked.wait(5)
        try:
            started = time.monotonic()
            assert me.get("/api/summary").status_code == 200
            assert me.get("/api/auth/status").json()["signed_in"] is True
            assert time.monotonic() - started < 3   # the lock is held for longer than that
        finally:
            time.sleep(1)
            release.set()
            holder.join()
