"""Player manager: exposes the one player the user chose.

There is deliberately no automatic source detection. The user picks an app
(default: Spotify), the way OBS Game Capture picks a window. If that app isn't
running, there is no active player and the overlay shows nothing, so a
YouTube tab can't show up unless the user chose that browser.

Selection is stored as a player *key* ("spotify", "firefox", or a raw id for
unknown apps), so it survives app restarts and Store-vs-classic installs.
When one app exposes several sessions (e.g. browser tabs) the manager picks
the playing one, and sticks with it while it keeps playing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .base import (
    _KNOWN_PLAYERS,
    DEFAULT_PLAYER_KEY,
    Art,
    MediaBackend,
    NowPlaying,
    PlaybackStatus,
    PlayerSnapshot,
)

log = logging.getLogger(__name__)

_KNOWN_NAMES = {key: name for _rx, key, name, _b in _KNOWN_PLAYERS}


def matches(snap: PlayerSnapshot, selected: str) -> bool:
    return bool(selected) and (snap.info.key == selected or snap.info.player_id == selected)


def select_session(
    snaps: Iterable[PlayerSnapshot],
    selected: str,
    current_id: Optional[str] = None,
    playing_since: Optional[dict[str, float]] = None,
    last_played: Optional[dict[str, float]] = None,
) -> Optional[PlayerSnapshot]:
    """Pick the session to show for the selected app, or None if it isn't running.

    Pure function so it can be unit tested without a backend.
    """
    playing_since = playing_since or {}
    last_played = last_played or {}
    cands = [s for s in snaps if matches(s, selected)]
    if not cands:
        return None

    playing = [s for s in cands if s.info.status == PlaybackStatus.PLAYING]
    if playing:
        for s in playing:  # stability: keep the current session while it plays
            if s.info.player_id == current_id:
                return s
        return max(playing, key=lambda s: playing_since.get(s.info.player_id, 0.0))

    for s in cands:
        if s.info.player_id == current_id:
            return s
    return max(cands, key=lambda s: last_played.get(s.info.player_id, 0.0))


@dataclass
class PlayerEntry:
    """One row in the dashboard's player picker (sessions grouped by app)."""
    key: str
    display_name: str
    is_browser: bool
    running: bool
    status: str
    title: str
    sessions: int
    selected: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class PlayerManager:
    def __init__(
        self,
        backend: MediaBackend,
        selected_player: str = DEFAULT_PLAYER_KEY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self._selected = selected_player or DEFAULT_PLAYER_KEY
        self._clock = clock
        self._active: Optional[PlayerSnapshot] = None
        self._snaps: list[PlayerSnapshot] = []
        self._playing_since: dict[str, float] = {}
        self._last_played: dict[str, float] = {}
        self._prev_status: dict[str, PlaybackStatus] = {}
        self._names: dict[str, str] = dict(_KNOWN_NAMES)
        self._listeners: list[Callable[[], None]] = []
        backend.add_listener(self.refresh)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.backend.start()

    async def stop(self) -> None:
        await self.backend.stop()

    def add_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.exception("manager listener failed")

    # ------------------------------------------------------------ selection

    @property
    def selected_player(self) -> str:
        return self._selected

    def set_selected(self, key: str) -> None:
        key = (key or "").strip() or DEFAULT_PLAYER_KEY
        if key != self._selected:
            self._selected = key
            self._active = None
            self.refresh()

    def refresh(self) -> None:
        """Re-read the backend snapshot and recompute the active session."""
        try:
            snaps = self.backend.snapshot()
        except Exception:
            log.exception("backend snapshot failed")
            snaps = []
        now = self._clock()
        seen = set()
        for s in snaps:
            pid = s.info.player_id
            seen.add(pid)
            self._names.setdefault(s.info.key, s.info.display_name)
            if s.info.status == PlaybackStatus.PLAYING:
                if self._prev_status.get(pid) != PlaybackStatus.PLAYING:
                    self._playing_since[pid] = now
                self._last_played[pid] = now
            self._prev_status[pid] = s.info.status
        for pid in list(self._prev_status):
            if pid not in seen:  # closed/crashed: forget it
                self._prev_status.pop(pid, None)
                self._playing_since.pop(pid, None)
                self._last_played.pop(pid, None)

        self._snaps = snaps
        current = self._active.info.player_id if self._active else None
        self._active = select_session(snaps, self._selected, current, self._playing_since, self._last_played)
        self._notify()

    # ------------------------------------------------------------- queries

    def active(self) -> Optional[PlayerSnapshot]:
        return self._active

    def now_playing(self) -> Optional[NowPlaying]:
        return self._active.now_playing if self._active else None

    def display_name(self, key: str) -> str:
        return self._names.get(key, key)

    def players(self) -> list[PlayerEntry]:
        """Open apps grouped by key, plus the selected one even if it's closed."""
        groups: dict[str, list[PlayerSnapshot]] = {}
        for s in self._snaps:
            groups.setdefault(s.info.key, []).append(s)
        rank = {PlaybackStatus.PLAYING: 0, PlaybackStatus.PAUSED: 1, PlaybackStatus.STOPPED: 2}
        out = []
        for key, ss in groups.items():
            best = min(ss, key=lambda s: rank[s.info.status])
            if any(s is self._active for s in ss):
                best = self._active
            out.append(PlayerEntry(
                key=key, display_name=best.info.display_name, is_browser=best.info.is_browser,
                running=True, status=best.info.status.value, title=best.now_playing.title,
                sessions=len(ss), selected=(key == self._selected),
            ))
        if self._selected not in groups:
            # The selected id may be a raw player_id rather than a key.
            if not any(s.info.player_id == self._selected for s in self._snaps):
                out.append(PlayerEntry(
                    key=self._selected, display_name=self.display_name(self._selected),
                    is_browser=False, running=False, status="not running", title="",
                    sessions=0, selected=True,
                ))
        # Stable order like OBS's window list; a missing selection goes first.
        out.sort(key=lambda e: (e.running, e.is_browser, e.display_name.lower()))
        return out

    def state(self) -> dict:
        np = self.now_playing()
        return {
            "selected_player": self._selected,
            "selected_name": self.display_name(self._selected),
            "running": self._active is not None,
            "now_playing": np.to_dict() if np else None,
            "backend": {"name": self.backend.name, "status": self.backend.status, "error": self.backend.error},
        }

    async def get_art(self) -> Optional[Art]:
        if not self._active:
            return None
        return await self.backend.get_art(self._active.info.player_id)
