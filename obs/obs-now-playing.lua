--[[
Now Playing Overlay: start and stop the app together with OBS.

Add in OBS via Tools -> Scripts -> "+" and pick this file. It ships next to
the app, so the app path is filled in automatically.

  * OBS starts  -> runs the app with --from-obs (no browser window).
  * OBS exits   -> runs the app with --quit-from-obs. The app only quits if
                   this script started it, so an app you opened yourself or
                   that started with Windows keeps running.

Uses only what OBS ships (LuaJIT + FFI); no Python needed in OBS.
]]

obs = obslua
local ffi = require("ffi")

local IS_WINDOWS = ffi.os == "Windows"
local APP_NAME = IS_WINDOWS and "obs-now-playing.exe" or "obs-now-playing"

-- Filled in by the app when it installs this script for an AppImage.
local BUNDLED_APP_PATH = ""

local cfg = { exe = "", extra_args = "", start_with_obs = true, close_with_obs = true }
local launched_this_session = false

local function log(msg)
  if obs and obs.script_log then
    obs.script_log(obs.LOG_INFO, "[now-playing] " .. msg)
  else
    print("[now-playing] " .. msg)
  end
end

local function file_exists(path)
  if not path or path == "" then return false end
  local f = io.open(path, "rb")
  if f then f:close() return true end
  return false
end

local function default_exe()
  if file_exists(BUNDLED_APP_PATH) then return BUNDLED_APP_PATH end
  local dir = (script_path and script_path()) or ""
  local candidate = dir .. APP_NAME
  if file_exists(candidate) then return candidate end
  return ""
end

-- ------------------------------------------------------------- launching

local launch_impl

if IS_WINDOWS then
  pcall(ffi.cdef, [[
    typedef uint16_t NP_WCHAR;
    int MultiByteToWideChar(unsigned int CodePage, unsigned long dwFlags, const char *lpMultiByteStr,
                            int cbMultiByte, NP_WCHAR *lpWideCharStr, int cchWideChar);
    void *ShellExecuteW(void *hwnd, const NP_WCHAR *lpOperation, const NP_WCHAR *lpFile,
                        const NP_WCHAR *lpParameters, const NP_WCHAR *lpDirectory, int nShowCmd);
  ]])
  local shell32 = ffi.load("shell32")

  local function wide(s)
    local n = ffi.C.MultiByteToWideChar(65001, 0, s, -1, nil, 0)
    local buf = ffi.new("NP_WCHAR[?]", n)
    ffi.C.MultiByteToWideChar(65001, 0, s, -1, buf, n)
    return buf
  end

  local function dirname(p)
    return (p:match("^(.*)[/\\][^/\\]*$")) or "."
  end

  -- ShellExecuteW starts the app detached and without a console window.
  launch_impl = function(exe, params)
    local r = shell32.ShellExecuteW(nil, wide("open"), wide(exe), wide(params), wide(dirname(exe)), 0)
    local code = tonumber(ffi.cast("intptr_t", r))
    return code > 32, "ShellExecute error " .. tostring(code)
  end
else
  local function sh_quote(s)
    return "'" .. (s:gsub("'", "'\\''")) .. "'"
  end
  launch_impl = function(exe, params)
    local ok = os.execute(sh_quote(exe) .. " " .. params .. " >/dev/null 2>&1 &")
    return ok == true or ok == 0, "could not start " .. exe
  end
end

local function launch(flag)
  if not file_exists(cfg.exe) then
    log("app not found. Set its path in Tools -> Scripts (" .. tostring(cfg.exe) .. ")")
    return false
  end
  -- Extra arguments go first so they can also carry an interpreter's own
  -- options (handy when running from source with python.exe).
  local params = cfg.extra_args or ""
  if flag and flag ~= "" then
    params = (params ~= "" and (params .. " ") or "") .. flag
  end
  local ok, err = launch_impl(cfg.exe, params)
  if ok then
    log("ran: " .. cfg.exe .. " " .. params)
  else
    log(err)
  end
  return ok
end

local function maybe_start()
  if launched_this_session or not cfg.start_with_obs then return end
  if launch("--from-obs") then launched_this_session = true end
end

-- --------------------------------------------------------- OBS callbacks

function script_description()
  return [[<b>Now Playing Overlay</b><br>
Starts the app together with OBS and, if OBS started it, closes it again when OBS exits.<br>
An app you started yourself (or that starts with Windows) is left running.]]
end

function script_properties()
  local props = obs.obs_properties_create()
  obs.obs_properties_add_path(props, "exe", "App", obs.OBS_PATH_FILE,
    IS_WINDOWS and "Now Playing app (*.exe)" or "", nil)
  obs.obs_properties_add_bool(props, "start_with_obs", "Start the app when OBS starts")
  obs.obs_properties_add_bool(props, "close_with_obs", "Close the app when OBS exits (only if OBS started it)")
  obs.obs_properties_add_text(props, "extra_args", "Extra arguments (optional)", obs.OBS_TEXT_DEFAULT)
  obs.obs_properties_add_button(props, "open_settings", "Open overlay settings", function()
    launch("")  -- a plain launch opens the dashboard (or starts the app if needed)
    return false
  end)
  return props
end

function script_defaults(settings)
  obs.obs_data_set_default_string(settings, "exe", default_exe())
  obs.obs_data_set_default_bool(settings, "start_with_obs", true)
  obs.obs_data_set_default_bool(settings, "close_with_obs", true)
  obs.obs_data_set_default_string(settings, "extra_args", "")
end

local function read(settings)
  cfg.exe = obs.obs_data_get_string(settings, "exe")
  cfg.extra_args = obs.obs_data_get_string(settings, "extra_args")
  cfg.start_with_obs = obs.obs_data_get_bool(settings, "start_with_obs")
  cfg.close_with_obs = obs.obs_data_get_bool(settings, "close_with_obs")
end

function script_load(settings)
  read(settings)
  maybe_start()
end

function script_update(settings)
  read(settings)
  maybe_start()  -- e.g. the path was only just set; never launches twice
end

function script_unload()
  if launched_this_session and cfg.close_with_obs then
    launch("--quit-from-obs")
  end
end
