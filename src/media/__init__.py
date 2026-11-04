"""Media layer: OS backends + the player manager."""
from __future__ import annotations

import sys

from .base import MediaBackend


class NullBackend(MediaBackend):
    """Used on unsupported platforms: reports a status, never any players."""

    name = "none"

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.status, self.error = "no media backend", reason

    async def start(self) -> None:
        self._emit()

    async def stop(self) -> None:
        pass

    def snapshot(self):
        return []

    async def get_art(self, player_id):
        return None


def create_backend() -> MediaBackend:
    """Pick the backend for this OS; never raises."""
    try:
        if sys.platform == "win32":
            from .windows_smtc import WindowsSMTCBackend

            return WindowsSMTCBackend()
        if sys.platform.startswith("linux"):
            from .linux_mpris import LinuxMPRISBackend

            return LinuxMPRISBackend()
    except ImportError as e:
        return NullBackend(f"media backend dependency missing: {e}")
    return NullBackend(f"unsupported platform: {sys.platform}")
