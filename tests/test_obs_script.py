"""Runs the real OBS Lua script in LuaJIT (via lupa) with a stubbed `obslua`.

On Windows this goes through the real FFI ShellExecuteW path and launches
pythonw, which records the flags it received.
"""
import os
import sys
import time

import pytest

lupa = pytest.importorskip("lupa.luajit21")

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "obs", "obs-now-playing.lua")

STUB = r"""
obslua = {
  LOG_INFO = 300, OBS_PATH_FILE = 0, OBS_TEXT_DEFAULT = 0,
  script_log = function(level, msg) table.insert(LOGS, msg) end,
  obs_properties_create = function() return {} end,
  obs_properties_add_path = function(p, name) table.insert(p, name) end,
  obs_properties_add_bool = function(p, name) table.insert(p, name) end,
  obs_properties_add_text = function(p, name) table.insert(p, name) end,
  obs_properties_add_button = function(p, name) table.insert(p, name) end,
  obs_data_set_default_string = function(s, k, v) if s[k] == nil then s[k] = v end end,
  obs_data_set_default_bool = function(s, k, v) if s[k] == nil then s[k] = v end end,
  obs_data_get_string = function(s, k) return s[k] or "" end,
  obs_data_get_bool = function(s, k) return s[k] == true end,
}
LOGS = {}
"""


def load(script_dir):
    rt = lupa.LuaRuntime()
    rt.execute(STUB)
    rt.execute(f"function script_path() return [[{script_dir}{os.sep}]] end")
    rt.execute(open(SCRIPT, encoding="utf-8").read())
    return rt


def settings(rt, **values):
    t = rt.table()
    for k, v in values.items():
        t[k] = v
    rt.globals().script_defaults(t)
    return t


def logs(rt):
    return list(rt.globals().LOGS.values())


def test_properties_and_defaults(tmp_path):
    exe = tmp_path / ("obs-now-playing.exe" if sys.platform == "win32" else "obs-now-playing")
    exe.write_bytes(b"")
    rt = load(str(tmp_path))
    s = settings(rt)
    assert s["exe"] == str(exe)  # found next to the script
    assert s["start_with_obs"] is True and s["close_with_obs"] is True
    props = list(rt.globals().script_properties().values())
    assert props == ["exe", "start_with_obs", "close_with_obs", "extra_args", "open_settings"]
    assert "Now Playing" in rt.globals().script_description()


def test_missing_app_is_logged_not_fatal(tmp_path):
    rt = load(str(tmp_path))
    s = settings(rt)
    assert s["exe"] == ""
    rt.globals().script_load(s)
    rt.globals().script_unload()  # never launched -> no quit attempt
    assert any("app not found" in m for m in logs(rt))
    assert not any("--quit-from-obs" in m for m in logs(rt))


@pytest.mark.skipif(sys.platform != "win32", reason="exercises ShellExecuteW")
def test_launches_with_flags_via_shellexecute(tmp_path):
    marker = tmp_path / "argv.txt"
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    code = f"import sys; open(r'{marker}', 'a').write(' '.join(sys.argv[1:]) + chr(10))"
    rt = load(str(tmp_path))
    s = settings(rt, exe=pyw, extra_args=f'-c "{code}"')

    def lines(n):
        deadline = time.time() + 10
        while time.time() < deadline:
            if marker.exists():
                got = marker.read_text().splitlines()
                if len(got) >= n:
                    return got
            time.sleep(0.1)
        return marker.read_text().splitlines() if marker.exists() else []

    g = rt.globals()
    g.script_load(s)
    g.script_update(s)  # must not launch a second time
    assert lines(1) == ["--from-obs"]
    g.script_unload()
    assert lines(2) == ["--from-obs", "--quit-from-obs"]


def test_start_disabled_means_no_launch_and_no_quit(tmp_path):
    rt = load(str(tmp_path))
    s = settings(rt, exe=sys.executable, start_with_obs=False)
    rt.globals().script_load(s)
    rt.globals().script_unload()
    assert not any(m.startswith("ran:") for m in logs(rt))
