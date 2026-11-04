"""Single-instance lock and a small "where is the running instance" file.

Windows: a named mutex in the session namespace. Linux: an flock'd file.
Both are released by the OS if the process dies, so there are no stale locks.
A second launch reads instance.json to find the running dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from typing import Optional

from . import paths

log = logging.getLogger(__name__)

LOCK_NAME = "obs-now-playing"
_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self, name: str = LOCK_NAME, lock_dir: Optional[str] = None) -> None:
        self.name = name
        self.lock_dir = lock_dir
        self._handle = None
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        """True if we're the only instance. Fails open on unexpected errors."""
        try:
            return self._acquire_windows() if sys.platform == "win32" else self._acquire_posix()
        except Exception:
            log.exception("single-instance check failed; continuing")
            return True

    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.CreateMutexW(None, False, f"Local\\{self.name}")
        err = ctypes.get_last_error()
        if not handle:
            log.warning("CreateMutexW failed (error %s); continuing", err)
            return True
        if err == _ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        self._handle = (k32, handle)
        return True

    def _acquire_posix(self) -> bool:
        import fcntl

        d = self.lock_dir or paths.runtime_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{self.name}.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._handle:
            k32, handle = self._handle
            k32.CloseHandle(handle)
            self._handle = None
        if self._fd is not None:
            try:
                os.close(self._fd)  # releases the flock
            except OSError:
                pass
            self._fd = None


# ------------------------------------------------------------ instance info

def info_path() -> str:
    # Not data_dir(): portable copies keep data next to their exe, but every
    # copy must find the one running instance.
    return os.path.join(paths.runtime_dir(), "instance.json")


def write_info(port: int, path: Optional[str] = None) -> None:
    path = path or info_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": port, "url": f"http://127.0.0.1:{port}/"}, f)
    except OSError:
        log.warning("could not write %s", path, exc_info=True)


def remove_info(path: Optional[str] = None) -> None:
    path = path or info_path()
    try:
        with open(path, encoding="utf-8") as f:
            if json.load(f).get("pid") != os.getpid():
                return  # not ours
        os.unlink(path)
    except (OSError, ValueError):
        pass


def running_dashboard_url(wait: float = 6.0, path: Optional[str] = None) -> Optional[str]:
    """URL of the running instance's dashboard, waiting briefly if it's still starting."""
    path = path or info_path()
    deadline = time.monotonic() + wait
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                url = json.load(f).get("url")
            if url:
                with urllib.request.urlopen(url + "api/settings", timeout=1) as r:
                    if r.status == 200:
                        return url
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.3)
