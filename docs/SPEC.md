# obs-now-playing — spec

A local "Now Playing" overlay for OBS. It reads the media app the user chose (Spotify by
default; Qobuz, Apple Music, browsers, VLC, etc. on request) and shows it as a customizable
browser source. Windows and Linux. No accounts, API keys, or logins. Everything stays local.

## Goal
Download a portable build from GitHub Releases, run it, click "Copy Browser Source URL" in the
dashboard that opens, paste the URL into an OBS Browser Source. Done.

## Core principles
- Zero-config: works and looks good with defaults (Spotify is the default source).
- The OBS URL is fixed with no query parameters. All settings apply live from the app.
- No Python/pip installs for end users (PyInstaller builds).
- Player-agnostic branding: no Spotify branding/logos, no "Spotify" in the app name.
- Windows 10 1809+ / 11, Linux x86_64 with MPRIS.
- No network at runtime except localhost, plus remote album-art URLs from MPRIS on Linux.

## Tech stack
Python 3.11+, aiohttp (HTTP + SSE); winrt-* packages (SMTC) on Windows; dbus-next (MPRIS) on
Linux; pystray + Pillow tray (optional on Linux); plain HTML/CSS/JS frontend, no build step,
bundled fonts; PyInstaller; GitHub Actions.

## Project structure
```
src/app.py               entry: single-instance lock, server, tray, open dashboard on first run
src/media/base.py        MediaBackend interface, PlayerInfo / NowPlaying / PlayerSnapshot, player table
src/media/windows_smtc.py  all SMTC sessions, unfiltered
src/media/linux_mpris.py   all org.mpris.MediaPlayer2.* names
src/media/manager.py     applies the user's player choice, exposes the active session
src/platform/paths.py    settings/log locations
src/platform/autostart.py  HKCU Run key / ~/.config/autostart .desktop
src/server.py            aiohttp routes
src/settings.py          load/save/validate settings.json, defaults, migration
src/tray.py              tray icon + menu (degrades gracefully)
web/overlay.html         OBS overlay (transparent)
web/dashboard.html       settings UI + live preview + copy URL
web/assets/              shared css/js, bundled fonts
tools/list_players.py    CLI: list every detected session + save cover art
build/                   PyInstaller specs, icons, AppImage recipe
.github/workflows/release.yml
```

## Player selection (changed from the original draft: no auto mode)
There is **no automatic source detection or priority logic**. It works like choosing a window
in OBS Game Capture:
- Setting `selected_player` (a player *key*), default `"spotify"`.
- Keys are stable app identifiers: `spotify`, `qobuz`, `apple-music`, `firefox`, `chrome`,
  `edge`, … for known apps (see `_KNOWN_PLAYERS` in `base.py`); the raw player id for unknown
  apps. Store and classic Spotify both map to `spotify`.
- The dashboard lists the apps open right now (grouped by key) and the user picks one. The
  selected app stays listed as "not running" when it's closed.
- If the selected app isn't running, there is no active player and the overlay renders
  **nothing** (fully transparent). Nothing else is ever substituted.
- A browser (YouTube, etc.) is only read when the user explicitly chose that browser.
- If one app has several sessions (browser tabs), show the playing one, and keep showing it
  while it plays; if none is playing, show the most recently played one.
- Removed from the draft: `player_mode` auto/locked, priority order, `excluded_players`,
  switching hysteresis.

## Media layer
- `PlayerInfo`: player_id, display_name, status (playing/paused/stopped), is_browser, key.
- `NowPlaying`: player_id, player_name, title, artist, album, status, position_ms, duration_ms,
  position_at_ms (epoch ms the position was sampled; clients extrapolate), art_version, is_ad.
- Backends emit change callbacks and run a 1s safety poll. Art is fetched separately.
- Windows: enumerate all SMTC sessions; subscribe to sessions/media/playback/timeline changes;
  extrapolate position from last_updated_time; read thumbnails via DataReader, cache by
  (player, title, artist, album); placeholder when missing.
- Linux: watch NameOwnerChanged; read Metadata/PlaybackStatus/Position; PropertiesChanged +
  1s Position poll; artUrl file:// / http(s):// / data: fetched off-loop with timeout + size
  cap; Identity for the name; strip `.instance_N` from bus names for stable ids; no D-Bus means
  a clear "no media backend" status, never a crash.
- Ads: title "Advertisement", or Spotify with an empty artist → `is_ad`; setting "Hide during ads".
- Must survive players starting, closing, and crashing mid-session.

