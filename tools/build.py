"""Build a release for the current OS.

    python tools/build.py              # PyInstaller onedir + release archive
    python tools/build.py --appimage   # Linux: also build an AppImage (needs appimagetool)

Output (dist/):
    Windows: obs-now-playing-vX.Y.Z-win64.zip
    Linux:   obs-now-playing-vX.Y.Z-linux-x86_64.tar.gz  (+ .AppImage)

The release folder holds the app plus obs-now-playing.lua next to it, so the
OBS script finds the app without any setup.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.version import __version__  # noqa: E402

NAME = "obs-now-playing"
IS_WIN = sys.platform == "win32"
DIST = os.path.join(ROOT, "dist")
APP_DIR = os.path.join(DIST, NAME)

README_FIRST = """Now Playing Overlay {version}

1. Run {exe}. A settings page opens in your browser.
2. Click "Copy Browser Source URL".
3. In OBS: Sources -> + -> Browser, paste the URL, Width 640, Height 200.

Optional: start/stop the app with OBS. In OBS open Tools -> Scripts, click +,
and add obs-now-playing.lua from this folder.

Settings and the log file are stored in this folder (or in {fallback} if this
folder isn't writable). Not affiliated with OBS, Spotify, Qobuz or Apple.
"""


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def pyinstaller() -> None:
    run([sys.executable, os.path.join(ROOT, "tools", "make_icons.py")])
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", DIST, "--workpath", os.path.join(ROOT, "build", "work"),
         os.path.join(ROOT, "build", f"{NAME}.spec")])


def stage() -> None:
    """Put the user-facing extras next to the executable."""
    shutil.copy2(os.path.join(ROOT, "obs", f"{NAME}.lua"), APP_DIR)
    exe = f"{NAME}.exe" if IS_WIN else NAME
    fallback = r"%APPDATA%\obs-now-playing" if IS_WIN else "~/.config/obs-now-playing"
    with open(os.path.join(APP_DIR, "README-FIRST.txt"), "w", encoding="utf-8", newline="\r\n" if IS_WIN else "\n") as f:
        f.write(README_FIRST.format(version=__version__, exe=exe, fallback=fallback))
    for leftover in ("settings.json", "instance.json", f"{NAME}.log"):  # from local test runs
        p = os.path.join(APP_DIR, leftover)
        if os.path.exists(p):
            os.unlink(p)
    # Licenses: the app's own (MIT) plus every bundled third-party package.
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(APP_DIR, "LICENSE.txt"))
    from tools.licenses import write_notices
    names = write_notices(APP_DIR, os.path.join(ROOT, "requirements.txt"))
    print(f"third-party notices: {len(names)} components")


def archive() -> str:
    if IS_WIN:
        out = os.path.join(DIST, f"{NAME}-v{__version__}-win64.zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for base, _dirs, files in os.walk(APP_DIR):
                for fn in files:
                    full = os.path.join(base, fn)
                    z.write(full, os.path.relpath(full, DIST))
    else:
        out = os.path.join(DIST, f"{NAME}-v{__version__}-linux-x86_64.tar.gz")
        with tarfile.open(out, "w:gz") as t:
            t.add(APP_DIR, arcname=NAME)
    print(f"wrote {out} ({os.path.getsize(out) / 1e6:.1f} MB)")
    return out


def appimage() -> str:
    """Wrap the onedir build in an AppImage (Linux only)."""
    tool = shutil.which("appimagetool") or os.environ.get("APPIMAGETOOL")
    if not tool:
        sys.exit("appimagetool not found (set APPIMAGETOOL or put it on PATH)")
    appdir = os.path.join(DIST, f"{NAME}.AppDir")
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(APP_DIR, os.path.join(appdir, "usr", "lib", NAME))
    recipe = os.path.join(ROOT, "build", "appimage")
    for fn in ("AppRun", f"{NAME}.desktop"):
        shutil.copy2(os.path.join(recipe, fn), appdir)
    os.chmod(os.path.join(appdir, "AppRun"), 0o755)
    shutil.copy2(os.path.join(ROOT, "build", "icon.png"), os.path.join(appdir, f"{NAME}.png"))
    out = os.path.join(DIST, f"{NAME}-v{__version__}-linux-x86_64.AppImage")
    run([tool, appdir, out], env={**os.environ, "ARCH": "x86_64"})
    os.chmod(out, os.stat(out).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"wrote {out}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--appimage", action="store_true", help="Linux: also build an AppImage")
    ap.add_argument("--skip-pyinstaller", action="store_true", help="only re-stage and re-archive")
    args = ap.parse_args()
    if not args.skip_pyinstaller:
        pyinstaller()
    stage()
    archive()
    if args.appimage:
        if IS_WIN:
            sys.exit("--appimage is Linux only")
        appimage()


if __name__ == "__main__":
    main()
