"""Entry point: python -m src.app  (or the frozen exe)

Lifecycle: single-instance check -> settings -> media manager -> HTTP server
(port fallback) -> autostart reconcile -> tray -> run until Quit / signal.
A second launch just opens the running instance's dashboard.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import json
import signal
import socket
import sys
import time
import urllib.request
import webbrowser
from typing import Optional

from .media import create_backend
from .media.manager import PlayerManager
from .notices import Notices
from .platform import paths, single_instance
from .platform.autostart import Autostart, AutostartError
from .platform.clipboard import copy_text
from .server import AppControl, create_app, start_server
from .settings import SettingsStore
from .version import __version__

log = logging.getLogger("obs-now-playing")

IS_WINDOWS = sys.platform == "win32"


# ------------------------------------------------------------------ setup

def setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    try:
        fh = logging.handlers.RotatingFileHandler(paths.log_path(), maxBytes=512 * 1024,
                                                  backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    if sys.stderr is not None and not paths.is_frozen():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def show_fatal(message: str) -> None:
    """Last-resort error for a windowless app: message box on Windows."""
    log.error(message)
    if IS_WINDOWS and paths.is_frozen():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Now Playing Overlay", 0x10)
        except Exception:
            pass
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        log.warning("couldn't open the browser", exc_info=True)


# -------------------------------------------------------------------- app

class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop_event = asyncio.Event()
        self.notices = Notices()
        self.autostart = Autostart()
        self.store: Optional[SettingsStore] = None
        self.manager: Optional[PlayerManager] = None
        self.runner = None
        self.tray = None
        self.port = 0

    @property
    def launched_by(self) -> str:
        if self.args.from_obs:
            return "obs"
        return "autostart" if self.args.autostart else "user"

    @property
    def dashboard_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def overlay_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/overlay"

    # ---------------------------------------------------------- startup

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.loop.set_exception_handler(self._loop_exception)
        settings_file = paths.settings_path()
        first_run = not os.path.exists(settings_file)
        self.store = SettingsStore(settings_file)
        if first_run:
            self.store.save()

        self.manager = PlayerManager(create_backend(), self.store.get("selected_player"))
        control = AppControl(launched_by=self.launched_by, quit=self.stop_event.set)
        app = create_app(self.manager, self.store, self.notices, control)
        await self._start_server(app)
        single_instance.write_info(self.port)

        await self.manager.start()
        self._sync_autostart(self.store.get("start_with_os"), at_startup=True)
        self.store.add_listener(self._on_settings)
        has_tray = await self._start_tray()

        log.info("Now Playing Overlay %s (started by %s)", __version__, self.launched_by)
        log.info("dashboard: %s   OBS browser source: %s", self.dashboard_url, self.overlay_url)
        log.info("data folder: %s", paths.data_dir())
        background = self.args.autostart or self.args.from_obs
        if self.args.open or (not self.args.no_browser and not background and (first_run or not has_tray)):
            open_browser(self.dashboard_url)

        self._install_signal_handlers()
        try:
            await self.stop_event.wait()
        finally:
            await self.shutdown()

    async def _start_server(self, app) -> None:
        wanted = self.args.port or self.store.get("port")
        try:
            self.runner = await start_server(app, wanted)
            self.port = wanted
            return
        except OSError as e:
            log.warning("port %d unavailable (%s)", wanted, e)
        # Prefer a nearby, recognizable port; fall back to any free one.
        for candidate in [*range(wanted + 1, min(wanted + 11, 65536)), None]:
            port = candidate or free_port()
            try:
                self.runner = await start_server(app, port)
                self.port = port
                break
            except OSError:
                if candidate is None:
                    raise
        else:  # pragma: no cover - loop always breaks or raises
            raise OSError("no free port")
        if not self.args.port:
            self.store.update({"port": self.port})  # keep the OBS URL stable from now on
        self.notices.set(
            "port-moved", "warn",
            f"Port {wanted} was already in use, so the app moved to port {self.port}. "
            f"Update your OBS Browser Source URL once to {self.overlay_url}",
        )

    async def _start_tray(self) -> bool:
        if self.args.no_tray:
            return False
        try:
            from .tray import Tray, TrayActions, linux_tray_available

            if not IS_WINDOWS:
                ok, reason = await linux_tray_available()
                if not ok:
                    log.info("running without a tray icon: %s", reason)
                    self.notices.set(
                        "no-tray", "info",
                        f"No tray icon on this desktop ({reason}). The app keeps running in the "
                        "background; launch it again to reopen this page.",
                    )
                    return False
            self.tray = Tray(TrayActions(
                open_settings=lambda: open_browser(self.dashboard_url),
                copy_url=self._tray_copy_url,
                toggle_autostart=lambda: self._from_thread(self._toggle_autostart),
                autostart_enabled=lambda: bool(self.store.get("start_with_os")),
                quit=lambda: self._from_thread(self.stop_event.set),
                autostart_label="Start with Windows" if IS_WINDOWS else "Start when I log in",
            ))
            self.tray.start()
            return True
        except Exception as e:
            log.exception("tray unavailable")
            self.notices.set("no-tray", "info", f"Tray icon unavailable ({e}). Launch the app again to reopen this page.")
            return False

    # ------------------------------------------------------- behaviors

    def _from_thread(self, fn) -> None:
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn)

    def _tray_copy_url(self) -> None:
        if copy_text(self.overlay_url):
            self.tray.notify(f"Copied {self.overlay_url}. Paste it into an OBS Browser Source.")
        else:
            open_browser(self.dashboard_url)  # copy from the dashboard instead

    def _toggle_autostart(self) -> None:
        self.store.update({"start_with_os": not self.store.get("start_with_os")})

    def _on_settings(self, settings: dict, changed: set) -> None:
        if "start_with_os" in changed:
            self._sync_autostart(settings["start_with_os"])
            if self.tray:
                self.tray.refresh()

    def _sync_autostart(self, wanted: bool, at_startup: bool = False) -> None:
        if not self.autostart.supported:
            return
        try:
            self.autostart.reconcile(wanted)
            self.notices.clear("autostart")
        except AutostartError as e:
            log.warning("%s", e)
            self.notices.set("autostart", "error", f"Couldn't change start-up behavior: {e}")
            if not at_startup:  # reflect reality in the UI
                actual = self.autostart.is_enabled()
                if actual != wanted:
                    self.store.update({"start_with_os": actual})

    def _install_signal_handlers(self) -> None:
        if IS_WINDOWS:
            return  # Ctrl+C arrives as KeyboardInterrupt
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                self.loop.add_signal_handler(sig, self.stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

    def _loop_exception(self, loop, context) -> None:
        log.error("unhandled asyncio error: %s", context.get("message"), exc_info=context.get("exception"))

    async def shutdown(self) -> None:
        log.info("shutting down")
        if self.tray:
            self.tray.stop()
        try:
            if self.manager:
                await self.manager.stop()
        finally:
            if self.runner:
                await self.runner.cleanup()
            single_instance.remove_info()


# ------------------------------------------------------------------- main

def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="obs-now-playing")
    ap.add_argument("--port", type=int, help="override the port for this run (not saved)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    ap.add_argument("--no-browser", action="store_true", help="never open the browser")
    ap.add_argument("--no-tray", action="store_true", help="run without a tray icon")
    ap.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)  # launched at login
    ap.add_argument("--from-obs", action="store_true",
                    help="started by the OBS script: no browser, and --quit-from-obs may close it")
    ap.add_argument("--quit", action="store_true", help="quit the running instance")
    ap.add_argument("--quit-from-obs", action="store_true",
                    help="quit the running instance only if the OBS script started it")
    ap.add_argument("--debug", action="store_true")
    return ap.parse_args(argv)


def request_quit(only_if_launched_by: Optional[str] = None,
                 lock: Optional[single_instance.SingleInstance] = None,
                 info_path: Optional[str] = None) -> int:
    """Ask the running instance to quit. Nothing running is fine, not an error."""
    lock = lock or single_instance.SingleInstance()
    if lock.acquire():
        lock.release()
        log.info("quit: no running instance")
        return 0
    url = single_instance.running_dashboard_url(wait=3.0, path=info_path)
    if url is None:
        log.warning("quit: instance is running but not responding")
        return 1
    body = json.dumps({"only_if_launched_by": only_if_launched_by} if only_if_launched_by else {}).encode()
    req = urllib.request.Request(url + "api/quit", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            result = json.load(r)
    except Exception as e:
        log.warning("quit request failed: %s", e)
        return 1
    log.info("quit: %s (running instance was started by %s)",
             "stopping" if result.get("quitting") else "left running", result.get("launched_by"))
    return 0


def wait_for_lock(lock: single_instance.SingleInstance, seconds: float) -> bool:
    """Retry the lock for a while: covers an instance that is just quitting
    (OBS "Reload scripts" sends quit and start back to back)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.25)
        if lock.acquire():
            return True
    return False


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.debug)

    if args.quit or args.quit_from_obs:
        return request_quit("obs" if args.quit_from_obs else None)

    lock = single_instance.SingleInstance()
    acquired = lock.acquire() or (args.from_obs and wait_for_lock(lock, 4.0))
    if not acquired:
        if args.autostart or args.from_obs:
            return 0  # already running; nothing to do in the background
        url = single_instance.running_dashboard_url()
        if url is None:
            show_fatal("Now Playing Overlay is already running but not responding. "
                       "Quit it from the tray (or end it in Task Manager) and try again.")
            return 1
        log.info("already running at %s", url)
        if not args.no_browser:
            open_browser(url)
        return 0

    try:
        asyncio.run(App(args).run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.exception("fatal error")
        show_fatal(f"Now Playing Overlay stopped because of an error:\n{e}\n\nLog: {paths.log_path()}")
        return 1
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