## HTTP API (127.0.0.1 only)
| Route | Purpose |
|---|---|
| `GET /` | dashboard |
| `GET /overlay` | overlay (the OBS URL) |
| `GET /api/now-playing` | active NowPlaying (or null) + timestamp |
| `GET /api/players` | open apps (grouped by key), which is selected, running state |
| `GET /api/art` | active cover, ETag/cache headers, placeholder fallback |
| `GET/POST /api/settings` | current settings / validated partial update, persisted |
| `GET /api/events` | SSE: now-playing, players, settings (1s polling fallback) |
| `POST /api/settings/reset` | reset look/behavior to defaults (keeps source choice + system settings) |
| `GET /api/demo` | demo track for the dashboard preview; the preview iframe loads `/overlay?demo=1` (OBS never uses a query) |

SSE events: `settings`, `players`, `now-playing`; full state on connect, then only real changes (position
ticks are not pushed; clients extrapolate from `position_at_ms` + `server_time_ms`). POSTs must be JSON
from a local origin; requests with a non-local Host header are rejected (DNS-rebinding guard).

## Overlay design
Wide dark rounded translucent card; rounded-square art left; large bold white title, lighter
artist; elapsed (left) and remaining "-0:58" (right); thin progress bar (white fill on dark
translucent track); blurred, saturated copy of the art behind the card (glass). Transparent page.
Art crossfade + text slide/fade on change, marquee for long text, optional fade when paused/idle,
rAF-interpolated progress. Fixed ~640x200 canvas; card scales inside it. Optional player label,
off by default. Selected app not running → nothing drawn.

Implementation notes (milestone 4):
- The 640x200 canvas is fitted to whatever size the OBS source has (crisp at 1280x400 too), so
  `scale` (0.25–1.0) only shrinks the card inside the canvas, anchored by `card_align`.
