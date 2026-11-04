import json
import os
import sys
import uuid

import pytest

from src.notices import Notices
from src.platform import autostart as au
from src.platform import single_instance as si

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_single_instance_lock(tmp_path):
    name = f"obs-now-playing-test-{uuid.uuid4().hex}"
    a = si.SingleInstance(name, lock_dir=str(tmp_path))
    b = si.SingleInstance(name, lock_dir=str(tmp_path))
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


def test_instance_info_roundtrip(tmp_path):
    p = str(tmp_path / "instance.json")
    si.write_info(12345, p)
    data = json.load(open(p))
    assert data["port"] == 12345 and data["pid"] == os.getpid()
    # nothing listens on that port -> not reachable, returns quickly
    assert si.running_dashboard_url(wait=0, path=p) is None
    si.remove_info(p)
    assert not os.path.exists(p)


def test_remove_info_leaves_other_instances_file(tmp_path):
    p = tmp_path / "instance.json"
    p.write_text(json.dumps({"pid": -1, "port": 1}))
    si.remove_info(str(p))
    assert p.exists()


def test_launch_command_from_source():
    cmd = au.launch_command()
    assert cmd[-1] == "--autostart"
    assert cmd[1] == "-c" and "from src.app import main" in cmd[2]


@windows_only
def test_windows_autostart_on_scratch_key():
    import winreg

    key = rf"Software\obs-now-playing-test-{uuid.uuid4().hex}\Run"
    a = au.Autostart(win_key=key)
    try:
        assert not a.is_enabled()
        a.reconcile(True)
        assert a.is_enabled() and a.is_current()
        assert "--autostart" in a.current()
        a.reconcile(True)  # idempotent
        a.reconcile(False)
        assert not a.is_enabled()
    finally:
        parent = key.rsplit("\\", 1)[0]
        for k in (key, parent):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
            except OSError:
                pass


def test_desktop_entry_quoting():
    assert au._desktop_quote("/usr/bin/app") == "/usr/bin/app"
    assert au._desktop_quote("/opt/My App/app") == '"/opt/My App/app"'
    assert au._desktop_quote('a"b$c`d\\e') == '"a\\"b\\$c\\`d\\\\e"'
    assert au._desktop_quote("100%") == "100%%"
    entry = au.desktop_entry()
    assert entry.startswith("[Desktop Entry]\n") and "Exec=" in entry and "--autostart" in entry


def test_desktop_path_respects_xdg(tmp_path):
    assert au.desktop_path(str(tmp_path)) == os.path.join(str(tmp_path), "autostart", "obs-now-playing.desktop")


def test_notices():
    n = Notices()
    calls = []
    n.add_listener(lambda: calls.append(1))
    n.set("a", "warn", "hello")
    n.set("a", "warn", "hello")  # unchanged -> no notify
    n.clear("a")
    n.clear("a")
    assert calls == [1, 1] and n.all() == []


def test_tray_icon_image():
    from src.tray import make_icon_image

    img = make_icon_image(64)
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0          # transparent rounded corner
    assert img.getpixel((32, 4))[3] == 255       # filled body


def test_appimage_script_install_fills_in_app_path(tmp_path, monkeypatch):
    from src.platform import paths

    bundled = tmp_path / "bundle" / "obs-now-playing.lua"
    bundled.parent.mkdir()
    bundled.write_text(open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "obs", "obs-now-playing.lua"),
                            encoding="utf-8").read(), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(paths, "data_dir", lambda: str(data))
    out = paths._install_appimage_script(str(bundled), "/home/me/Apps/Now Playing.AppImage")
    text = open(out, encoding="utf-8").read()
    assert out == str(data / "obs-now-playing.lua")
    assert 'local BUNDLED_APP_PATH = [==[/home/me/Apps/Now Playing.AppImage]==]' in text
    mtime = os.path.getmtime(out)
    paths._install_appimage_script(str(bundled), "/home/me/Apps/Now Playing.AppImage")
    assert os.path.getmtime(out) == mtime  # unchanged content is not rewritten


def test_instance_info_lives_outside_portable_data_dir(monkeypatch, tmp_path):
    from src.platform import paths
    monkeypatch.setattr(paths, "data_dir", lambda: str(tmp_path / "portable-copy"))
    assert si.info_path().startswith(paths.runtime_dir())
    assert "portable-copy" not in si.info_path()


async def test_linux_tray_needs_a_display(monkeypatch):
    from src import tray
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert await tray.linux_tray_available() == (False, "no graphical session")
    assert tray.xembed_tray_present() is False
