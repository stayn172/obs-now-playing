"""Backend-agnostic media model and the MediaBackend interface.

Backends (Windows SMTC, Linux MPRIS) translate their native sessions into
``PlayerSnapshot`` objects and call ``on_change`` whenever something moves.
The manager (``manager.py``) does all selection logic on top of these.
"""
from __future__ import annotations

import abc
import hashlib
import re
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Optional


class PlaybackStatus(str, Enum):
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass
class PlayerInfo:
    player_id: str
    display_name: str
    status: PlaybackStatus
    is_browser: bool
    # Stable selection key: "spotify", "firefox", ... for known apps, the raw
    # player id otherwise. The user's choice is stored as this key so it
    # survives restarts and Store-vs-classic installs.
    key: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            self.key = self.player_id

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "player_id": self.player_id,
            "display_name": self.display_name,
            "status": self.status.value,
            "is_browser": self.is_browser,
        }


@dataclass
class NowPlaying:
    player_id: str
    player_name: str
    title: str = ""
    artist: str = ""
    album: str = ""
    status: PlaybackStatus = PlaybackStatus.STOPPED
    position_ms: int = 0
    duration_ms: int = 0
    # Wall-clock time (epoch ms) at which position_ms was valid. Clients
    # extrapolate from here while playing.
    position_at_ms: int = 0
    art_version: str = ""
    is_ad: bool = False

    def position_now_ms(self, now_ms: Optional[int] = None) -> int:
        """Position extrapolated to ``now_ms`` (clamped to duration)."""
        if self.status != PlaybackStatus.PLAYING or not self.position_at_ms:
            return self.position_ms
        now_ms = now_ms if now_ms is not None else epoch_ms()
        pos = self.position_ms + max(0, now_ms - self.position_at_ms)
        if self.duration_ms > 0:
            pos = min(pos, self.duration_ms)
        return pos

    def track_key(self) -> tuple:
        return (self.player_id, self.title, self.artist, self.album)

    def to_dict(self, now_ms: Optional[int] = None) -> dict:
        now_ms = now_ms if now_ms is not None else epoch_ms()
        d = asdict(self)
        d["status"] = self.status.value
        d["position_ms"] = self.position_now_ms(now_ms)
        d["position_at_ms"] = now_ms
        return d


@dataclass
class PlayerSnapshot:
    """Everything a backend knows about one player right now."""
    info: PlayerInfo
    now_playing: NowPlaying


@dataclass
class Art:
    data: bytes
    mime: str = "image/jpeg"
    version: str = field(default="")

    def __post_init__(self) -> None:
        if not self.version:
            self.version = art_hash(self.data)


ChangeCallback = Callable[[], None]


class MediaBackend(abc.ABC):
    """Interface implemented by each OS backend.

    Contract:
      * ``start`` never raises for "no media available" situations; it sets
        ``status`` instead and keeps retrying internally.
      * ``on_change`` is invoked on the asyncio loop thread.
      * A ~1s safety poll runs even when events are delivered.
    """

    name: str = "base"

    def __init__(self) -> None:
        self._callbacks: list[ChangeCallback] = []
        self.status: str = "starting"  # "ok" or a human-readable problem
        self.error: Optional[str] = None

    def add_listener(self, cb: ChangeCallback) -> None:
        self._callbacks.append(cb)

    def _emit(self) -> None:
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception:  # pragma: no cover - listener bugs must not kill the backend
                import logging
                logging.getLogger(__name__).exception("change listener failed")

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    def snapshot(self) -> list[PlayerSnapshot]:
        """Current state of every detected player."""

    @abc.abstractmethod
    async def get_art(self, player_id: str) -> Optional[Art]:
        """Cover art for the player's current track, or None."""


# ---------------------------------------------------------------- helpers

def epoch_ms() -> int:
    return int(time.time() * 1000)


def art_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:12] if data else ""


def sniff_mime(data: bytes, fallback: str = "image/jpeg") -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data.lstrip()[:5] == b"<?xml" or data.lstrip()[:4] == b"<svg":
        return "image/svg+xml"
    return fallback


# (regex on player id, key, friendly name, is_browser). Checked in order,
# case-insensitive. Shared by both backends; ids differ per OS but the
# patterns are loose enough to cover both.
_KNOWN_PLAYERS: list[tuple[str, str, str, bool]] = [
    (r"spotify", "spotify", "Spotify", False),
    (r"qobuz", "qobuz", "Qobuz", False),
    (r"applemusic|apple\.music|itunes", "apple-music", "Apple Music", False),
    (r"tidal", "tidal", "TIDAL", False),
    (r"deezer", "deezer", "Deezer", False),
    (r"amazon.*music", "amazon-music", "Amazon Music", False),
    (r"zunemusic|media\.player|microsoft\.media", "media-player", "Media Player", False),
    (r"foobar", "foobar2000", "foobar2000", False),
    (r"musicbee", "musicbee", "MusicBee", False),
    (r"aimp", "aimp", "AIMP", False),
    (r"vlc", "vlc", "VLC", False),
    (r"mpv", "mpv", "mpv", False),
    (r"rhythmbox", "rhythmbox", "Rhythmbox", False),
    (r"strawberry", "strawberry", "Strawberry", False),
    (r"elisa", "elisa", "Elisa", False),
    (r"cider", "cider", "Cider", False),
    # Browsers. 308046B0AF4A39CB / 6F193CCC56814779 are the Firefox / Firefox
    # Nightly AUMIDs on Windows.
    (r"msedge|microsoftedge|\bedge\b", "edge", "Edge", True),
    (r"firefox|308046B0AF4A39CB|6F193CCC56814779", "firefox", "Firefox", True),
    (r"librewolf", "librewolf", "LibreWolf", True),
    (r"brave", "brave", "Brave", True),
    (r"vivaldi", "vivaldi", "Vivaldi", True),
    (r"opera", "opera", "Opera", True),
    (r"chromium", "chromium", "Chromium", True),
    (r"chrome", "chrome", "Chrome", True),
    (r"plasma-browser-integration", "browser", "Browser", True),
]
_KNOWN_RE = [(re.compile(p, re.I), k, n, b) for p, k, n, b in _KNOWN_PLAYERS]

DEFAULT_PLAYER_KEY = "spotify"


def classify_player(player_id: str, fallback_name: Optional[str] = None) -> tuple[str, str, bool]:
    """Map a raw player id to (selection key, friendly name, is_browser)."""
    for rx, key, name, is_browser in _KNOWN_RE:
        if rx.search(player_id) or (fallback_name and rx.search(fallback_name)):
            return key, name, is_browser
    return player_id, (fallback_name or player_id), False


def detect_ad(player_key: str, title: str, artist: str) -> bool:
    t = (title or "").strip().lower()
    if t in ("advertisement", "spotify advertisement"):
        return True
    if player_key == "spotify" and t and not (artist or "").strip():
        # Spotify free tier exposes ads with an empty artist (title is often
        # "Spotify" or the advertiser's name).
        return True
    return False
