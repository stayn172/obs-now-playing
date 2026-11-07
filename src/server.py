"""aiohttp server: pages, JSON API, cover art, and Server-Sent Events.

Binds to 127.0.0.1 only. A Host-header check blocks DNS-rebinding, and
state-changing requests must be JSON from a local origin, so random websites
can't change settings through the user's browser.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from aiohttp import web

from . import settings as settings_mod
from .demo import DEMO_TRACKS, PLACEHOLDER_SVG, PLACEHOLDER_VERSION, demo_art, demo_now_playing
from .media.base import PlaybackStatus, art_hash, epoch_ms
from .media.manager import PlayerManager
from .notices import Notices
from .platform import paths
from .settings import SettingsStore
from .version import __version__

log = logging.getLogger(__name__)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
SSE_HEARTBEAT = 15.0
OVERLAY_SIZE = {"width": 640, "height": 200}

K_MANAGER: web.AppKey[PlayerManager] = web.AppKey("manager", PlayerManager)
K_SETTINGS: web.AppKey[SettingsStore] = web.AppKey("settings", SettingsStore)
K_HUB: web.AppKey["EventHub"] = web.AppKey("hub")
K_NOTICES: web.AppKey[Notices] = web.AppKey("notices", Notices)


@dataclass
class AppControl:
    """How the running app was started, and how to stop it (for /api/quit)."""
    launched_by: str = "user"  # "user" | "autostart" | "obs"
    quit: Callable[[], None] = lambda: None


K_CONTROL: web.AppKey[AppControl] = web.AppKey("control", AppControl)


# ------------------------------------------------------------------- payloads

def now_playing_payload(manager: PlayerManager) -> dict:
    now = epoch_ms()
    np = manager.now_playing()
    d = None
    if np is not None:
        d = np.to_dict(now)
        d["art_url"] = f"/api/art?v={np.art_version}" if np.art_version else None
    return {
        "now_playing": d,
        "selected_player": manager.selected_player,
        "selected_name": manager.display_name(manager.selected_player),
        "running": np is not None,
        "server_time_ms": now,
    }


def players_payload(manager: PlayerManager, obs_clients: int = 0) -> dict:
    b = manager.backend
    return {
        "selected_player": manager.selected_player,
        "selected_name": manager.display_name(manager.selected_player),
        "running": manager.active() is not None,
        "players": [p.to_dict() for p in manager.players()],
        "backend": {"name": b.name, "status": b.status, "error": b.error},
        # OBS Browser Sources currently connected (OBS puts "OBS/<version>"
        # in its user agent). 0 usually means the source needs a refresh.
        "obs_connected": obs_clients,
    }


def settings_payload(store: SettingsStore) -> dict:
    return {
        "settings": store.all(),
        "schema": settings_mod.schema_json(),
        "schema_version": settings_mod.SCHEMA_VERSION,
        "overlay_size": OVERLAY_SIZE,
        "obs_script_path": paths.obs_script_path(),
        "version": __version__,
    }


def notices_payload(notices: Notices) -> dict:
    return {"notices": notices.all()}


def _np_signature(payload: dict) -> Any:
    """What counts as a change worth pushing (ignores the clock ticking)."""
    np = payload.get("now_playing")
    if not np:
        return (payload["selected_player"], None)
    anchor = np["position_ms"]
    if np["status"] == PlaybackStatus.PLAYING.value:
        anchor = np["position_at_ms"] - np["position_ms"]  # track start time
    keep = {k: v for k, v in np.items() if k not in ("position_ms", "position_at_ms")}
    return (payload["selected_player"], json.dumps(keep, sort_keys=True), anchor // 1500)


# ------------------------------------------------------------------------ SSE

class _Client:
    __slots__ = ("pending", "wake", "is_obs")

    def __init__(self, is_obs: bool = False) -> None:
        self.is_obs = is_obs
        self.pending: dict[str, str] = {}
        self.wake = asyncio.Event()


class EventHub:
    """Fan-out to SSE clients. Each client only holds the latest payload per
    event type, so slow clients coalesce instead of buffering without bound."""

    def __init__(self) -> None:
        self.clients: set[_Client] = set()
        self._sigs: dict[str, Any] = {}
        self.closing = False

    def publish(self, event: str, payload: dict, signature: Any = None) -> bool:
        sig = signature if signature is not None else json.dumps(payload, sort_keys=True)
        if self._sigs.get(event) == sig:
            return False
        self._sigs[event] = sig
        data = json.dumps(payload, separators=(",", ":"))
        for c in self.clients:
            c.pending[event] = data
            c.wake.set()
        return True

    def obs_count(self) -> int:
        return sum(1 for c in self.clients if c.is_obs)

    def close(self) -> None:
        self.closing = True
        for c in self.clients:
            c.wake.set()


async def _sse_write(resp: web.StreamResponse, event: str, data: str) -> None:
    await resp.write(f"event: {event}\ndata: {data}\n\n".encode())


def is_obs_browser(request: web.Request) -> bool:
    """The overlay says so when OBS's window.obsstudio exists; the user agent
    ("... OBS/<version>") is a fallback."""
    return request.query.get("client") == "obs" or "OBS/" in request.headers.get("User-Agent", "")


async def handle_events(request: web.Request) -> web.StreamResponse:
    hub = request.app[K_HUB]
    manager = request.app[K_MANAGER]
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    client = _Client(is_obs=is_obs_browser(request))
    hub.clients.add(client)
    if client.is_obs:
        hub.publish("players", players_payload(manager, hub.obs_count()))
    try:
        await resp.write(b"retry: 2000\n\n")
        # Fresh full state on connect.
        await _sse_write(resp, "settings", json.dumps(settings_payload(request.app[K_SETTINGS])))
        await _sse_write(resp, "players", json.dumps(players_payload(manager, hub.obs_count())))
        await _sse_write(resp, "now-playing", json.dumps(now_playing_payload(manager)))
        await _sse_write(resp, "notices", json.dumps(notices_payload(request.app[K_NOTICES])))
        while not hub.closing:
            try:
                await asyncio.wait_for(client.wake.wait(), timeout=SSE_HEARTBEAT)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
                continue
            client.wake.clear()
            pending, client.pending = client.pending, {}
            for event, data in pending.items():
                await _sse_write(resp, event, data)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        hub.clients.discard(client)
        if client.is_obs and not hub.closing:
            hub.publish("players", players_payload(manager, hub.obs_count()))
    return resp


# ------------------------------------------------------------------ handlers

def _page(name: str) -> web.StreamResponse:
    path = os.path.join(paths.web_dir(), name)
    if not os.path.exists(path):
        raise web.HTTPNotFound(text=f"{name} not found")
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def handle_dashboard(request: web.Request) -> web.StreamResponse:
    return _page("dashboard.html")


async def handle_overlay(request: web.Request) -> web.StreamResponse:
    return _page("overlay.html")


async def handle_now_playing(request: web.Request) -> web.Response:
    return web.json_response(now_playing_payload(request.app[K_MANAGER]),
                             headers={"Cache-Control": "no-store"})


async def handle_players(request: web.Request) -> web.Response:
    return web.json_response(players_payload(request.app[K_MANAGER], request.app[K_HUB].obs_count()),
                             headers={"Cache-Control": "no-store"})


async def handle_art(request: web.Request) -> web.Response:
    q = request.query
    if "demo" in q:
        try:
            idx = int(q["demo"]) % len(DEMO_TRACKS)
        except ValueError:
            raise web.HTTPBadRequest(text="bad demo index")
        data, mime = demo_art(idx), "image/svg+xml"
        version = art_hash(data)
    else:
        art = await request.app[K_MANAGER].get_art()
        if art is None:
            data, mime, version = PLACEHOLDER_SVG, "image/svg+xml", PLACEHOLDER_VERSION
        else:
            data, mime, version = art.data, art.mime, art.version

    etag = f'"{version}"'
    # URLs carrying the matching ?v= are content-addressed: cache forever.
    # The bare /api/art always revalidates since its content changes.
    cache = "public, max-age=31536000, immutable" if q.get("v") == version else "no-cache"
    headers = {"ETag": etag, "Cache-Control": cache}
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    return web.Response(body=data, content_type=mime, headers=headers)


async def handle_get_settings(request: web.Request) -> web.Response:
    return web.json_response(settings_payload(request.app[K_SETTINGS]), headers={"Cache-Control": "no-store"})


async def _json_body(request: web.Request) -> Any:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="expected application/json")
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="invalid JSON")


async def handle_post_settings(request: web.Request) -> web.Response:
    body = await _json_body(request)
    store = request.app[K_SETTINGS]
    settings, errors, changed = store.update(body)
    status = 400 if errors.get("_") else 200
    return web.json_response({
        "settings": settings,
        "errors": errors,
        "changed": sorted(changed),
        "restart_required": sorted(changed & settings_mod.RESTART_REQUIRED),
    }, status=status)


async def handle_reset_settings(request: web.Request) -> web.Response:
    await _json_body(request)
    settings, changed = request.app[K_SETTINGS].reset()
    return web.json_response({"settings": settings, "errors": {}, "changed": sorted(changed),
                              "restart_required": []})


async def handle_notices(request: web.Request) -> web.Response:
    return web.json_response(notices_payload(request.app[K_NOTICES]), headers={"Cache-Control": "no-store"})


async def handle_dismiss_notice(request: web.Request) -> web.Response:
    body = await _json_body(request)
    notices = request.app[K_NOTICES]
    nid = body.get("id") if isinstance(body, dict) else None
    if isinstance(nid, str):
        match = [n for n in notices.all() if n["id"] == nid and n["dismissible"]]
        if match:
            notices.clear(nid)
    return web.json_response(notices_payload(notices))


async def handle_quit(request: web.Request) -> web.Response:
    """Stop the app. ``{"only_if_launched_by": "obs"}`` makes it conditional,
    so closing OBS never closes an app the user (or autostart) started."""
    body = await _json_body(request)
    control = request.app[K_CONTROL]
    only_if = body.get("only_if_launched_by") if isinstance(body, dict) else None
    quitting = only_if is None or only_if == control.launched_by
    if quitting:
        log.info("quit requested via API (launched by %s)", control.launched_by)
        # Let this response go out before the server shuts down.
        asyncio.get_running_loop().call_later(0.1, control.quit)
    return web.json_response({"quitting": quitting, "launched_by": control.launched_by})


async def handle_demo(request: web.Request) -> web.Response:
    now = epoch_ms()
    return web.json_response({
        "now_playing": demo_now_playing(now),
        "selected_player": "demo",
        "selected_name": "Demo",
        "running": True,
        "server_time_ms": now,
    }, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- middleware

def _hostname(netloc: str) -> str:
    try:
        return urlsplit("//" + netloc).hostname or ""
    except ValueError:
        return ""


@web.middleware
async def local_only(request: web.Request, handler):
    if _hostname(request.host or "") not in LOCAL_HOSTS:
        raise web.HTTPForbidden(text="forbidden host")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and origin != "null" and (urlsplit(origin).hostname or "") not in LOCAL_HOSTS:
            raise web.HTTPForbidden(text="forbidden origin")
    return await handler(request)


# --------------------------------------------------------------------- wiring

def create_app(manager: PlayerManager, store: SettingsStore, notices: Notices | None = None,
               control: AppControl | None = None) -> web.Application:
    app = web.Application(middlewares=[local_only], client_max_size=64 * 1024)
    hub = EventHub()
    notices = notices if notices is not None else Notices()
    app[K_MANAGER], app[K_SETTINGS], app[K_HUB], app[K_NOTICES] = manager, store, hub, notices
    app[K_CONTROL] = control if control is not None else AppControl()
    notices.add_listener(lambda: hub.publish("notices", notices_payload(notices)))

    def on_manager_change() -> None:
        np = now_playing_payload(manager)
        hub.publish("now-playing", np, _np_signature(np))
        hub.publish("players", players_payload(manager, hub.obs_count()))

    def on_settings_change(settings: dict, changed: set) -> None:
        if "selected_player" in changed:
            manager.set_selected(settings["selected_player"])  # triggers on_manager_change
        hub.publish("settings", settings_payload(store))

    manager.add_listener(on_manager_change)
    store.add_listener(on_settings_change)
    if manager.selected_player != store.get("selected_player"):
        manager.set_selected(store.get("selected_player"))

    async def on_shutdown(_app: web.Application) -> None:
        hub.close()

    app.on_shutdown.append(on_shutdown)

    r = app.router
    r.add_get("/", handle_dashboard)
    r.add_get("/overlay", handle_overlay)
    r.add_get("/api/now-playing", handle_now_playing)
    r.add_get("/api/players", handle_players)
    r.add_get("/api/art", handle_art)
    r.add_get("/api/settings", handle_get_settings)
    r.add_post("/api/settings", handle_post_settings)
    r.add_post("/api/settings/reset", handle_reset_settings)
    r.add_get("/api/events", handle_events)
    r.add_get("/api/demo", handle_demo)
    r.add_get("/api/notices", handle_notices)
    r.add_post("/api/notices/dismiss", handle_dismiss_notice)
    r.add_post("/api/quit", handle_quit)
    assets = os.path.join(paths.web_dir(), "assets")
    if os.path.isdir(assets):
        r.add_static("/assets", assets, append_version=False)

    async def revalidate_assets(request: web.Request, response: web.StreamResponse) -> None:
        # Without this, browsers (and OBS) heuristically cache CSS/JS and keep
        # showing the old version after an update. no-cache still allows a
        # cheap 304 via ETag/Last-Modified.
        if request.path.startswith("/assets/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-cache"

    app.on_response_prepare.append(revalidate_assets)
    return app


async def start_server(app: web.Application, port: int, host: str = "127.0.0.1") -> web.AppRunner:
    """Start listening; raises OSError if the port is taken."""
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2.0)
    await runner.setup()
    site = web.TCPSite(runner, host, port, reuse_address=False)
    try:
        await site.start()
    except OSError:
        await runner.cleanup()
        raise
    return runner
