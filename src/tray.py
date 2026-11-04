"""Tray icon: Open settings, Copy OBS URL, Start with OS, Quit.

pystray runs its own event loop, so it lives in a daemon thread; every menu
action is handed back to the asyncio loop with call_soon_threadsafe.
Linux: only used with a StatusNotifier host + AppIndicator bindings (GNOME
needs the AppIndicator extension). Without one the app runs tray-less.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

TITLE = "Now Playing Overlay"


@dataclass
class TrayActions:
    open_settings: Callable[[], None]
    copy_url: Callable[[], None]
    toggle_autostart: Callable[[], None]
    autostart_enabled: Callable[[], bool]
    quit: Callable[[], None]
    autostart_label: str = "Start with Windows"


def make_icon_image(size: int = 64):
    """Rounded square with a music note, drawn at 4x and downsampled."""
    from PIL import Image, ImageDraw

    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # vertical gradient background
    top, bottom = (124, 140, 255), (84, 96, 230)
    grad = Image.new("RGBA", (1, s))
    for y in range(s):
        t = y / (s - 1)
        grad.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    grad = grad.resize((s, s))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.24), fill=255)
    img.paste(grad, (0, 0), mask)
    # beamed eighth notes
    w = (255, 255, 255, 255)
    stem = int(s * 0.06)
    x1, x2 = int(s * 0.36), int(s * 0.70)
    y_top1, y_top2 = int(s * 0.24), int(s * 0.18)
    r = int(s * 0.11)
    cy1, cy2 = int(s * 0.70), int(s * 0.64)
    d.polygon([(x1, y_top1), (x2, y_top2), (x2, y_top2 + stem * 2), (x1, y_top1 + stem * 2)], fill=w)
    d.rectangle((x1 - stem, y_top1, x1, cy1), fill=w)
    d.rectangle((x2 - stem, y_top2, x2, cy2), fill=w)
    d.ellipse((x1 - stem - 2 * r + stem // 2, cy1 - r, x1 + stem // 2 - stem, cy1 + r), fill=w)
    d.ellipse((x2 - stem - 2 * r + stem // 2, cy2 - r, x2 + stem // 2 - stem, cy2 + r), fill=w)
    return img.resize((size, size), Image.LANCZOS)


def xembed_tray_present() -> bool:
    """Does an X11 system tray (XEmbed) own the tray selection? Works under
    XWayland too. KDE (xembedsniproxy), XFCE, Cinnamon, MATE and GNOME with
    the AppIndicator extension all provide one."""
    if not os.environ.get("DISPLAY"):
        return False
    try:
        from Xlib import display

        d = display.Display()
        try:
            atom = d.intern_atom(f"_NET_SYSTEM_TRAY_S{d.get_default_screen()}")
            owner = d.get_selection_owner(atom)
            return bool(getattr(owner, "id", owner))
        finally:
            d.close()
    except Exception:
        return False


async def _sni_host_present() -> bool:
    from dbus_next import BusType
    from dbus_next.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    try:
        intro = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
        iface = bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", intro)             .get_interface("org.freedesktop.DBus")
        return bool(await iface.call_name_has_owner("org.kde.StatusNotifierWatcher"))
    finally:
        bus.disconnect()


async def linux_tray_available() -> tuple[bool, str]:
    """Pick a pystray backend that will actually show an icon.

    1. AppIndicator (StatusNotifier host + PyGObject): best, but PyGObject is
       only importable from a system Python, not from the PyInstaller build.
    2. X11/XEmbed tray via python-xlib: pure Python, works in frozen builds.
    """
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, "no graphical session"
    sni = False
    try:
        sni = await _sni_host_present()
    except Exception as e:
        log.debug("StatusNotifier check failed: %s", e)
    if sni:
        try:
            import gi  # noqa: F401  (pystray's appindicator backend needs it)
            os.environ.setdefault("PYSTRAY_BACKEND", "appindicator")
            return True, "appindicator"
        except ImportError:
            pass
    loop = asyncio.get_running_loop()
    if await loop.run_in_executor(None, xembed_tray_present):
        os.environ.setdefault("PYSTRAY_BACKEND", "xorg")
        return True, "xorg"
    return False, "no tray host found (on GNOME, install the AppIndicator extension)"


class Tray:
    def __init__(self, actions: TrayActions) -> None:
        self.actions = actions
        self._icon: Any = None
        self._thread: Optional[threading.Thread] = None
        self.running = False

    def start(self) -> None:
        import pystray

        a = self.actions
        menu = pystray.Menu(
            pystray.MenuItem("Open settings", lambda *_: a.open_settings(), default=True),
            pystray.MenuItem("Copy OBS URL", lambda *_: a.copy_url()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(a.autostart_label, lambda *_: a.toggle_autostart(),
                             checked=lambda _item: bool(a.autostart_enabled())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *_: a.quit()),
        )
        self._icon = pystray.Icon("obs-now-playing", make_icon_image(), TITLE, menu)
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self.running = True
        log.info("tray icon started")

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:
            log.exception("tray stopped unexpectedly")
        finally:
            self.running = False

    def notify(self, message: str) -> None:
        if not self._icon:
            return
        try:
            self._icon.notify(message, TITLE)
        except Exception:
            log.debug("tray notification failed", exc_info=True)

    def refresh(self) -> None:
        """Re-evaluate checkmarks (e.g. after the dashboard toggled autostart)."""
        try:
            if self._icon:
                self._icon.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
