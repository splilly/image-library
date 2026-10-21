"""Hub & agents: an agent's drive appears on the hub, and the hub's plans are carried out by the agent."""

import io

import pytest
from conftest import file_row, register, scan
from fastapi.testclient import TestClient

from imagelib import remote, sync
from imagelib.catalog import Catalog
from imagelib.previews import PreviewStore
from imagelib.server import create_app


@pytest.fixture
def hub(tmp_path):
    app = create_app(tmp_path / "hub", auto_relate=False)
    with TestClient(app, headers={"x-imagelib": "1"}) as client:
        yield client


@pytest.fixture
def agent(drive, tmp_path, hub):
    """An agent machine with its own catalog and the sample drive attached."""
    root, rel = drive
    cat = Catalog(tmp_path / "agent")
    cat.conn.execute("UPDATE machines SET name = 'mac-mini' WHERE id = ?", (cat.machine_id,))
    vid = register(cat, root, "APFS drive")
    scan(cat, root, vid)
    token = hub.post("/api/remote/agents", json={"name": "Mac mini"}).json()["token"]
    sync.save_hub(cat, sync.HubConfig(url="https://hub", token=token))

    def post(path, body):
        r = hub.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()

    def run():
        return sync.sync_with_hub(cat, PreviewStore(cat.previews_path), post)

    return {"cat": cat, "vid": vid, "root": root, "rel": rel, "token": token, "post": post, "sync": run}


def hub_files(hub, **params):
    items, cursor = [], None
    while True:
        page = hub.get("/api/files", params={**params, **({"cursor": cursor} if cursor else {})}).json()
        items += page["items"]
        cursor = page["next"]
        if not cursor:
            return {i["path"]: i for i in items}


def test_agent_scans_appear_on_the_hub(hub, agent):
    sent = agent["sync"]()
    # Every file travels (notes.txt too, for comparing folders); the summary counts pictures.
    assert sent["files"] == 23 and sent["contents"] == 20 and sent["previews"] > 0
    assert hub.get("/api/summary").json()["files"] == 22
    vol = next(v for v in hub.get("/api/volumes").json() if v["id"] == agent["vid"])
    assert vol["label"] == "APFS drive" and not vol["online"] and vol["agent"]["machine"] == "mac-mini"
    files = hub_files(hub)
    photo = files[agent["rel"]["photo1"]]
    assert photo["copies"] == 2 and photo["status"] == "ok"
    assert hub.get(f"/api/preview/{photo['hash']}?size=m").status_code == 200
    assert files[agent["rel"]["zeroed"]]["status"] == "corrupt"
    agents = hub.get("/api/remote/agents").json()
    assert agents[0]["machine"] == "mac-mini" and agents[0]["connected_volumes"] == ["APFS drive"]


def test_only_changes_are_sent_again(hub, agent):
    agent["sync"]()
    assert agent["sync"]()["files"] == 0
    root, rel = agent["root"], agent["rel"]
    (root / rel["photo6"]).unlink()
    scan(agent["cat"], root, agent["vid"])
    sent = agent["sync"]()
    assert sent["files"] == 1
    assert hub_files(hub, status="missing")[rel["photo6"]]["state"] == "missing"


def test_the_most_recent_change_wins(hub, agent):
    agent["sync"]()
    hub_cat = Catalog.shared(hub.app.state.access.catalog_dir)
    path = agent["rel"]["photo5"]
    hub_cat.conn.execute("UPDATE files SET error = 'hub knows better', changed_at = 9e12 WHERE rel_path = ?", (path,))
    agent["cat"].conn.execute("UPDATE files SET error = 'agent says' WHERE rel_path = ?", (path,))
    agent["sync"]()
    assert hub_cat.conn.execute("SELECT error FROM files WHERE rel_path = ?", (path,)).fetchone()[0] == "hub knows better"


