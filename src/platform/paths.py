"""Where settings and logs live, per OS.

Windows: next to the exe when frozen and writable (portable), else
%APPDATA%\\obs-now-playing. Linux: $XDG_CONFIG_HOME/obs-now-playing
(~/.config/obs-now-playing). Running from source uses the per-user location.
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import Optional

APP_DIR_NAME = "obs-now-playing"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> str:
    """Folder holding bundled read-only files (web/), source or frozen."""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def web_dir() -> str:
    return os.path.join(resource_dir(), "web")


def _writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        fd, p = tempfile.mkstemp(prefix=".w", dir=d)
        os.close(fd)
        os.unlink(p)
        return True
    except OSError:
        return False


def _user_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_DIR_NAME)


_data_dir: Optional[str] = None


def data_dir() -> str:
    global _data_dir
    if _data_dir is None:
        candidate = None
        if sys.platform == "win32" and is_frozen():
            exe_dir = os.path.dirname(sys.executable)
            if _writable(exe_dir):
                candidate = exe_dir
        if candidate is None:
            candidate = _user_dir()
            os.makedirs(candidate, exist_ok=True)
        _data_dir = candidate
    return _data_dir


def runtime_dir() -> str:
    """Per-user folder for the instance lock/info, independent of where the
    app lives, so every copy (portable folders, a new version, the AppImage)
    finds the running instance."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        d = os.path.join(base, APP_DIR_NAME)
    elif os.environ.get("XDG_RUNTIME_DIR"):
        d = os.path.join(os.environ["XDG_RUNTIME_DIR"], APP_DIR_NAME)
    else:
        d = _user_dir()
    os.makedirs(d, exist_ok=True)
    return d


OBS_SCRIPT_NAME = "obs-now-playing.lua"


def obs_script_path() -> Optional[str]:
    """Path of the OBS script to show the user (one that stays valid).

    Release folders: next to the exe. Source checkout: obs/ in the repo.
    AppImage: the bundled copy lives in a mount that changes every launch, so
    it is copied to the data folder with the AppImage's path filled in.
    """
    if not is_frozen():
        p = os.path.join(resource_dir(), "obs", OBS_SCRIPT_NAME)
        return p if os.path.exists(p) else None
    bundled = os.path.join(os.path.dirname(sys.executable), OBS_SCRIPT_NAME)
    if not os.path.exists(bundled):
        return None
    appimage = os.environ.get("APPIMAGE")
    if not appimage:
        return bundled
    return _install_appimage_script(bundled, appimage)


def _install_appimage_script(bundled: str, appimage: str) -> Optional[str]:
    target = os.path.join(data_dir(), OBS_SCRIPT_NAME)
    try:
        with open(bundled, encoding="utf-8") as f:
            text = f.read()
        literal = "[==[" + appimage + "]==]"
        text = text.replace('local BUNDLED_APP_PATH = ""', f"local BUNDLED_APP_PATH = {literal}", 1)
        current = None
        if os.path.exists(target):
            with open(target, encoding="utf-8") as f:
                current = f.read()
        if current != text:
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)
        return target
    except OSError:
        return None


def settings_path() -> str:
    return os.path.join(data_dir(), "settings.json")


def log_path() -> str:
    return os.path.join(data_dir(), "obs-now-playing.log")
