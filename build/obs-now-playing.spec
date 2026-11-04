# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Windows and Linux (one file, platform branches).

Build with:  python tools/build.py   (wraps PyInstaller + packaging)

onedir, not onefile: onefile unpacks to a temp dir on every start (slow) and
trips antivirus heuristics far more often. UPX is off for the same reason.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 (SPECPATH is injected)
sys.path.insert(0, ROOT)
from src.version import __version__  # noqa: E402

IS_WIN = sys.platform == "win32"
NAME = "obs-now-playing"

# Backends are imported lazily by create_backend(); list them so the
# analysis never misses one. winrt ships its own PyInstaller hook that
# collects the projection tables; pystray's contrib hook collects backends.
hiddenimports = ["src.media.windows_smtc"] if IS_WIN else ["src.media.linux_mpris", "dbus_next.aio"]

excludes = [
    "tkinter", "unittest", "pydoc_data", "test",
    "pytest", "_pytest", "lupa", "PyInstaller",
]
excludes += ["dbus_next"] if IS_WIN else ["winrt"]

a = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "launcher.py")],  # noqa: F821
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "web"), "web")],
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)  # noqa: F821

version_info = None
if IS_WIN:
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )

    nums = tuple(int(x) for x in (__version__.split("-")[0].split(".") + ["0", "0", "0"])[:4])
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("FileDescription", "Now Playing Overlay for OBS"),
                StringStruct("ProductName", "Now Playing Overlay"),
                StringStruct("FileVersion", __version__),
                StringStruct("ProductVersion", __version__),
                StringStruct("OriginalFilename", f"{NAME}.exe"),
                StringStruct("InternalName", NAME),
                StringStruct("LegalCopyright", "Open source; not affiliated with OBS, Spotify, Qobuz or Apple"),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    # NP_CONSOLE=1 builds a console variant for debugging startup failures.
    console=os.environ.get("NP_CONSOLE") == "1",
    icon=os.path.join(SPECPATH, "icon.ico") if IS_WIN else None,  # noqa: F821
    version=version_info,
    upx=False,
    strip=False,
    debug=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, strip=False, name=NAME)  # noqa: F821
