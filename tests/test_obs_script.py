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
  obs_data_release = function(d) end,
  -- sources / buttons / timers
  obs_enum_sources = function() return SOURCES end,
  source_list_release = function(list) end,
  obs_source_get_unversioned_id = function(src) return src.kind end,
  obs_source_get_settings = function(src) return src.settings end,
  obs_data_set_string = function(d, k, v) d[k] = v end,
  obs_source_update = function(src, d) table.insert(UPDATES, src.name .. "=" .. d.url) end,
  timer_add = function(fn, ms) TIMERS[fn] = ms end,
  timer_remove = function(fn) TIMERS[fn] = nil end,
}
LOGS = {}
SOURCES = {}
UPDATES = {}
TIMERS = {}
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
    assert props == ["exe", "start_with_obs", "close_with_obs", "extra_args", "open_settings", "refresh_overlay"]
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


def add_sources(rt):
    rt.execute("""
      SOURCES = {
        { name = "Now Playing", kind = "browser_source", settings = { url = "http://127.0.0.1:8765/overlay" } },
        { name = "Other port",  kind = "browser_source", settings = { url = "http://localhost:8766/overlay" } },
        { name = "Alerts",      kind = "browser_source", settings = { url = "https://dashboard.twitch.tv/widgets/alertbox" } },
        { name = "Preview",     kind = "browser_source", settings = { url = "http://127.0.0.1:8765/" } },
        { name = "Camera",      kind = "dshow_input",    settings = {} },
      }
    """)


def test_refresh_only_touches_overlay_sources(tmp_path):
    rt = load(str(tmp_path))
    add_sources(rt)
    assert rt.globals().reload_overlay_sources is None  # local, reached via the button
    s = settings(rt)
    g = rt.globals()
    props = g.script_properties()
    # call the "Refresh overlay in OBS" button through the script's own closure
    rt.execute("""
      local props = {}
      obslua.obs_properties_add_button = function(p, name, text, cb) props[name] = cb end
      script_properties()
      props.refresh_overlay()
    """)
    assert list(g.UPDATES.values()) == [
        "Now Playing=about:blank", "Now Playing=http://127.0.0.1:8765/overlay",
        "Other port=about:blank", "Other port=http://localhost:8766/overlay",
    ]
    # settings end exactly as they were (fixed URL, no query added)
    assert rt.eval("SOURCES[1].settings.url") == "http://127.0.0.1:8765/overlay"
    assert rt.eval("SOURCES[3].settings.url") == "https://dashboard.twitch.tv/widgets/alertbox"


@pytest.mark.skipif(sys.platform != "win32", reason="launches via ShellExecuteW")
def test_start_schedules_refreshes_and_unload_clears_them(tmp_path):
    marker = tmp_path / "argv.txt"
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    code = f"import sys; open(r'{marker}', 'a').write(' '.join(sys.argv[1:]) + chr(10))"
    rt = load(str(tmp_path))
    add_sources(rt)
    g = rt.globals()
    g.script_load(settings(rt, exe=pyw, extra_args=f'-c "{code}"'))
    assert sorted(g.TIMERS.values()) == [3000, 10000]
    first = next(fn for fn, ms in g.TIMERS.items() if ms == 3000)
    first()  # OBS fires the timer
    assert len(list(g.UPDATES.values())) == 4  # two overlay sources rebuilt
    assert list(g.TIMERS.values()) == [10000]  # one-shot: removed itself
    g.script_unload()
    assert len(list(g.TIMERS.values())) == 0  # pending refresh cancelled
