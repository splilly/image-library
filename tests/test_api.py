import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from imagelib.server import create_app


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "catalog", workers=2)
    with TestClient(app, headers={"x-imagelib": "1"}) as client:
        yield client


def wait_for_scan(api, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = api.get("/api/scan").json()
        if not state["running"]:
            return state
        time.sleep(0.2)
    raise AssertionError("scan did not finish")


@pytest.fixture
def scanned(api, drive):
    root, rel = drive
    vid = api.post("/api/volumes", json={"path": str(root), "label": "Sample"}).json()["id"]
    api.post("/api/scan", json={"volume_ids": [vid]}).raise_for_status()
    state = wait_for_scan(api)
    assert [t["state"] for t in state["targets"]] == ["done"]
    return api, vid, root, rel


def all_files(api, **params):
    items, cursor = [], None
    while True:
        page = api.get("/api/files", params={**params, "limit": 5, **({"cursor": cursor} if cursor else {})}).json()
        items += page["items"]
        cursor = page["next"]
        if not cursor:
            return items


def test_rejects_foreign_hosts_and_cross_site_writes(tmp_path):
    app = create_app(tmp_path / "catalog")
    with TestClient(app) as client:
        assert client.get("/api/summary", headers={"host": "evil.example"}).status_code == 403
        assert client.post("/api/scan/pause").status_code == 403          # no x-imagelib header
        assert client.post("/api/scan/pause", headers={"x-imagelib": "1"}).status_code == 200
        assert client.get("/api/summary").status_code == 200


def test_scan_through_the_api_and_summarise(scanned):
    api, vid, root, rel = scanned
    s = api.get("/api/summary").json()
    assert s["files"] == 22 and s["corrupt"] == 4 and s["redundant_copies"] == 3
    vols = api.get("/api/volumes").json()
    assert vols[0]["online"] and vols[0]["stats"]["done"] == 22


def test_listing_pages_through_everything_in_order(scanned):
    api, vid, root, rel = scanned
    items = all_files(api, volume=vid)
    paths = [i["path"] for i in items]
    assert len(paths) == 22 and paths == sorted(paths)
    by_date = all_files(api, sort="date_desc")
    dates = [i["taken_at"] or "" for i in by_date]
    assert dates == sorted(dates, reverse=True)


def test_filters(scanned):
    api, vid, root, rel = scanned
    problems = {i["path"] for i in all_files(api, status="problems")}
    assert {rel["truncated"], rel["zeroed"], rel["empty"], rel["garbage_raw"], rel["grey_band"]} == problems
    assert len(all_files(api, unique=True)) == 19
    assert {i["path"] for i in all_files(api, folder="photos/2004", recursive=False)} == \
        {rel[f"photo{i}"] for i in range(1, 7)}
    assert len(all_files(api, folder="photos/2004")) == 8   # includes .thumbnails
    assert [i["path"] for i in all_files(api, q="tn_")] == [rel["orphan_thumb"]]
    assert all(i["camera"] == "Canon EOS 10D" for i in all_files(api, camera="Canon EOS 10D"))
    assert api.get("/api/files/count", params={"kind": "video"}).json()["files"] == 1
    assert {i["path"] for i in all_files(api, status="duplicates")} == \
        {rel[f"photo{i}"] for i in (1, 2, 3)} | {rel[f"dup{i}"] for i in (1, 2, 3)}
    dup = next(i for i in all_files(api) if i["path"] == rel["dup1"])
    assert dup["copies"] == 2


def test_folders_and_facets(scanned):
    api, vid, root, rel = scanned
    top = {f["name"]: f["files"] for f in api.get("/api/folders", params={"volume": vid}).json()}
    assert top["photos"] == 8 and top["broken"] == 5 and "$RECYCLE.BIN" not in top
    sub = api.get("/api/folders", params={"volume": vid, "folder": "photos/2004"}).json()
    assert [f["name"] for f in sub] == [".thumbnails"]
    facets = api.get("/api/facets").json()
    assert {"name": "Canon EOS 10D", "count": 9} in facets["cameras"]  # 6 photos + 3 damaged copies


def test_content_detail_shows_locations_and_lookalikes(scanned):
    api, vid, root, rel = scanned
    photo1 = next(i for i in all_files(api) if i["path"] == rel["photo1"])
    detail = api.get(f"/api/contents/{photo1['hash']}").json()
    assert {loc["path"] for loc in detail["locations"]} == {rel["photo1"], rel["dup1"]}
    assert all(loc["online"] for loc in detail["locations"])
    thumb1 = next(i for i in all_files(api) if i["path"] == rel["thumb1"])
    assert thumb1["hash"] in {s["hash"] for s in detail["similar"]}
    assert detail["metadata"]["Exif.Image.Model"] == "Canon EOS 10D"


def test_previews_and_originals(scanned):
    api, vid, root, rel = scanned
    items = {i["path"]: i for i in all_files(api)}
    r = api.get(f"/api/preview/{items[rel['photo1']]['hash']}?size=m")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(r.content)).size == (1024, 768)
    r = api.get(f"/api/files/{items[rel['photo1']]['id']}/original")
    assert r.status_code == 200 and r.content == (root / rel["photo1"]).read_bytes()
    assert api.get(f"/api/files/{items[rel['heic']]['id']}/original").status_code == 415
    assert api.get("/api/preview/" + "00" * 32).status_code == 404
    assert api.get("/api/preview/zz").status_code == 400


