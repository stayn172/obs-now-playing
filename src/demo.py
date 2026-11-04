"""Demo tracks for the dashboard preview (fictional; generated SVG covers)."""
from __future__ import annotations

from typing import Optional

from .media.base import art_hash, epoch_ms

DEMO_TRACKS = [
    {"title": "Neon Afterglow", "artist": "The Midnight Static", "album": "City Lights",
     "duration_ms": 192_000, "colors": ("#ff4f8b", "#5b2bff")},
    {"title": "A Really Long Song Title That Needs To Scroll Across The Card",
     "artist": "Orchestra of Unnecessarily Long Names feat. Somebody", "album": "Marquee",
     "duration_ms": 167_000, "colors": ("#16d9a6", "#0b4f8a")},
    {"title": "Paper Planes", "artist": "Lumen", "album": "Glass Rooms",
     "duration_ms": 245_000, "colors": ("#ffb347", "#c2185b")},
]

PLACEHOLDER_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#3a3a44"/><stop offset="1" stop-color="#1c1c22"/></linearGradient></defs>
<rect width="300" height="300" fill="url(#g)"/>
<path d="M170 80v100.5a32 32 0 1 1-16-27.7V96l-50 12v84.5a32 32 0 1 1-16-27.7V96l82-20z"
 fill="#ffffff" fill-opacity="0.35"/></svg>"""
PLACEHOLDER_VERSION = art_hash(PLACEHOLDER_SVG)


def demo_art(index: int) -> bytes:
    a, b = DEMO_TRACKS[index % len(DEMO_TRACKS)]["colors"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/></linearGradient>
<radialGradient id="r" cx="0.3" cy="0.3" r="0.6"><stop offset="0" stop-color="#fff" stop-opacity="0.5"/>
<stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient></defs>
<rect width="300" height="300" fill="url(#g)"/><circle cx="90" cy="90" r="170" fill="url(#r)"/>
<circle cx="210" cy="215" r="{40 + index * 12}" fill="none" stroke="#fff" stroke-opacity="0.45" stroke-width="10"/>
</svg>""".encode()


def demo_now_playing(now_ms: Optional[int] = None) -> dict:
    """The demo playlist loops forever so the preview always animates."""
    now_ms = now_ms if now_ms is not None else epoch_ms()
    total = sum(t["duration_ms"] for t in DEMO_TRACKS)
    t = now_ms % total
    for i, track in enumerate(DEMO_TRACKS):
        if t < track["duration_ms"]:
            break
        t -= track["duration_ms"]
    version = art_hash(demo_art(i))
    return {
        "player_id": "demo",
        "player_name": "Demo",
        "title": track["title"],
        "artist": track["artist"],
        "album": track["album"],
        "status": "playing",
        "position_ms": t,
        "duration_ms": track["duration_ms"],
        "position_at_ms": now_ms,
        "art_version": version,
        "is_ad": False,
        "art_url": f"/api/art?demo={i}&v={version}",
    }
