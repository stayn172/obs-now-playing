import asyncio
import json

import pytest

from src.media.base import Art, PlaybackStatus as S
from src.media.manager import PlayerManager
from src.server import K_HUB, create_app
from src.settings import SettingsStore

from .fakes import FakeBackend, snap


@pytest.fixture
def env(tmp_path):
    backend = FakeBackend()
    store = SettingsStore(str(tmp_path / "settings.json"))
    manager = PlayerManager(backend, store.get("selected_player"))
    return backend, store, manager


@pytest.fixture
async def client(aiohttp_client, env):
    backend, store, manager = env
    return await aiohttp_client(create_app(manager, store))


async def test_now_playing_selected_running(client, env):
    backend, *_ = env
    backend.set(snap("Spotify.exe", title="Hello", art_version="abc"), snap("Chrome", title="YouTube"))
    d = await (await client.get("/api/now-playing")).json()
    assert d["running"] and d["now_playing"]["title"] == "Hello"
    assert d["now_playing"]["art_url"] == "/api/art?v=abc"
    assert d["selected_player"] == "spotify"


async def test_now_playing_not_running_is_empty(client, env):
    env[0].set(snap("Chrome", title="YouTube"))
    d = await (await client.get("/api/now-playing")).json()
    assert d["running"] is False and d["now_playing"] is None and d["selected_name"] == "Spotify"


async def test_post_settings_switches_player(client, env):
    backend, store, _ = env
    backend.set(snap("Spotify.exe"), snap("Chrome", title="Video"))
    r = await client.post("/api/settings", json={"selected_player": "chrome", "scale": 0.1, "bogus": 1})
    d = await r.json()
    assert r.status == 200 and d["changed"] == ["scale", "selected_player"]
    assert d["settings"]["scale"] == 0.25
    np = await (await client.get("/api/now-playing")).json()
    assert np["now_playing"]["title"] == "Video"
    assert store.get("selected_player") == "chrome"


async def test_post_settings_reports_errors(client):
    r = await client.post("/api/settings", json={"font": "Nope", "port": 9000})
    d = await r.json()
    assert r.status == 200 and "font" in d["errors"] and d["restart_required"] == ["port"]
    r = await client.post("/api/settings", json=[1])
    assert r.status == 400


async def test_settings_payload_points_to_obs_script(client):
    import os
    d = await (await client.get("/api/settings")).json()
    assert d["obs_script_path"].endswith("obs-now-playing.lua") and os.path.exists(d["obs_script_path"])


async def test_reset(client):
    await client.post("/api/settings", json={"scale": 0.5})
    d = await (await client.post("/api/settings/reset", json={})).json()
    assert d["settings"]["scale"] == 1.0


async def test_players_list(client, env):
    env[0].set(snap("Spotify.exe", S.PAUSED), snap("Chrome"), snap("Chrome#2"))
    d = await (await client.get("/api/players")).json()
    keys = [p["key"] for p in d["players"]]
    assert keys[0] == "spotify" and "chrome" in keys
    assert d["backend"]["status"] == "ok"


async def test_art_etag_and_placeholder(client, env):
    backend, *_ = env
    r = await client.get("/api/art")
    assert r.status == 200 and r.content_type == "image/svg+xml"  # placeholder
    backend.art["Spotify.exe"] = art = Art(b"\xff\xd8fakejpeg")
    backend.set(snap("Spotify.exe", art_version=art.version))
    r = await client.get("/api/art")
    assert r.content_type == "image/jpeg" and await r.read() == b"\xff\xd8fakejpeg"
    assert r.headers["Cache-Control"] == "no-cache"
    r2 = await client.get("/api/art", headers={"If-None-Match": r.headers["ETag"]})
    assert r2.status == 304
    r3 = await client.get(f"/api/art?v={art.version}")
    assert "immutable" in r3.headers["Cache-Control"]


async def test_demo(client):
    d = await (await client.get("/api/demo")).json()
    assert d["now_playing"]["status"] == "playing" and d["now_playing"]["title"]
    r = await client.get(d["now_playing"]["art_url"])
    assert r.status == 200 and r.content_type == "image/svg+xml"


