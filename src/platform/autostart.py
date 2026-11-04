"""Start-with-OS support.

Windows: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run value.
Linux: ~/.config/autostart/obs-now-playing.desktop (XDG autostart).

The command points at the frozen exe (or the AppImage), or, when running from
source, at pythonw/python with the repo on sys.path. ``--autostart`` tells
the app not to pop the browser open at login.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional

from . import paths

log = logging.getLogger(__name__)

APP_ID = "obs-now-playing"
APP_TITLE = "Now Playing Overlay"
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_FLAG = "--autostart"


class AutostartError(RuntimeError):
    pass


def launch_command() -> list[str]:
    if paths.is_frozen():
        exe = os.environ.get("APPIMAGE") or sys.executable
        return [exe, AUTOSTART_FLAG]
    py = sys.executable
    if sys.platform == "win32":
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(pyw):
            py = pyw  # no console window at login
    repo = paths.resource_dir()
    code = f"import sys; sys.path.insert(0, {repo!r}); from src.app import main; sys.exit(main())"
    return [py, "-c", code, AUTOSTART_FLAG]


# ---------------------------------------------------------------- Windows

def _win_command() -> str:
    return subprocess.list2cmdline(launch_command())


def _win_read(key_path: str) -> Optional[str]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            value, _ = winreg.QueryValueEx(k, APP_ID)
            return value
    except FileNotFoundError:
        return None


def _win_write(key_path: str, enabled: bool) -> None:
    import winreg

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, _win_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_ID)
                except FileNotFoundError:
                    pass
    except OSError as e:
        raise AutostartError(f"couldn't update the Windows startup entry: {e}") from e


# ------------------------------------------------------------------ Linux

def desktop_path(config_home: Optional[str] = None) -> str:
    base = config_home or os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", f"{APP_ID}.desktop")


def _desktop_quote(arg: str) -> str:
    """Quote one Exec= argument per the Desktop Entry spec."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg.replace("%", "%%")
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return '"' + escaped.replace("%", "%%") + '"'


def desktop_entry() -> str:
    exec_line = " ".join(_desktop_quote(a) for a in launch_command())
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_TITLE}\n"
        "Comment=Local now-playing overlay for OBS\n"
        f"Exec={exec_line}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _linux_write(path: str, enabled: bool) -> None:
    try:
        if enabled:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(desktop_entry())
            os.replace(tmp, path)
        elif os.path.exists(path):
            os.unlink(path)
    except OSError as e:
        raise AutostartError(f"couldn't update {path}: {e}") from e


def _linux_read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# ------------------------------------------------------------------ API

class Autostart:
    """Thin wrapper so tests can point at a scratch registry key / folder."""

    def __init__(self, win_key: str = WIN_RUN_KEY, linux_path: Optional[str] = None) -> None:
        self.win_key = win_key
        self.linux_path = linux_path or desktop_path()

    @property
    def supported(self) -> bool:
        return sys.platform == "win32" or sys.platform.startswith("linux")

    def current(self) -> Optional[str]:
        """The installed command/entry, or None."""
        if sys.platform == "win32":
            return _win_read(self.win_key)
        if sys.platform.startswith("linux"):
            return _linux_read(self.linux_path)
        return None

    def is_enabled(self) -> bool:
        return self.current() is not None

    def is_current(self) -> bool:
        """Installed and pointing at this copy of the app."""
        cur = self.current()
        if cur is None:
            return False
        expected = _win_command() if sys.platform == "win32" else desktop_entry()
        return cur == expected

    def set_enabled(self, enabled: bool) -> None:
        if sys.platform == "win32":
            _win_write(self.win_key, enabled)
        elif sys.platform.startswith("linux"):
            _linux_write(self.linux_path, enabled)
        else:
            raise AutostartError(f"autostart isn't supported on {sys.platform}")
        log.info("autostart %s", "enabled" if enabled else "disabled")

    def reconcile(self, wanted: bool) -> None:
        """Make the OS entry match the setting; re-point it if the app moved."""
        if wanted and not self.is_current():
            self.set_enabled(True)
        elif not wanted and self.is_enabled():
            self.set_enabled(False)
