"""List every detected media session with its metadata and save cover art.

    python -m tools.list_players            # one snapshot
    python -m tools.list_players --watch    # print on every change
    python -m tools.list_players --out art_dump

Use this to check that a player is detected before building on it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.media import create_backend  # noqa: E402
from src.media.base import PlayerSnapshot  # noqa: E402

EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif", "image/svg+xml": "svg"}


def fmt_ms(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


async def dump(backend, snaps: list[PlayerSnapshot], out_dir: str | None) -> None:
    if not snaps:
        print(f"No media sessions detected. (backend: {backend.name}, status: {backend.status}"
              + (f", {backend.error}" if backend.error else "") + ")")
        return
    print(f"{len(snaps)} session(s) via {backend.name}:")
    for s in snaps:
        i, np = s.info, s.now_playing
        print(f"\n  [{i.status.value.upper()}] {i.display_name}" + ("  (browser)" if i.is_browser else ""))
        print(f"    key:      {i.key}")
        print(f"    id:       {i.player_id}")
        print(f"    title:    {np.title!r}")
        print(f"    artist:   {np.artist!r}")
        print(f"    album:    {np.album!r}")
        print(f"    position: {fmt_ms(np.position_now_ms())} / {fmt_ms(np.duration_ms)}")
        if np.is_ad:
            print("    ** detected as advertisement **")
        art = await backend.get_art(i.player_id)
        if art is None:
            print("    art:      none")
        elif out_dir:
            os.makedirs(out_dir, exist_ok=True)
            name = re.sub(r"[^\w.-]+", "_", i.player_id)
            path = os.path.join(out_dir, f"{name}.{EXT.get(art.mime, 'bin')}")
            with open(path, "wb") as f:
                f.write(art.data)
            print(f"    art:      {len(art.data)} bytes {art.mime} -> {path}")
        else:
            print(f"    art:      {len(art.data)} bytes {art.mime}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", action="store_true", help="keep running and print on every change")
    ap.add_argument("--out", default="art_dump", help="folder for cover art (default: art_dump, '' to skip)")
    ap.add_argument("--wait", type=float, default=2.5, help="seconds to wait for sessions and art")
    args = ap.parse_args()

    backend = create_backend()
    changed = asyncio.Event()
    backend.add_listener(changed.set)
    await backend.start()
    try:
        await asyncio.sleep(args.wait)  # let enumeration + thumbnail reads finish
        await dump(backend, backend.snapshot(), args.out or None)
        while args.watch:
            changed.clear()
            await changed.wait()
            await asyncio.sleep(0.3)
            print("\n" + "-" * 60)
            await dump(backend, backend.snapshot(), args.out or None)
    finally:
        await backend.stop()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