async def test_rejects_foreign_host_and_origin(client):
    r = await client.get("/api/settings", headers={"Host": "evil.example:8765"})
    assert r.status == 403
    r = await client.post("/api/settings", json={"scale": 2}, headers={"Origin": "https://evil.example"})
    assert r.status == 403
    r = await client.post("/api/settings", data="scale=2",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status == 415


async def test_pages_served(client):
    for path in ("/", "/overlay"):
        r = await client.get(path)
        assert r.status == 200 and "text/html" in r.content_type
    html = await (await client.get("/overlay")).text()
    assert "/assets/overlay.js" in html and "/assets/fonts.css" in html
    html = await (await client.get("/")).text()
    assert "/assets/dashboard.js" in html and "Copy Browser Source URL" in html


async def test_overlay_assets_served(client):
    for path, ctype in [("/assets/overlay.js", "javascript"), ("/assets/overlay.css", "text/css"),
                        ("/assets/fonts.css", "text/css"), ("/assets/placeholder.svg", "image/svg+xml"), ("/assets/dashboard.js", "javascript"), ("/assets/dashboard.css", "text/css"),
                        ("/assets/fonts/inter-latin-wght.woff2", "font/woff2")]:
        r = await client.get(path)
        assert r.status == 200 and ctype in r.content_type, path


async def test_fonts_css_references_existing_files():
    import os
    import re
    from src.platform import paths
    css = open(os.path.join(paths.web_dir(), "assets", "fonts.css"), encoding="utf-8").read()
    files = re.findall(r'url\("fonts/([^"]+)"\)', css)
    assert len(files) == 12
    for f in files:
        assert os.path.exists(os.path.join(paths.web_dir(), "assets", "fonts", f)), f


async def _read_event(resp, timeout=2.0):
    event, data = None, None
    while True:
        line = (await asyncio.wait_for(resp.content.readline(), timeout)).decode().rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
        elif line == "" and event:
            return event, data


async def _next_of(resp, name):
    while True:
        ev, data = await _read_event(resp)
        if ev == name:
            return data


async def test_sse_initial_state_and_push(client, env):
    backend, *_ = env
    backend.set(snap("Spotify.exe", title="First"))
    resp = await client.get("/api/events")
    assert resp.headers["Content-Type"] == "text/event-stream"
    got = dict([await _read_event(resp) for _ in range(3)])
    assert set(got) == {"settings", "players", "now-playing"}
    assert got["now-playing"]["now_playing"]["title"] == "First"

    backend.set(snap("Spotify.exe", title="Second"))
    assert (await _next_of(resp, "now-playing"))["now_playing"]["title"] == "Second"

    await client.post("/api/settings", json={"title_size": 44})
    assert (await _next_of(resp, "settings"))["settings"]["title_size"] == 44
    resp.close()


async def test_notices_over_sse_and_dismiss(client):
    from src.server import K_NOTICES
    notices = client.server.app[K_NOTICES]
    resp = await client.get("/api/events")
    assert (await _next_of(resp, "notices")) == {"notices": []}
    notices.set("port-moved", "warn", "moved")
    notices.set("sticky", "error", "can't dismiss", dismissible=False)
    got = await _next_of(resp, "notices")
    assert [n["id"] for n in got["notices"]] == ["port-moved", "sticky"]
    d = await (await client.post("/api/notices/dismiss", json={"id": "sticky"})).json()
    assert len(d["notices"]) == 2  # not dismissible
    d = await (await client.post("/api/notices/dismiss", json={"id": "port-moved"})).json()
    assert [n["id"] for n in d["notices"]] == ["sticky"]
    resp.close()


async def test_sse_dedupes_unchanged_state(client, env):
    backend, *_ = env
    backend.set(snap("Spotify.exe", title="Same"))
    hub = client.server.app[K_HUB]
    published = []
    orig = hub.publish
    hub.publish = lambda *a, **k: published.append(orig(*a, **k)) or published[-1]
    backend.set(snap("Spotify.exe", title="Same"))  # identical state
    assert published and not any(published)


async def test_obs_connection_is_reported(client, env):
    backend, *_ = env
    backend.set(snap("Spotify.exe"))
    d = await (await client.get("/api/players")).json()
    assert d["obs_connected"] == 0

    dash = await client.get("/api/events")  # a normal browser (the dashboard)
    assert (await _next_of(dash, "players"))["obs_connected"] == 0
    obs_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.0.0 Safari/537.36 OBS/31.0.2"
    obs = await client.get("/api/events", headers={"User-Agent": obs_ua})
    assert (await _next_of(dash, "players"))["obs_connected"] == 1  # pushed to the dashboard
    assert (await (await client.get("/api/players")).json())["obs_connected"] == 1

    obs.close()                      # OBS source removed / OBS closed
    backend.set(snap("Spotify.exe", title="next"))  # next write notices the gone client
    for _ in range(5):
        got = await _next_of(dash, "players")
        if got["obs_connected"] == 0:
            break
    assert got["obs_connected"] == 0
    dash.close()


async def test_overlay_in_obs_identifies_itself(client):
    resp = await client.get("/api/events?client=obs")
    assert (await _next_of(resp, "players"))["obs_connected"] == 1
    resp.close()