def test_agent_tokens_open_the_sync_endpoints_and_nothing_else(hub, agent, monkeypatch):
    assert hub.post("/api/sync/exchange", json={}).status_code == 401
    assert hub.post("/api/sync/exchange", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    # Remotely (through Tailscale), the agent token is not a session for the rest of the app.
    monkeypatch.setattr(remote, "owner_logins", lambda: ["me@example.com"])
    hub.post("/api/remote/password", json={"password": "a long password"})
    hub.put("/api/remote", json={"tailscale_enabled": True, "tailscale_hosts": ["hub.tail.ts.net"]})
    with TestClient(hub.app, base_url="https://hub.tail.ts.net",
                    headers={"x-imagelib": "1", "tailscale-user-login": "me@example.com",
                             "authorization": f"Bearer {agent['token']}"}) as far:
        assert far.get("/api/summary").status_code == 401
        payload, _, _ = sync.build_batch(agent["cat"], sync.Cursor(), {})
        assert far.post("/api/sync/exchange", json={**payload, "results": [], "more": True}).status_code == 200
    # Revoked: refused.
    agent_id = hub.get("/api/remote/agents").json()[0]["id"]
    hub.post(f"/api/remote/agents/{agent_id}/revoke")
    with pytest.raises(Exception):
        agent["sync"]()


def test_hub_plans_are_carried_out_by_the_agent(hub, agent):
    agent["sync"]()
    root, rel = agent["root"], agent["rel"]
    files = hub_files(hub)
    hub.post("/api/decisions", json={"file_ids": [files[rel["dup1"]]["id"]], "decision": "discard",
                                     "reason": "duplicate"}).raise_for_status()
    plan = hub.post("/api/plans").json()
    detail = hub.get(f"/api/plans/{plan['id']}").json()
    assert detail["volumes"][0]["counts"]["pending"]["files"] == 1

    sent = agent["sync"]()                          # receives the move, carries it out, reports back
    assert sent["actions"] == 1 and sent["results"] == 1
    assert not (root / rel["dup1"]).exists()
    assert (root / "_imagelib_quarantine" / f"plan-{plan['id']}" / rel["dup1"]).exists()
    items = hub.get(f"/api/plans/{plan['id']}/items").json()["items"]
    assert items[0]["status"] == "quarantined"
    assert hub_files(hub, status="missing")[rel["dup1"]]["state"] == "missing"

    restored = hub.post(f"/api/plans/{plan['id']}/restore", json={}).json()
    assert restored["relayed"] == 1                 # the hub cannot reach that drive: the agent will
    agent["sync"]()
    assert (root / rel["dup1"]).exists()
    assert hub.get(f"/api/plans/{plan['id']}/items").json()["items"][0]["status"] == "restored"


def test_the_last_copy_rule_holds_across_machines(hub, agent):
    agent["sync"]()
    rel = agent["rel"]
    files = hub_files(hub)
    # The kept copy (photo 2) is on the agent's drive; the agent reports it connected: allowed.
    hub.post("/api/decisions", json={"file_ids": [files[rel["dup2"]]["id"]], "decision": "discard"})
    plan = hub.post("/api/plans").json()
    agent["sync"]()
    assert hub.get(f"/api/plans/{plan['id']}/items").json()["items"][0]["status"] == "quarantined"


def test_bundles_carry_the_same_data_without_a_connection(hub, agent, tmp_path):
    bundle = tmp_path / "agent.imagelib-bundle"
    counts = sync.export_bundle(agent["cat"], PreviewStore(agent["cat"].previews_path), bundle)
    assert counts["files"] == 23
    r = hub.post("/api/remote/bundle/import",
                 files={"file": ("agent.imagelib-bundle", bundle.read_bytes(), "application/octet-stream")})
    assert r.status_code == 200 and r.json()["files"] == 23 and r.json()["previews"] > 0
    assert hub.get("/api/summary").json()["files"] == 22
    # A second export only carries what changed since.
    assert sync.export_bundle(agent["cat"], PreviewStore(agent["cat"].previews_path), bundle)["files"] == 0
    bad = hub.post("/api/remote/bundle/import", files={"file": ("x", io.BytesIO(b"not a bundle"), "x")})
    assert bad.status_code == 400
