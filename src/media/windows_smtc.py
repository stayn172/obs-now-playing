"""Windows backend: Global System Media Transport Controls (SMTC).

Tracks every SMTC session (unfiltered); selection happens in the manager.
WinRT event handlers fire on worker threads, so they only poke an asyncio
Event; all reads happen on the loop in ``_refresh_all``. A 1s safety poll
covers apps that forget to raise events.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

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

MAX_ART_BYTES = 10 * 1024 * 1024
ART_CACHE_SIZE = 32
ART_MAX_ATTEMPTS = 5
POLL_INTERVAL = 1.0
DEBOUNCE = 0.05

# GlobalSystemMediaTransportControlsSessionPlaybackStatus values
_PB_PLAYING = 4
_PB_PAUSED = 5


@dataclass
class _Tracked:
    session: Any
    tokens: list = field(default_factory=list)
    snapshot: Optional[PlayerSnapshot] = None
    art_key: Optional[tuple] = None
    art_attempts: int = 0
    art_task: Optional[asyncio.Task] = None


def _status(pb_status: Any) -> PlaybackStatus:
    v = int(pb_status) if pb_status is not None else 0
    if v == _PB_PLAYING:
        return PlaybackStatus.PLAYING
    if v == _PB_PAUSED:
        return PlaybackStatus.PAUSED
    return PlaybackStatus.STOPPED


def _td_ms(td: Any) -> int:
    try:
        return max(0, int(td.total_seconds() * 1000))
    except Exception:
        return 0


def _dt_ms(dt: Any) -> int:
    """WinRT DateTime -> epoch ms; 0 for unset (year 1601) values."""
    try:
        if dt is None or dt.year < 2000:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


class WindowsSMTCBackend(MediaBackend):
    name = "windows-smtc"

    def __init__(self, poll_interval: float = POLL_INTERVAL) -> None:
        super().__init__()
        self._poll_interval = poll_interval
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake = asyncio.Event()
        self._resubscribe = True
        self._task: Optional[asyncio.Task] = None
        self._mgr: Any = None
        self._mgr_token: Any = None
        self._tracked: dict[str, _Tracked] = {}
        self._art_cache: "OrderedDict[tuple, Art]" = OrderedDict()
        self._last_emitted: Any = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name="smtc-backend")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._unsubscribe_all()
        if self._mgr is not None and self._mgr_token is not None:
            try:
                self._mgr.remove_sessions_changed(self._mgr_token)
            except Exception:
                pass
        self._mgr = None

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                if self._mgr is None:
                    await self._connect()
                await self._refresh_all()
                if self.status != "ok":
                    self.status, self.error = "ok", None
                    self._emit()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never die: report and retry
                log.exception("SMTC refresh failed")
                self.status, self.error = "error", f"{type(e).__name__}: {e}"
                self._emit()
                self._unsubscribe_all()
                self._mgr = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
                await asyncio.sleep(DEBOUNCE)  # coalesce event bursts
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _connect(self) -> None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )

        self._mgr = await SessionManager.request_async()
        self._mgr_token = self._mgr.add_sessions_changed(self._on_sessions_changed)
        self._resubscribe = True
        log.info("Connected to SMTC")

    # --------------------------------------------------------------- events

    def _poke(self, *_: Any) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._wake.set)

    def _on_sessions_changed(self, *_: Any) -> None:
        self._resubscribe = True
        self._poke()

    def _subscribe(self, t: _Tracked) -> None:
        s = t.session
        try:
            t.tokens = [
                ("media_properties", s.add_media_properties_changed(self._poke)),
                ("playback_info", s.add_playback_info_changed(self._poke)),
                ("timeline_properties", s.add_timeline_properties_changed(self._poke)),
            ]
        except Exception:
            log.debug("subscribe failed", exc_info=True)

    def _unsubscribe(self, t: _Tracked) -> None:
        for kind, token in t.tokens:
            try:
                getattr(t.session, f"remove_{kind}_changed")(token)
            except Exception:
                pass
        t.tokens = []
        if t.art_task and not t.art_task.done():
            t.art_task.cancel()

    def _unsubscribe_all(self) -> None:
        for t in self._tracked.values():
            self._unsubscribe(t)
        self._tracked.clear()

    # -------------------------------------------------------------- reading

    async def _refresh_all(self) -> None:
        sessions = list(self._mgr.get_sessions())
        current: dict[str, Any] = {}
        for s in sessions:
            try:
                aumid = s.source_app_user_model_id or "unknown"
            except Exception:
                continue
            pid, n = aumid, 2
            while pid in current:  # several sessions from one app (e.g. tabs)
                pid, n = f"{aumid}#{n}", n + 1
            current[pid] = s

        if self._resubscribe:
            # Session objects can't be compared across enumerations, so on
            # any sessions_changed we just rebuild the subscriptions.
            self._resubscribe = False
            old = self._tracked
            self._tracked = {}
            for pid, s in current.items():
                t = _Tracked(session=s)
                prev = old.pop(pid, None)
                if prev:
                    self._unsubscribe(prev)
                    t.snapshot, t.art_key, t.art_attempts = prev.snapshot, prev.art_key, prev.art_attempts
                self._subscribe(t)
                self._tracked[pid] = t
            for prev in old.values():
                self._unsubscribe(prev)
        else:
            for pid in list(self._tracked):
                if pid not in current:
                    self._unsubscribe(self._tracked.pop(pid))
            for pid, s in current.items():
                if pid in self._tracked:
                    self._tracked[pid].session = s
                else:
                    t = _Tracked(session=s)
                    self._subscribe(t)
                    self._tracked[pid] = t

        for pid, t in list(self._tracked.items()):
            try:
                await self._read_session(pid, t)
            except Exception:
                # A single crashed/closing app must not affect the others.
                log.debug("reading session %s failed", pid, exc_info=True)

        sig = self._signature()
        if sig != self._last_emitted:
            self._last_emitted = sig
            self._emit()

    async def _read_session(self, pid: str, t: _Tracked) -> None:
        s = t.session
        props = await s.try_get_media_properties_async()
        pb = s.get_playback_info()
        tl = s.get_timeline_properties()

        aumid = pid.split("#", 1)[0]
        key, name, is_browser = classify_player(aumid)
        status = _status(pb.playback_status if pb else None)

        title = (props.title or "") if props else ""
        artist = (props.artist or "") if props else ""
        album = (props.album_title or "") if props else ""
        if props and not artist:
            artist = props.album_artist or ""

        now = epoch_ms()
        duration = position = position_at = 0
        if tl is not None:
            duration = max(0, _td_ms(tl.end_time) - _td_ms(tl.start_time))
            position = _td_ms(tl.position)
            position_at = _dt_ms(tl.last_updated_time) or now
            position_at = min(position_at, now)
            if duration and position > duration:
                position = duration

        track_key = (pid, title, artist, album)
        art = self._art_cache.get(track_key)
        if t.art_key != track_key:
            t.art_key, t.art_attempts = track_key, 0
            if t.art_task and not t.art_task.done():
                t.art_task.cancel()
        if (
            art is None
            and props is not None
            and props.thumbnail is not None
            and t.art_attempts < ART_MAX_ATTEMPTS
            and (t.art_task is None or t.art_task.done())
        ):
            t.art_attempts += 1
            t.art_task = asyncio.create_task(self._fetch_art(track_key, props.thumbnail))

        t.snapshot = PlayerSnapshot(
            info=PlayerInfo(
                player_id=pid, display_name=name, status=status, is_browser=is_browser, key=key
            ),
            now_playing=NowPlaying(
                player_id=pid,
                player_name=name,
                title=title,
                artist=artist,
                album=album,
                status=status,
                position_ms=position,
                duration_ms=duration,
                position_at_ms=position_at,
                art_version=art.version if art else "",
                is_ad=detect_ad(key, title, artist),
            ),
        )

    async def _fetch_art(self, track_key: tuple, ref: Any) -> None:
        try:
            data, mime = await read_thumbnail(ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("thumbnail read failed for %s", track_key, exc_info=True)
            return
        if not data:
            return
        self._art_cache[track_key] = Art(data=data, mime=sniff_mime(data, mime or "image/jpeg"))
        self._art_cache.move_to_end(track_key)
        while len(self._art_cache) > ART_CACHE_SIZE:
            self._art_cache.popitem(last=False)
        self._wake.set()  # re-read so art_version propagates

    def _signature(self) -> tuple:
        out = []
        for pid in sorted(self._tracked):
            snap = self._tracked[pid].snapshot
            if snap is None:
                continue
            np = snap.now_playing
            # Position is excluded when playing (it moves every poll); seeks
            # still show up via a jump vs. the extrapolated value.
            out.append((pid, np.title, np.artist, np.album, np.status, np.duration_ms,
                        np.art_version, np.position_ms // 2000 if np.status != PlaybackStatus.PLAYING
                        else (np.position_ms - np.position_at_ms) // 2000))
        return tuple(out)

    # ------------------------------------------------------------ interface

    def snapshot(self) -> list[PlayerSnapshot]:
        return [t.snapshot for t in self._tracked.values() if t.snapshot is not None]

    async def get_art(self, player_id: str) -> Optional[Art]:
        t = self._tracked.get(player_id)
        if not t or not t.art_key:
            return None
        return self._art_cache.get(t.art_key)


async def read_thumbnail(ref: Any) -> tuple[bytes, str]:
    """Read an IRandomAccessStreamReference into bytes."""
    from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions

    stream = await ref.open_read_async()
    try:
        size = int(stream.size)
        if size <= 0 or size > MAX_ART_BYTES:
            return b"", ""
        buf = Buffer(size)
        result = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
        length = int(result.length)
        reader = DataReader.from_buffer(result)
        data = bytearray(length)
        reader.read_bytes(data)
        mime = ""
        try:
            mime = stream.content_type or ""
        except Exception:
            pass
        return bytes(data), mime
    finally:
        try:
            stream.close()
        except Exception:
            pass
