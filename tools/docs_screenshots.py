"""Render README screenshots from fake demo data (no real tracks or cover art).

    python tools/docs_screenshots.py           ->  docs/overlay.png, docs/dashboard.png
    python tools/docs_screenshots.py --serve   ->  just serve the demo on :8765
                                                   (capture with any browser)

Serves the real overlay/dashboard with a fake media backend and captures them
with headless Edge/Chrome. The overlay shot has a transparent background.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.demo import DEMO_TRACKS, demo_art  # noqa: E402
from src.media.base import (  # noqa: E402
    Art, MediaBackend, NowPlaying, PlaybackStatus, PlayerInfo, PlayerSnapshot, epoch_ms,
)
from src.media.manager import PlayerManager  # noqa: E402
from src.server import create_app, start_server  # noqa: E402
from src.settings import SettingsStore  # noqa: E402

PORT = 8765  # the default, so screenshots show the real OBS URL
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google-chrome", "chromium", "microsoft-edge",
]


class DemoBackend(MediaBackend):
    name = "demo"

    def __init__(self) -> None:
        super().__init__()
        self.status = "ok"
        t = DEMO_TRACKS[0]
        self.art = Art(demo_art(0), "image/svg+xml")
        now = epoch_ms()
        self.snaps = [
            PlayerSnapshot(
                PlayerInfo("Spotify.exe", "Spotify", PlaybackStatus.PLAYING, False, "spotify"),
                NowPlaying("Spotify.exe", "Spotify", t["title"], t["artist"], t["album"], PlaybackStatus.PLAYING,
                           position_ms=74_000, duration_ms=t["duration_ms"], position_at_ms=now,
                           art_version=self.art.version)),
            PlayerSnapshot(
                PlayerInfo("308046B0AF4A39CB", "Firefox", PlaybackStatus.PAUSED, True, "firefox"),
                NowPlaying("308046B0AF4A39CB", "Firefox", "Cooking stream highlights", "Some Channel",
                           status=PlaybackStatus.PAUSED, duration_ms=600_000)),
        ]

    async def start(self) -> None:
        self._emit()

    async def stop(self) -> None:
        pass

    def snapshot(self):
        return list(self.snaps)

    async def get_art(self, player_id):
        return self.art if player_id == "Spotify.exe" else None


def find_browser() -> str:
    for b in BROWSERS:
        path = b if os.path.isabs(b) else shutil.which(b)
        if path and os.path.exists(path):
            return path
    sys.exit("No Edge/Chrome found for headless screenshots")


def shoot(browser: str, url: str, out: str, size: str, transparent: bool) -> None:
    with tempfile.TemporaryDirectory() as profile:
        cmd = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
               f"--user-data-dir={profile}", f"--window-size={size}", "--virtual-time-budget=4000",
               f"--screenshot={out}", url]
        if transparent:
            cmd.insert(2, "--default-background-color=00000000")
        subprocess.run(cmd, check=True, timeout=60, capture_output=True)
    print("wrote", os.path.relpath(out, ROOT))


async def serve_forever() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SettingsStore(os.path.join(tmp, "settings.json"))
        manager = PlayerManager(DemoBackend(), "spotify")
        runner = await start_server(create_app(manager, store), PORT)
        await manager.start()
        print(f"demo dashboard: http://127.0.0.1:{PORT}/  overlay: http://127.0.0.1:{PORT}/overlay", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


async def main() -> None:
    if "--serve" in sys.argv:
        await serve_forever()
        return
    browser = find_browser()
    docs = os.path.join(ROOT, "docs")
    os.makedirs(docs, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        store = SettingsStore(os.path.join(tmp, "settings.json"))
        manager = PlayerManager(DemoBackend(), "spotify")
        runner = await start_server(create_app(manager, store), PORT)
        await manager.start()
        loop = asyncio.get_running_loop()
        try:
            base = f"http://127.0.0.1:{PORT}"
            await loop.run_in_executor(None, shoot, browser, f"{base}/overlay",
                                       os.path.join(docs, "overlay.png"), "1280,400", True)
            await loop.run_in_executor(None, shoot, browser, f"{base}/",
                                       os.path.join(docs, "dashboard.png"), "1280,1060", False)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