- Glass = blurred cover layer + dark tint, both at `bg_opacity` (OBS can't blur the scene behind).
- Hiding: not running / ads hide at once; pauses get a ~0.9s grace so track skips don't blink.
- Content updates never wait on animation frames (OBS throttles off-screen sources).
- No duration reported (e.g. some browser videos) → timeline hidden.
- `/overlay?demo=1` = dashboard preview; the dashboard may `postMessage({type: "preview-settings",
  settings})` for instant, unsaved previews while dragging sliders.

## Settings (live, defaults, validated/clamped server side, schema version)
- Layout: card radius, art radius, scale (0.25–1), card align (left/center/right), art position, show/hide art
- Style: background opacity (default 0.7), blur on/off + amount, accent (custom white by default /
  auto from art), text color,
  font (bundled), title size
- Content: album, progress bar, time labels, player label
- Behavior: hide when paused, hide when nothing is playing, fade duration, hide during ads
- Player: `selected_player` (default `spotify`)
- System: start with OS, port, reset to defaults

## Dashboard UX
Copy URL button (with confirmation), the URL, recommended width/height, 3-step OBS guide.
Implementation notes (milestone 5): controls are generated from the server's schema; every change
saves immediately (sliders debounced ~200ms) and the preview also gets unsaved values via
postMessage. The source list keeps a stable order (native apps, then browsers, alphabetical; a
selected app that isn't running is listed first) and warns that a selected browser shows videos too.
A port change shows a restart banner with the new URL. "Start with OS" is stored but only takes
effect once milestone 6 wires autostart. Live
preview iframe on a checkerboard. Grouped settings. **Source picker**: list of open apps with
status and current title; click to select; the selected app is highlighted (shows "not running"
when closed). Demo track toggle. Status line: "Reading: <app>" / "<app> is not running".

## Start/stop with OBS (added after milestone 6)
- `obs/obs-now-playing.lua`: OBS Lua script (Tools → Scripts), LuaJIT + FFI only, no Python in OBS.
  Ships next to the exe so its app path is pre-filled. Options: app path, start with OBS, close with
  OBS, extra arguments (placed before the app's flags), "Open overlay settings" button.
- On load it runs `app --from-obs`; on unload `app --quit-from-obs`. Windows launches via
  `ShellExecuteW` (detached, no console); Linux via `sh … &`.
- The app records who started it (`user` / `autostart` / `obs`). `POST /api/quit`
  `{"only_if_launched_by": "obs"}` only stops an OBS-started instance, so an app the user or
  autostart started survives OBS closing. `--quit` stops unconditionally.
- `--from-obs` waits up to 4s for the lock, so "Reload scripts" (quit + start back to back) ends with
  exactly one fresh instance. If OBS crashes, the app keeps running and is reused next time.
- The dashboard's System section shows the script's path with the 3-click setup.
- Tests run the real script in LuaJIT via `lupa` (dev dependency), including the ShellExecuteW path.

## Platform, reliability, packaging
- Settings/log: next to exe if writable, else %APPDATA%\obs-now-playing; Linux ~/.config/obs-now-playing.
- Autostart: HKCU Run / ~/.config/autostart/obs-now-playing.desktop.
- Tray (pystray; Linux only with AppIndicator/SNI; GNOME needs an extension): Open settings,
  Copy OBS URL, Start with OS, Quit. Dashboard opens via webbrowser.open.
- Single instance (named mutex / lock file); second launch opens the dashboard.
- Port 8765; if taken, pick a free one once and persist it.
- Rotating log; no console window on Windows; never crash on backend errors.
- Release folders must include `obs-now-playing.lua` next to the executable.
- Windows onedir zip `obs-now-playing-vX.Y.Z-win64.zip`; Linux tarball (+ AppImage if feasible);
  Actions on `v*` tags builds both and attaches them to the Release.
- README: setup, supported players table, troubleshooting, build from source, non-affiliation disclaimer.

## Milestones
1. ✅ `tools/list_players.py` (verified on Windows: Spotify + Firefox, art saved)
2. ✅ media package + manager + unit tests (`pytest`)
3. ✅ aiohttp server with API + SSE + demo mode (settings.py + paths.py pulled forward)
4. ✅ overlay.html with animations; fonts bundled (Inter, Montserrat, Roboto, Poppins; OFL)
5. ✅ settings.py + dashboard (preview, source picker, copy button)
6. ✅ tray, single instance, autostart, port fallback, paths
   - Second launch reads `instance.json` (data folder) and opens the running dashboard.
   - Port fallback tries port+1..port+10, then any free port; persisted, with a dashboard notice.
   - Autostart: setting is the source of truth; reconciled at startup (re-points a moved portable
     folder). From source it registers `pythonw -c "...from src.app import main..." --autostart`.
   - `--autostart` never opens the browser. Without a tray (Linux w/o StatusNotifier host or
     python3-gi), the dashboard opens on manual launch and a notice explains it.
   - Notices (`GET /api/notices`, SSE `notices`, `POST /api/notices/dismiss`) carry friendly errors.
7. PyInstaller builds (Windows ✅ built + tested here; Linux pieces written, built in CI)
   - `python tools/build.py` → `dist/obs-now-playing-vX.Y.Z-win64.zip` (~19 MB, 36 MB unpacked):
     exe + `obs-now-playing.lua` + `README-FIRST.txt` beside it, onedir, no UPX, GUI subsystem,
     version resource, icon generated by `tools/make_icons.py`. Linux: `.tar.gz`, `--appimage`
     wraps it with `build/appimage/` (needs appimagetool).
   - One spec (`build/obs-now-playing.spec`) with platform branches; `NP_CONSOLE=1` builds a
     console variant whose startup errors go to stderr instead of a dialog.
   - winrt ships its own PyInstaller hook (collects the `_table.pywinrt` files).
   - Verified on Windows from a fresh unzip: track + art in ~0.7s, idle CPU 0, portable data next
     to the exe, second copy/umlaut paths, OBS script default path, `--quit`.
   - Instance lock/info live in a per-user runtime folder (`%LOCALAPPDATA%` / `$XDG_RUNTIME_DIR`)
     so any copy finds the running one. Each portable copy keeps its own settings.
   - AppImage: the app copies the OBS script to `~/.config/obs-now-playing/` with `$APPIMAGE`
     filled in, because the bundle's mount path changes every launch.
   - Still open: a clean Windows VM without Python (Windows Home has no Sandbox) and a real
     Linux desktop run.
8. ✅ release workflow + README
   - `.github/workflows/ci.yml`: tests on windows-latest + ubuntu-22.04 for pushes/PRs.
   - `.github/workflows/release.yml`: on `v*` tags checks the tag matches `src/version.py`, runs
     tests, builds Windows zip + Linux tar.gz/AppImage (ubuntu-22.04 → glibc 2.35 baseline),
     smoke-tests each packaged app (start, HTTP, `--quit`), publishes with SHA256SUMS.txt.
     "Run workflow" builds the artifacts without publishing (dry run).
   - Linux packaged builds can't import the system PyGObject, so the tray falls back to pystray's
     X11/XEmbed backend when an X11 tray owns `_NET_SYSTEM_TRAY_Sn`.
   - README screenshots come from `tools/docs_screenshots.py` (fake demo data, no real covers).
   - License: MIT (© stayn172). Releases add LICENSE.txt, THIRD_PARTY_NOTICES.txt and licenses/
     (collected by `tools/licenses.py` from the runtime dependency tree's metadata).
   - Open: repo topics (`gh repo edit --add-topic …`) once the GitHub repo exists.

## Acceptance criteria
- Fresh unzip, run: dashboard opens, URL copies, OBS shows the current Spotify track with art within 2s.
- Any setting change updates OBS without refreshing the source.
- Works with Spotify (Store + classic), Qobuz, Apple Music (app + web via browser selection), a
  browser player; Linux: Spotify + one other MPRIS player.
- Nothing but the selected app is ever shown; selected app closed means an empty overlay.
- Survives restarts, pauses, ads, skips, players closing. Idle CPU under ~1%.