def test_original_of_an_offline_drive_is_refused_clearly(scanned, tmp_path):
    api, vid, root, rel = scanned
    item = next(i for i in all_files(api) if i["path"] == rel["photo1"])
    root.rename(tmp_path / "unplugged")
    api.get("/api/volumes", params={"refresh": True})
    assert api.get(f"/api/files/{item['id']}/original").status_code == 409


def test_where_upload_finds_exact_and_resized_copies(scanned):
    api, vid, root, rel = scanned
    r = api.post("/api/where", files={"file": ("x.jpg", (root / rel["photo1"]).read_bytes(), "image/jpeg")})
    matches = r.json()["matches"]
    assert matches[0]["exact"] and {loc["path"] for loc in matches[0]["locations"]} == {rel["photo1"], rel["dup1"]}

    buf = io.BytesIO()
    with Image.open(root / rel["photo2"]) as im:
        im.resize((640, 480)).save(buf, "JPEG", quality=60)
    matches = api.post("/api/where", files={"file": ("emailed.jpg", buf.getvalue(), "image/jpeg")}).json()["matches"]
    assert not any(m["exact"] for m in matches)
    assert rel["photo2"] in {loc["path"] for m in matches for loc in m["locations"]}


def test_folder_browser(scanned):
    api, vid, root, rel = scanned
    listing = api.get("/api/fs", params={"path": str(root)}).json()
    assert {"photos", "broken"} <= {d["name"] for d in listing["dirs"]}
    assert listing["volume"]["id"] == vid
    assert api.get("/api/fs", params={"path": str(root / "nope")}).status_code == 400


def test_browsing_a_drive_from_another_catalog_does_not_register_it(api, drive, tmp_path):
    from imagelib import volumes
    from imagelib.catalog import Catalog

    root, rel = drive
    volumes.init_volume(root, "Elsewhere", Catalog(tmp_path / "other").machine_id)
    listing = api.get("/api/fs", params={"path": str(root)}).json()
    assert listing["volume"] is None
    assert api.get("/api/volumes").json() == []


def test_pause_and_resume_through_the_api(api, drive):
    root, rel = drive
    vid = api.post("/api/volumes", json={"path": str(root)}).json()["id"]
    api.post("/api/scan", json={"volume_ids": [vid]})
    api.post("/api/scan/pause")
    state = wait_for_scan(api)
    unfinished = api.get("/api/summary").json()["unfinished_scans"]
    # Either the pause landed (scan resumable) or the tiny library finished first.
    assert (state["targets"][0]["state"] == "paused") == bool(unfinished)
    if unfinished:
        api.post("/api/scan", json={"resume": True}).raise_for_status()
        wait_for_scan(api)
    summary = api.get("/api/summary").json()
    assert summary["pending"] == 0 and summary["files"] == 22 and not summary["unfinished_scans"]
    assert api.post("/api/scan", json={"resume": True}).status_code == 400  # nothing left to resume


def test_scanning_a_folder_by_path(api, drive):
    root, rel = drive
    api.post("/api/volumes", json={"path": str(root)})
    api.post("/api/scan", json={"path": str(root / "photos")}).raise_for_status()
    wait_for_scan(api)
    assert api.get("/api/summary").json()["files"] == 8


def test_event_stream_delivers_scan_state_through_the_middleware(tmp_path):
    import json
    import threading

    import uvicorn
    import httpx2 as httpx

    app = create_app(tmp_path / "catalog")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/events", timeout=10) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            for line in r.iter_lines():
                if line.startswith("data: "):
                    state = json.loads(line[6:])
                    break
        assert state["running"] is False and state["targets"] == []
    finally:
        server.should_exit = True
        thread.join(10)
