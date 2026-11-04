"""Linux backend: MPRIS over the D-Bus session bus (dbus-next).

Tracks every ``org.mpris.MediaPlayer2.*`` name. PropertiesChanged drives
updates; Position (which MPRIS never signals) is polled about once a second.
If the session bus is unavailable (containers, headless), the backend reports
a clear status and keeps retrying instead of crashing.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from .base import (
    Art,
    MediaBackend,
    NowPlaying,
    PlaybackStatus,
    PlayerInfo,
    PlayerSnapshot,
    classify_player,
    detect_ad,
    epoch_ms,
    sniff_mime,
)

log = logging.getLogger(__name__)

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
IFACE_ROOT = "org.mpris.MediaPlayer2"
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
IFACE_PROPS = "org.freedesktop.DBus.Properties"

POLL_INTERVAL = 1.0
ART_TIMEOUT = 5.0
MAX_ART_BYTES = 10 * 1024 * 1024
ART_CACHE_SIZE = 32


@dataclass
class _Player:
    bus_name: str
    player_id: str
    identity: str = ""
    props_iface: Any = None
    player_iface: Any = None
    metadata: dict = field(default_factory=dict)
    status: PlaybackStatus = PlaybackStatus.STOPPED
    position_ms: int = 0
    position_at_ms: int = 0
    art_url: str = ""


def _v(x: Any) -> Any:
    """Unwrap a dbus-next Variant."""
    return getattr(x, "value", x)


def _status(s: Any) -> PlaybackStatus:
    s = str(_v(s) or "").lower()
    if s == "playing":
        return PlaybackStatus.PLAYING
    if s == "paused":
        return PlaybackStatus.PAUSED
    return PlaybackStatus.STOPPED


def player_id_from_bus_name(bus_name: str) -> str:
    """'org.mpris.MediaPlayer2.firefox.instance_1_42' -> 'firefox'.

    Instance suffixes change on every launch, so they're stripped to keep the
    user's selection stable.
    """
    pid = bus_name[len(MPRIS_PREFIX):] if bus_name.startswith(MPRIS_PREFIX) else bus_name
    return re.sub(r"\.instance_?[\d_]+$|\.instance\d+$", "", pid)


def normalize_art_url(url: str) -> str:
    # Old Spotify clients report a dead open.spotify.com/image/ host.
    return re.sub(r"^https?://open\.spotify\.com/image/", "https://i.scdn.co/image/", url or "")


class LinuxMPRISBackend(MediaBackend):
    name = "linux-mpris"

    def __init__(self, poll_interval: float = POLL_INTERVAL) -> None:
        super().__init__()
        self._poll_interval = poll_interval
        self._bus: Any = None
        self._players: dict[str, _Player] = {}  # by bus name
        self._task: Optional[asyncio.Task] = None
        self._art_cache: "OrderedDict[str, Art]" = OrderedDict()
        self._art_pending: dict[str, asyncio.Task] = {}
        self._http: Any = None
        self._last_sig: Any = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mpris-backend")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        for t in self._art_pending.values():
            t.cancel()
        if self._http is not None:
            await self._http.close()
        self._disconnect()

    def _disconnect(self) -> None:
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:
                pass
        self._bus = None
        self._players.clear()

    async def _run(self) -> None:
        backoff = 2.0
        while True:
            try:
                if self._bus is None:
                    await self._connect()
                    backoff = 2.0
                await self._poll_positions()
                self._maybe_emit()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if self._bus is None:
                    log.warning("D-Bus session bus unavailable: %s", msg)
                    self.status, self.error = "no media backend", f"D-Bus session bus unavailable ({msg})"
                else:
                    log.exception("MPRIS loop failed")
                    self.status, self.error = "error", msg
                self._disconnect()
                self._emit()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            await asyncio.sleep(self._poll_interval)

    async def _connect(self) -> None:
        from dbus_next import BusType
        from dbus_next.aio import MessageBus

        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.path.exists(
            f"/run/user/{os.getuid()}/bus"
        ):
            raise RuntimeError("no DBUS_SESSION_BUS_ADDRESS")

        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self._bus = bus
        intro = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
        dbus_obj = bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", intro)
        dbus_iface = dbus_obj.get_interface("org.freedesktop.DBus")
        dbus_iface.on_name_owner_changed(self._on_name_owner_changed)

        names = await dbus_iface.call_list_names()
        for n in names:
            if n.startswith(MPRIS_PREFIX):
                await self._add_player(n)
        self.status, self.error = "ok", None
        log.info("Connected to D-Bus session bus, %d MPRIS players", len(self._players))
        self._emit()

    # --------------------------------------------------------------- events

    def _on_name_owner_changed(self, name: str, old_owner: str, new_owner: str) -> None:
        if not name.startswith(MPRIS_PREFIX):
            return
        if old_owner and name in self._players:
            self._players.pop(name, None)
            self._maybe_emit(force=True)
        if new_owner:
            asyncio.ensure_future(self._add_player_safe(name))

    async def _add_player_safe(self, name: str) -> None:
        try:
            await self._add_player(name)
            self._maybe_emit(force=True)
        except Exception:
            log.debug("adding player %s failed", name, exc_info=True)

    async def _add_player(self, bus_name: str) -> None:
        bus = self._bus
        intro = await asyncio.wait_for(bus.introspect(bus_name, MPRIS_PATH), timeout=3)
        obj = bus.get_proxy_object(bus_name, MPRIS_PATH, intro)
        base_id = player_id_from_bus_name(bus_name)
        taken = {p.player_id for p in self._players.values()}
        pid, n = base_id, 2
        while pid in taken:
            pid, n = f"{base_id}#{n}", n + 1
        p = _Player(bus_name=bus_name, player_id=pid)

        try:
            root = obj.get_interface(IFACE_ROOT)
            p.identity = str(await root.get_identity())
        except Exception:
            p.identity = base_id
        try:
            p.player_iface = obj.get_interface(IFACE_PLAYER)
        except Exception:
            log.debug("%s has no Player interface", bus_name)
            return
        p.props_iface = obj.get_interface(IFACE_PROPS)
        p.props_iface.on_properties_changed(
            lambda iface, changed, invalidated, _bn=bus_name: self._on_props(_bn, iface, changed, invalidated)
        )
        try:
            p.player_iface.on_seeked(lambda pos, _bn=bus_name: self._on_seeked(_bn, pos))
        except Exception:
            pass

        all_props = await p.props_iface.call_get_all(IFACE_PLAYER)
        self._apply(p, all_props)
        self._players[bus_name] = p

    def _on_props(self, bus_name: str, iface: str, changed: dict, invalidated: list) -> None:
        p = self._players.get(bus_name)
        if not p or iface != IFACE_PLAYER:
            return
        self._apply(p, changed)
        if invalidated:
            asyncio.ensure_future(self._reload_props(p))
        self._maybe_emit()

    async def _reload_props(self, p: _Player) -> None:
        try:
            self._apply(p, await p.props_iface.call_get_all(IFACE_PLAYER))
            self._maybe_emit()
        except Exception:
            pass

    def _on_seeked(self, bus_name: str, pos_us: int) -> None:
        p = self._players.get(bus_name)
        if p:
            p.position_ms, p.position_at_ms = int(pos_us) // 1000, epoch_ms()
            self._maybe_emit(force=True)

    def _apply(self, p: _Player, props: dict) -> None:
        if "Metadata" in props:
            md = {k: _v(v) for k, v in (_v(props["Metadata"]) or {}).items()}
            if md.get("xesam:title") != p.metadata.get("xesam:title"):
                # New track: position restarts; the next poll corrects it.
                p.position_ms, p.position_at_ms = 0, epoch_ms()
            p.metadata = md
            url = normalize_art_url(str(md.get("mpris:artUrl") or ""))
            if url != p.art_url:
                p.art_url = url
                if url:
                    self._ensure_art(url)
        if "PlaybackStatus" in props:
            new = _status(props["PlaybackStatus"])
            if new != p.status:
                # Freeze/resume the extrapolation at the transition.
                p.position_ms = self._extrapolated(p)
                p.position_at_ms = epoch_ms()
                p.status = new
        if "Position" in props:
            p.position_ms, p.position_at_ms = int(_v(props["Position"]) or 0) // 1000, epoch_ms()

    @staticmethod
    def _extrapolated(p: _Player) -> int:
        if p.status != PlaybackStatus.PLAYING or not p.position_at_ms:
            return p.position_ms
        return p.position_ms + max(0, epoch_ms() - p.position_at_ms)

    async def _poll_positions(self) -> None:
        for p in list(self._players.values()):
            if p.status == PlaybackStatus.STOPPED:
                continue
            try:
                pos = await asyncio.wait_for(p.player_iface.get_position(), timeout=1)
                p.position_ms, p.position_at_ms = int(pos) // 1000, epoch_ms()
            except Exception:
                pass  # many players don't implement Position; extrapolate

    # ------------------------------------------------------------------ art

    def _ensure_art(self, url: str) -> None:
        if url in self._art_cache or url in self._art_pending:
            return
        task = asyncio.ensure_future(self._fetch_art(url))
        self._art_pending[url] = task
        task.add_done_callback(lambda _t, u=url: self._art_pending.pop(u, None))

    async def _fetch_art(self, url: str) -> None:
        try:
            data = await asyncio.wait_for(self._load_art_bytes(url), timeout=ART_TIMEOUT)
        except Exception as e:
            log.debug("art fetch failed for %s: %s", url[:120], e)
            return
        if not data:
            return
        self._art_cache[url] = Art(data=data, mime=sniff_mime(data))
        while len(self._art_cache) > ART_CACHE_SIZE:
            self._art_cache.popitem(last=False)
        self._maybe_emit(force=True)

    async def _load_art_bytes(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme == "file":
            path = unquote(parsed.path)

            def _read() -> bytes:
                if os.path.getsize(path) > MAX_ART_BYTES:
                    return b""
                with open(path, "rb") as f:
                    return f.read()

            return await asyncio.get_running_loop().run_in_executor(None, _read)
        if parsed.scheme == "data":
            header, _, payload = url.partition(",")
            raw = base64.b64decode(payload) if ";base64" in header else unquote(payload).encode()
            return raw if len(raw) <= MAX_ART_BYTES else b""
        if parsed.scheme in ("http", "https"):
            import aiohttp

            if self._http is None:
                self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=ART_TIMEOUT))
            async with self._http.get(url) as resp:
                resp.raise_for_status()
                if (resp.content_length or 0) > MAX_ART_BYTES:
                    return b""
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf += chunk
                    if len(buf) > MAX_ART_BYTES:
                        return b""
                return bytes(buf)
        return b""

    # ------------------------------------------------------------ interface

    def _build(self, p: _Player) -> PlayerSnapshot:
        md = p.metadata
        key, name, is_browser = classify_player(p.player_id, p.identity)
        artist = md.get("xesam:artist") or ""
        if isinstance(artist, (list, tuple)):
            artist = ", ".join(str(a) for a in artist)
        title = str(md.get("xesam:title") or "")
        art = self._art_cache.get(p.art_url) if p.art_url else None
        return PlayerSnapshot(
            info=PlayerInfo(player_id=p.player_id, display_name=name, status=p.status,
                            is_browser=is_browser, key=key),
            now_playing=NowPlaying(
                player_id=p.player_id,
                player_name=name,
                title=title,
                artist=str(artist),
                album=str(md.get("xesam:album") or ""),
                status=p.status,
                position_ms=p.position_ms,
                duration_ms=int(md.get("mpris:length") or 0) // 1000,
                position_at_ms=p.position_at_ms,
                art_version=art.version if art else "",
                is_ad=detect_ad(key, title, str(artist)),
            ),
        )

    def _maybe_emit(self, force: bool = False) -> None:
        snaps = self.snapshot()
        sig = tuple(
            (s.info.player_id, s.now_playing.title, s.now_playing.artist, s.now_playing.album,
             s.now_playing.status, s.now_playing.art_version, s.now_playing.duration_ms,
             (s.now_playing.position_ms - (s.now_playing.position_at_ms
              if s.now_playing.status == PlaybackStatus.PLAYING else 0)) // 2000)
            for s in snaps
        )
        if force or sig != self._last_sig:
            self._last_sig = sig
            self._emit()

    def snapshot(self) -> list[PlayerSnapshot]:
        return [self._build(p) for p in self._players.values()]

    async def get_art(self, player_id: str) -> Optional[Art]:
        for p in self._players.values():
            if p.player_id == player_id and p.art_url:
                return self._art_cache.get(p.art_url)
        return None
