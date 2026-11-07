/* Dashboard: settings UI, source picker, live preview, copy URL.
 *
 * Every change is saved right away (debounced while dragging) and pushed to
 * OBS over SSE by the server. While a slider moves, the preview iframe also
 * gets the unsaved values via postMessage so it reacts instantly.
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, props = {}, ...children) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") n.className = v;
      else if (k === "dataset") Object.assign(n.dataset, v);
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== undefined && v !== null && v !== false) n.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c != null) n.append(c);
    return n;
  };
  const svg = (paths) => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    s.innerHTML = paths; // static, trusted markup only
    return s;
  };
  const ICON_APP = '<g transform="translate(.5 -.5)"><path d="M9 18V6l11-2v12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="6" cy="18" r="3" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="17" cy="16" r="3" fill="none" stroke="currentColor" stroke-width="2"/></g>';
  const ICON_BROWSER = '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z" fill="none" stroke="currentColor" stroke-width="2"/>';

  const IS_WINDOWS = /Windows/i.test(navigator.userAgent);

  // Labels and presentation for each setting (schema comes from the server).
  const GROUPS = [
    ["Layout", ["scale", "card_align", "art_position", "show_art", "card_radius", "art_radius"]],
    ["Style", ["font", "title_size", "text_color", "accent_mode", "accent_color", "bg_opacity", "bg_blur", "bg_blur_amount"]],
    ["Content", ["show_album", "show_progress", "show_times", "show_player_label"]],
    ["Behavior", ["hide_when_paused", "hide_when_idle", "hide_during_ads", "fade_ms"]],
    ["System", ["start_with_os", "port"]],
  ];
  const META = {
    scale: { label: "Size", unit: "%", factor: 100, step: 0.01 },
    card_align: { label: "Position in canvas", labels: { left: "Left", center: "Center", right: "Right" } },
    art_position: { label: "Cover side", labels: { left: "Left", right: "Right" } },
    show_art: { label: "Show cover" },
    card_radius: { label: "Card corners", unit: "px" },
    art_radius: { label: "Cover corners", unit: "px" },
    font: { label: "Font" },
    title_size: { label: "Text size", unit: "px" },
    text_color: { label: "Text color" },
    accent_mode: { label: "Progress bar color", labels: { custom: "Custom", auto: "From cover" } },
    accent_color: { label: "Custom color" },
    bg_opacity: { label: "Background opacity", unit: "%", factor: 100, step: 0.01 },
    bg_blur: { label: "Blurred cover background" },
    bg_blur_amount: { label: "Blur amount", unit: "" },
    show_album: { label: "Album name" },
    show_progress: { label: "Progress bar" },
    show_times: { label: "Time labels" },
    show_player_label: { label: "App name label" },
    hide_when_paused: { label: "Hide when paused" },
    hide_when_idle: { label: "Hide when nothing is playing" },
    hide_during_ads: { label: "Hide during ads" },
    fade_ms: { label: "Fade duration", unit: "ms", step: 50 },
    start_with_os: { label: IS_WINDOWS ? "Start with Windows" : "Start when I log in" },
    port: { label: "Port" },
  };
  // Rows that only make sense with another setting enabled.
  const DEPENDS = {
    accent_color: (s) => s.accent_mode === "custom",
    bg_blur_amount: (s) => s.bg_blur,
  };

  let settings = {};
  let schema = {};
  let players = null;
  const controls = {}; // key -> { row, set(value) }
  const pending = {};  // unsaved values
  let saveTimer = 0;
  let startPort = null;

  // ------------------------------------------------------------ helpers

  function toast(msg, isErr = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.toggle("err", isErr);
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.hidden = true; }, 3500);
  }

  async function api(path, body) {
    const opts = body === undefined ? { cache: "no-store" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(path, opts);
    if (!r.ok && r.status !== 400) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  }

  const loadPref = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v === "1"; } catch (_) { return d; } };
  const savePref = (k, v) => { try { localStorage.setItem(k, v ? "1" : "0"); } catch (_) {} };

  // --------------------------------------------------------- URL + copy

  const overlayUrl = () => `${location.origin}/overlay`;

  async function copyUrl() {
    const url = overlayUrl();
    let ok = false;
    try {
      await navigator.clipboard.writeText(url);
      ok = true;
    } catch (_) {
      const ta = el("textarea", { style: "position:fixed;opacity:0" });
      ta.value = url;
      document.body.append(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch (_) {}
      ta.remove();
    }
    const btn = $("#copy");
    const label = btn.querySelector(".copy-label");
    if (ok) {
      btn.classList.add("copied");
      label.textContent = "Copied! Paste it into OBS";
      clearTimeout(copyUrl._t);
      copyUrl._t = setTimeout(() => { btn.classList.remove("copied"); label.textContent = "Copy Browser Source URL"; }, 2200);
    } else {
      toast("Couldn't copy automatically. Select the URL and press Ctrl+C.", true);
    }
  }

  // ------------------------------------------------------------ settings

  function buildSettings() {
    const host = $("#settings");
    host.replaceChildren();
    for (const [group, keys] of GROUPS) {
      const card = el("section", { class: "card group" }, el("h2", {}, group));
      for (const key of keys) if (schema[key]) card.append(buildControl(key));
      if (group === "System") card.append(el("div", { id: "obs-hint", class: "hint obs-hint", hidden: true }));
      host.append(card);
    }
  }

  function buildControl(key) {
    const f = schema[key];
    const m = META[key] || { label: key };
    const id = `s-${key}`;
    const row = el("div", { class: "row", dataset: { key } });
    let set;

    if (f.type === "bool") {
      const input = el("input", { type: "checkbox", id });
      input.addEventListener("change", () => change(key, input.checked, true));
      row.append(el("label", { for: id }, m.label),
        el("div", { class: "ctl" }, el("label", { class: "switch" }, input,
          el("span", { class: "track" }, el("span", { class: "thumb" })))));
      set = (v) => { input.checked = !!v; };
    } else if ((f.type === "int" || f.type === "float") && key !== "port") {
      const factor = m.factor || 1;
      const input = el("input", {
        type: "range", id, min: f.min, max: f.max, step: m.step || (f.type === "int" ? 1 : 0.01),
      });
      const val = el("span", { class: "val" });
      const show = (v) => { val.textContent = `${Math.round(v * factor)}${m.unit || ""}`; };
      input.addEventListener("input", () => { const v = Number(input.value); show(v); change(key, v, false); });
      input.addEventListener("change", () => change(key, Number(input.value), true));
      row.append(el("label", { for: id }, m.label), el("div", { class: "ctl" }, input, val));
      set = (v) => { input.value = v; show(v); };
    } else if (key === "port") {
      const input = el("input", { type: "number", id, min: f.min, max: f.max, step: 1 });
      input.addEventListener("change", () => {
        const v = Math.round(Number(input.value));
        if (Number.isFinite(v)) change(key, v, true);
      });
      row.append(el("label", { for: id }, m.label), el("div", { class: "ctl" }, input));
      set = (v) => { input.value = v; };
    } else if (f.type === "choice" && f.choices.length <= 3 && m.labels) {
      const seg = el("div", { class: "seg", role: "group", "aria-label": m.label });
      const buttons = f.choices.map((c) => {
        const b = el("button", { type: "button", "aria-pressed": "false", onclick: () => change(key, c, true) }, m.labels[c] || c);
        b.dataset.value = c;
        seg.append(b);
        return b;
      });
      row.append(el("span", { class: "label" }, m.label), el("div", { class: "ctl" }, seg));
      set = (v) => buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.value === v)));
    } else if (f.type === "choice") {
      const sel = el("select", { id }, f.choices.map((c) => el("option", { value: c }, c)));
      sel.addEventListener("change", () => change(key, sel.value, true));
      row.append(el("label", { for: id }, m.label), el("div", { class: "ctl" }, sel));
      set = (v) => { sel.value = v; };
    } else if (f.type === "color") {
      const input = el("input", { type: "color", id });
      input.addEventListener("input", () => change(key, input.value, false));
      input.addEventListener("change", () => change(key, input.value, true));
      row.append(el("label", { for: id }, m.label), el("div", { class: "ctl" }, input));
      set = (v) => { input.value = v; };
    } else {
      return document.createTextNode("");
    }
    controls[key] = { row, set };
    return row;
  }

  function renderSettings(fromServer) {
    const s = { ...settings, ...pending };
    for (const [key, c] of Object.entries(controls)) {
      // Never yank a control the user is interacting with.
      if (fromServer && (key in pending || c.row.contains(document.activeElement))) continue;
      c.set(s[key]);
    }
    for (const [key, cond] of Object.entries(DEPENDS)) {
      if (controls[key]) controls[key].row.hidden = !cond(s);
    }
  }

  function change(key, value, commit) {
    pending[key] = value;
    renderSettings(false);
    pushPreview();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, commit ? 0 : 200);
  }

  async function flush() {
    const body = { ...pending };
    if (!Object.keys(body).length) return;
    try {
      const res = await api("/api/settings", body);
      for (const k of Object.keys(body)) if (pending[k] === body[k]) delete pending[k];
      settings = res.settings || settings;
      const errs = Object.entries(res.errors || {});
      if (errs.length) toast(`Couldn't save ${errs.map(([k, e]) => `${(META[k] || {}).label || k}: ${e}`).join("; ")}`, true);
      renderSettings(false);
      pushPreview();
      renderBanner();
    } catch (e) {
      toast("Couldn't reach the app. Is it still running?", true);
    }
  }

  function pushPreview() {
    const frame = $("#preview");
    if (frame.contentWindow) {
      // Only unsaved values; saved ones reach the preview over its own SSE.
      const unsaved = Object.keys(pending).length ? { ...pending } : null;
      frame.contentWindow.postMessage({ type: "preview-settings", settings: unsaved }, location.origin);
    }
  }

  function renderObsHint(path) {
    const box = $("#obs-hint");
    if (!box) return;
    box.hidden = !path;
    if (!path) return;
    box.replaceChildren(
      el("b", {}, "Start and stop with OBS: "),
      "in OBS open Tools → Scripts, click +, and add ",
      el("code", { class: "path" }, path),
      ". The app then starts with OBS and closes with it (unless you started it yourself).");
  }

  function renderBanner() {
    const b = $("#banner");
    if (startPort !== null && settings.port && settings.port !== startPort) {
      b.className = "banner";
      b.textContent = `Port changed to ${settings.port}. It takes effect after restarting the app. ` +
        `Then update the OBS source to http://127.0.0.1:${settings.port}/overlay.`;
      b.hidden = false;
    } else {
      b.hidden = true;
    }
  }

  // ------------------------------------------------------------- notices

  function renderNotices(p) {
    const host = $("#notices");
    host.replaceChildren(...(p.notices || []).map((n) => el("div", {
      class: `banner ${n.level === "error" ? "err" : n.level === "info" ? "info" : ""}`,
    },
      el("span", { class: "msg" }, n.message),
      n.dismissible ? el("button", {
        type: "button", class: "x", "aria-label": "Dismiss",
        onclick: () => api("/api/notices/dismiss", { id: n.id }).then(renderNotices).catch(() => {}),
      }, "×") : null)));
  }

  // ------------------------------------------------------------- players

  function statusChip(p) {
    if (!p.running) return el("span", { class: "chip missing" }, "Not running");
    const label = { playing: "Playing", paused: "Paused", stopped: "Idle" }[p.status] || p.status;
    return el("span", { class: `chip ${p.status}` }, label);
  }

  let playersSig = "";
  function renderPlayers() {
    const host = $("#players");
    if (!players) return;
    const list = players.players || [];
    // Rebuild only on real changes so keyboard focus isn't thrown away.
    const sig = JSON.stringify(list);
    if (sig === playersSig) return;
    playersSig = sig;
    const focusedKey = host.contains(document.activeElement) ? document.activeElement.dataset.key : null;
    host.replaceChildren();
    for (const p of list) {
      let sub;
      if (!p.running) sub = "Overlay stays empty until this app is open";
      else if (p.title) sub = p.title + (p.sessions > 1 ? ` · ${p.sessions} sessions` : "");
      else sub = p.is_browser ? "No media in this browser right now" : "Nothing loaded";
      host.append(el("button", {
        type: "button", class: "player", role: "radio", "aria-checked": String(!!p.selected), dataset: { key: p.key },
        "aria-label": `${p.display_name}${p.is_browser ? " (browser)" : ""}, ${p.running ? p.status : "not running"}${p.title ? `: ${p.title}` : ""}`,
        onclick: () => { if (!p.selected) selectPlayer(p.key); },
      },
        el("span", { class: "icon" }, svg(p.is_browser ? ICON_BROWSER : ICON_APP)),
        el("span", { class: "meta" },
          el("span", { class: "name" }, p.display_name, p.is_browser ? el("span", { class: "tag" }, "Browser") : null),
          el("span", { class: "sub" }, sub)),
        statusChip(p)));
    }
    if (!list.some((p) => p.running)) {
      host.append(el("div", { class: "empty" }, "No other media apps are open. Start one and it shows up here."));
    }
    if (focusedKey) {
      const again = [...host.querySelectorAll(".player")].find((b) => b.dataset.key === focusedKey);
      if (again) again.focus();
    }
    const sel = list.find((p) => p.selected);
    if (sel && sel.is_browser) {
      host.append(el("p", { class: "player-note" },
        "Browsers show whatever plays in them, including videos. Pick a music app to avoid that."));
    }
  }

  async function selectPlayer(key) {
    if (players) {
      players.players.forEach((p) => { p.selected = p.key === key; });
      renderPlayers();
    }
    try {
      await api("/api/settings", { selected_player: key });
    } catch (_) {
      toast("Couldn't switch the source.", true);
    }
  }

  function renderStatus() {
    const st = $("#status");
    const text = st.querySelector(".status-text");
    if (!players) { st.className = "status"; text.textContent = "Connecting…"; return; }
    const b = players.backend || {};
    if (b.status && b.status !== "ok" && b.status !== "starting") {
      st.className = "status err";
      text.textContent = `Media backend problem: ${b.error || b.status}`;
    } else if (players.running) {
      st.className = "status ok";
      text.textContent = `Reading: ${players.selected_name}`;
    } else {
      st.className = "status warn";
      text.textContent = `${players.selected_name} is not running`;
    }
    renderPreviewHint();
  }

  // ------------------------------------------------------------- preview

  function setPreviewSrc() {
    const demo = $("#demo").checked;
    const src = demo ? "/overlay?demo=1" : "/overlay";
    const frame = $("#preview");
    if (frame.getAttribute("src") !== src) frame.setAttribute("src", src);
    renderPreviewHint();
  }

  function renderPreviewHint() {
    const box = $("#preview-empty");
    const demo = $("#demo").checked;
    if (demo || !players || players.running) { box.hidden = true; return; }
    box.replaceChildren(
      `${players.selected_name} is not running, so OBS shows nothing right now.`,
      el("button", { type: "button", class: "btn-link", onclick: () => { $("#demo").checked = true; savePref("demo", true); setPreviewSrc(); } },
        "Preview with a demo track"));
    box.hidden = false;
  }

  // ------------------------------------------------------------------ data

  function onSettingsPayload(p) {
    const first = !Object.keys(schema).length;
    schema = p.schema || schema;
    settings = p.settings || settings;
    if (startPort === null) startPort = settings.port;
    if (p.overlay_size) {
      $("#w").textContent = p.overlay_size.width;
      $("#h").textContent = p.overlay_size.height;
    }
    if (p.version) $("#version").textContent = `v${p.version}`;
    if (first) buildSettings();
    renderObsHint(p.obs_script_path);
    renderSettings(true);
    renderBanner();
  }

  function onPlayersPayload(p) {
    players = p;
    renderPlayers();
    renderStatus();
    renderObsConnection(p.obs_connected);
  }

  // OBS identifies itself in its user agent, so the app knows whether a
  // Browser Source is actually reading the overlay.
  function renderObsConnection(count) {
    const box = $("#obs-conn");
    if (typeof count !== "number") { box.hidden = true; return; }
    box.hidden = false;
    box.className = `obs-conn ${count > 0 ? "on" : "off"}`;
    box.replaceChildren(el("span", { class: "dot" }),
      el("span", {}, count > 0
        ? "OBS is connected and showing the overlay."
        : "OBS isn't connected. If OBS was open before this app, remove the Browser Source and add it again. The OBS script (see System below) does this automatically."));
  }

  let pollTimer = 0;
  function startPolling() {
    if (pollTimer) return;
    const poll = async () => {
      try {
        onSettingsPayload(await api("/api/settings"));
        onPlayersPayload(await api("/api/players"));
        renderNotices(await api("/api/notices"));
      } catch (_) {
        const st = $("#status");
        st.className = "status err";
        st.querySelector(".status-text").textContent = "App not reachable. Is it running?";
      }
    };
    poll();
    pollTimer = setInterval(poll, 2000);
  }
  function stopPolling() { clearInterval(pollTimer); pollTimer = 0; }

  function connect() {
    if (!window.EventSource) { startPolling(); return; }
    const es = new EventSource("/api/events");
    es.addEventListener("settings", (e) => onSettingsPayload(JSON.parse(e.data)));
    es.addEventListener("players", (e) => onPlayersPayload(JSON.parse(e.data)));
    es.addEventListener("notices", (e) => renderNotices(JSON.parse(e.data)));
    es.onopen = stopPolling;
    es.onerror = () => {
      startPolling();
      if (es.readyState === EventSource.CLOSED) setTimeout(connect, 2000);
    };
  }

  // ------------------------------------------------------------------ init

  $("#url").textContent = overlayUrl();
  $("#copy").addEventListener("click", copyUrl);
  $("#demo").checked = loadPref("demo", false);
  $("#demo").addEventListener("change", (e) => { savePref("demo", e.target.checked); setPreviewSrc(); });
  $("#preview").addEventListener("load", pushPreview);
  $("#reset").addEventListener("click", async () => {
    if (!confirm("Reset the look and behavior to defaults? Your source and system settings are kept.")) return;
    try {
      const res = await api("/api/settings/reset", {});
      settings = res.settings;
      for (const k of Object.keys(pending)) delete pending[k];
      renderSettings(false);
      pushPreview();
      toast("Defaults restored.");
    } catch (_) {
      toast("Couldn't reset settings.", true);
    }
  });
  window.addEventListener("beforeunload", () => { if (Object.keys(pending).length) flush(); });

  setPreviewSrc();
  connect();
})();
