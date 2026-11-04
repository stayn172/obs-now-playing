"""Render the app icon (same artwork as the tray icon) for packaging.

    python tools/make_icons.py   ->  build/icon.ico, build/icon.png
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.tray import make_icon_image  # noqa: E402


def main() -> None:
    out = os.path.join(ROOT, "build")
    os.makedirs(out, exist_ok=True)
    big = make_icon_image(256)
    big.save(os.path.join(out, "icon.png"))
    big.save(os.path.join(out, "icon.ico"), sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    print("wrote build/icon.png and build/icon.ico")


if __name__ == "__main__":
    main()
