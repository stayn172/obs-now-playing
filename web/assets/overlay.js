/* OBS overlay logic. No frameworks, no build step.
 *
 * Data: SSE (/api/events) for settings + now-playing; falls back to 1s
 * polling while SSE is down. /overlay?demo=1 (dashboard preview only) reads
 * the demo track instead. The dashboard can also postMessage unsaved
 * settings for instant preview while dragging sliders.
 *
 * Visibility: the selected app not running means nothing is drawn. Nothing
 * else is ever substituted.
 */
(() => {
  "use strict";

  const CANVAS_W = 640, CANVAS_H = 200;
  const PLACEHOLDER = "/assets/placeholder.svg";
  const HIDE_GRACE_MS = 900;     // avoid blinking on track skips / brief pauses
  const MARQUEE_SPEED = 38;      // px per second
  const MARQUEE_PAUSE = 2500;    // ms at the start of each loop
  const FONT_STACKS = {
    Inter: '"Inter", system-ui, sans-serif',
    Montserrat: '"Montserrat", system-ui, sans-serif',
    Poppins: '"Poppins", system-ui, sans-serif',
    Roboto: '"Roboto", system-ui, sans-serif',
    System: 'system-ui, -apple-system, "Segoe UI", "Noto Sans", Ubuntu, sans-serif',
  };

  const params = new URLSearchParams(location.search);
  const DEMO = params.has("demo");

  const $ = (sel) => document.querySelector(sel);
  const stage = $("#stage");
  const card = $("#card");
  const textBox = card.querySelector(".text");
  const label = card.querySelector(".label");
  const lines = {
    title: card.querySelector(".title"),
    artist: card.querySelector(".artist"),
    album: card.querySelector(".album"),
  };
  const artImgs = [...card.querySelectorAll(".art img")];
  const bgArts = [...card.querySelectorAll(".bg-art")];
  const elElapsed = card.querySelector(".elapsed");
  const elRemaining = card.querySelector(".remaining");
  const fill = card.querySelector(".fill");

  let settings = {};
  let previewOverride = null;   // unsaved settings from the dashboard
  let payload = null;           // last now-playing payload
  let np = null;                // payload.now_playing
  let clockOffset = 0;          // server clock - local clock
  let shownKey = null;          // track currently rendered
  let visible = false;
  let hideTimer = 0;
  let raf = 0;
  let lastSecond = -1;
  let artSrc = null, artFront = 0;
  let autoAccent = null;

  // ------------------------------------------------------------ helpers

  const fmt = (ms) => {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
  };

  const trackKey = (n) => (n ? [n.player_id, n.title, n.artist, n.album].join("\u0001") : null);

  function currentPos() {
    if (!np) return 0;
    let pos = np.position_ms;
    if (np.status === "playing") pos += Date.now() + clockOffset - np.position_at_ms;
    if (np.duration_ms > 0) pos = Math.min(pos, np.duration_ms);
    return Math.max(0, pos);
  }

  function effective() {
    return previewOverride ? { ...settings, ...previewOverride } : settings;
  }

  // ------------------------------------------------------------- fitting

  function fit() {
    const k = Math.min(innerWidth / CANVAS_W, innerHeight / CANVAS_H) || 1;
    const x = (innerWidth - CANVAS_W * k) / 2, y = (innerHeight - CANVAS_H * k) / 2;
    stage.style.transform = `translate(${x}px, ${y}px) scale(${k})`;
  }

  // ------------------------------------------------------------ settings

  function applySettings() {
    const s = effective();
    if (!Object.keys(s).length) return;
    const st = card.style;
    st.setProperty("--card-radius", `${s.card_radius}px`);
    st.setProperty("--art-radius", `${s.art_radius}px`);
    st.setProperty("--scale", s.scale);
    st.setProperty("--bg-opacity", s.bg_opacity);
    st.setProperty("--blur", `${Math.round(s.bg_blur_amount * 0.8)}px`);
    st.setProperty("--text", s.text_color);
    st.setProperty("--font", FONT_STACKS[s.font] || FONT_STACKS.Inter);
    st.setProperty("--title-size", `${s.title_size}px`);
    st.setProperty("--fade", `${s.fade_ms}ms`);
    applyAccent();
    card.dataset.align = s.card_align || "left";
    card.classList.toggle("art-right", s.art_position === "right");
    card.classList.toggle("no-art", !s.show_art);
    card.classList.toggle("no-blur", !s.bg_blur);
    card.classList.toggle("no-album", !s.show_album);
    card.classList.toggle("no-progress", !s.show_progress);
    card.classList.toggle("no-times", !s.show_times);
    card.classList.toggle("show-label", !!s.show_player_label);
    updateVisibility();
    scheduleMarquee();
  }

  function applyAccent() {
    const s = effective();
    const accent = s.accent_mode === "auto" && autoAccent ? autoAccent : s.accent_color || "#ffffff";
    card.style.setProperty("--accent", accent);
  }

  // Dominant vivid colour of the cover, lifted so it reads on a dark card.
  function computeAccent(img) {
    try {
      const c = document.createElement("canvas");
      c.width = c.height = 24;
      const ctx = c.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, 24, 24);
      const d = ctx.getImageData(0, 0, 24, 24).data;
      let r = 0, g = 0, b = 0, wsum = 0;
      for (let i = 0; i < d.length; i += 4) {
        const R = d[i], G = d[i + 1], B = d[i + 2];
        const max = Math.max(R, G, B), min = Math.min(R, G, B);
        const sat = max ? (max - min) / max : 0;
        const w = sat * sat * (max / 255) + 0.001;
        r += R * w; g += G * w; b += B * w; wsum += w;
      }
      r /= wsum; g /= wsum; b /= wsum;
      const max = Math.max(r, g, b), min = Math.min(r, g, b);
      if (max - min < 24) return null; // greyscale cover: keep custom colour
      const lift = Math.max(0, (200 - max) / 255); // brighten dark colours
      const mix = (v) => Math.round(v + (255 - v) * lift);
      return `rgb(${mix(r)}, ${mix(g)}, ${mix(b)})`;
    } catch (_) {
      return null;
    }
  }

  // ----------------------------------------------------------------- art

  function setArt(url) {
    const src = url || PLACEHOLDER;
    if (src === artSrc) return;
    artSrc = src;
    const img = new Image();
    img.onload = () => {
      if (artSrc !== src) return;
      const back = 1 - artFront;
      artImgs[back].src = src;
      artImgs[back].classList.add("on");
      artImgs[artFront].classList.remove("on");
      bgArts[back].style.backgroundImage = `url("${src}")`;
      bgArts[back].classList.add("on");
      bgArts[artFront].classList.remove("on");
      artFront = back;
      autoAccent = src === PLACEHOLDER ? null : computeAccent(img);
      applyAccent();
    };
    img.onerror = () => {
      if (artSrc === src && src !== PLACEHOLDER) { artSrc = null; setArt(null); }
    };
    img.src = src;
  }

  // ------------------------------------------------------------- marquee

  let marqueeQueued = false;
  function scheduleMarquee() {
    if (marqueeQueued) return;
    marqueeQueued = true;
    requestAnimationFrame(() => { marqueeQueued = false; Object.values(lines).forEach(setupMarquee); });
  }

  function setText(line, text) {
    line.dataset.text = text || "";
    setupMarquee(line);
  }

  function setupMarquee(line) {
    const text = line.dataset.text || "";
    const track = line.querySelector(".track");
    if (track._anim) { track._anim.cancel(); track._anim = null; }
    track.replaceChildren(document.createTextNode(text));
    line.classList.remove("overflow");
    if (!text || line.offsetParent === null) return;
    const textW = track.scrollWidth;
    if (textW <= line.clientWidth + 1) return;

    const gap = document.createElement("span");
    gap.className = "gap";
    const copy = document.createElement("span");
    copy.textContent = text;
    track.append(gap, copy);
    line.classList.add("overflow");
    const distance = textW + gap.offsetWidth;
    const travel = (distance / MARQUEE_SPEED) * 1000;
    const total = travel + MARQUEE_PAUSE;
    track._anim = track.animate(
      [
        { transform: "translateX(0)", offset: 0 },
        { transform: "translateX(0)", offset: MARQUEE_PAUSE / total },
        { transform: `translateX(${-distance}px)`, offset: 1 },
      ],
      { duration: total, iterations: Infinity, easing: "linear" },
    );
  }

  // ------------------------------------------------------------- content

  function renderText() {
    const s = effective();
    const idle = !np || np.status === "stopped" || !np.title;
    label.textContent = np ? np.player_name : "";
    setText(lines.title, idle ? "Nothing playing" : np.title);
    setText(lines.artist, idle ? (payload && payload.selected_name) || "" : np.artist);
    setText(lines.album, idle ? "" : np.album);
    card.classList.toggle("no-duration", !np || !(np.duration_ms > 0) || idle);
    lastSecond = -1;
    tick();
  }

  let swapping = null;
  async function swapText() {
    const token = (swapping = {});
    const out = textBox.animate(
      [{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-6px)" }],
      { duration: 180, easing: "ease-in", fill: "forwards" },
    );
    // Don't let content depend on animation frames: hidden/throttled pages
    // (OBS sources off-screen) may never advance the animation.
    await Promise.race([out.finished.catch(() => {}), new Promise((r) => setTimeout(r, 250))]);
    if (swapping !== token) { out.cancel(); return; }
    renderText();
    out.cancel();
    textBox.animate(
      [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "none" }],
      { duration: 340, easing: "cubic-bezier(.2,.8,.2,1)" },
    );
  }

  function setNowPlaying(p) {
    payload = p;
    clockOffset = (p.server_time_ms || Date.now()) - Date.now();
    np = p.now_playing || null;
    const key = trackKey(np);
    if (key !== shownKey) {
      const animate = visible && shownKey !== null && key !== null;
      shownKey = key;
      if (animate) swapText(); else renderText();
    } else if (np) {
      label.textContent = np.player_name;
      card.classList.toggle("no-duration", !(np.duration_ms > 0) || np.status === "stopped" || !np.title);
    }
    if (np) setArt(np.art_url);
    updateVisibility();
    tick();
  }

  // ---------------------------------------------------------- visibility

  function wantVisible() {
    const s = effective();
    if (!np) return false; // selected app not running: draw nothing
    if (np.is_ad && s.hide_during_ads) return false;
    const idle = np.status === "stopped" || !np.title;
    if (idle) return !s.hide_when_idle;
    if (np.status === "paused" && s.hide_when_paused) return false;
    return true;
  }

  function updateVisibility() {
    const want = wantVisible();
    if (want) {
      clearTimeout(hideTimer);
      hideTimer = 0;
      if (!visible) {
        visible = true;
        card.classList.remove("hidden");
        scheduleMarquee();
      }
    } else if (visible && !hideTimer) {
      // Not running / ads hide right away; pauses get a grace period.
      const immediate = !np || (np.is_ad && effective().hide_during_ads);
      const hide = () => { hideTimer = 0; if (!wantVisible()) { visible = false; card.classList.add("hidden"); } };
      if (immediate) hide(); else hideTimer = setTimeout(hide, HIDE_GRACE_MS);
    }
  }

  // ------------------------------------------------------------ progress

  // Safe to call any time; at most one animation frame is ever pending.
  function tick() {
    if (!np) return;
    const pos = currentPos();
    const dur = np.duration_ms || 0;
    if (dur > 0) {
      fill.style.transform = `translateX(${(pos / dur - 1) * 100}%)`;
      const sec = Math.floor(pos / 1000);
      if (sec !== lastSecond) {
        lastSecond = sec;
        elElapsed.textContent = fmt(pos);
        elRemaining.textContent = `-${fmt(dur - pos)}`;
      }
    }
    // Only animate while something is actually moving and on screen.
    if (np.status === "playing" && visible && dur > 0 && !raf) {
      raf = requestAnimationFrame(() => { raf = 0; tick(); });
    }
  }

  // --------------------------------------------------------------- data

  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${url}: ${r.status}`);
    return r.json();
  }

  function onSettings(p) {
    settings = p.settings || {};
    applySettings();
  }

  // If the app can't be reached (quit, crashed, restarting), hide the card
  // instead of showing a frozen track whose timer keeps counting up. The
  // overlay keeps retrying and comes back by itself when the app returns.
  const OFFLINE_AFTER = 2; // consecutive failed requests, about 2 seconds
  let failures = 0;
  let offline = false;
  function reachable() { failures = 0; offline = false; }
  function unreachable() {
    failures += 1;
    if (offline || failures < OFFLINE_AFTER) return;
    offline = true;
    setNowPlaying({ now_playing: null, selected_name: payload ? payload.selected_name : "", server_time_ms: Date.now() });
  }

  let pollTimer = 0;
  function startPolling() {
    if (pollTimer) return;
    const poll = async () => {
      try {
        onSettings(await getJSON("/api/settings"));
        if (!DEMO) setNowPlaying(await getJSON("/api/now-playing"));
        reachable();
      } catch (_) {
        unreachable(); // keep trying
      }
    };
    poll();
    pollTimer = setInterval(poll, 1000);
  }
  function stopPolling() { clearInterval(pollTimer); pollTimer = 0; }

  function connect() {
    if (!window.EventSource) { startPolling(); return; }
    const es = new EventSource("/api/events");
    es.addEventListener("settings", (e) => { reachable(); onSettings(JSON.parse(e.data)); });
    es.addEventListener("now-playing", (e) => { reachable(); if (!DEMO) setNowPlaying(JSON.parse(e.data)); });
    es.onopen = () => { reachable(); stopPolling(); };
    es.onerror = () => {
      startPolling(); // EventSource retries by itself; poll meanwhile
      if (es.readyState === EventSource.CLOSED) setTimeout(connect, 2000);
    };
  }

  function startDemo() {
    const poll = async () => {
      try { setNowPlaying(await getJSON("/api/demo")); reachable(); } catch (_) { unreachable(); }
    };
    poll();
    setInterval(poll, 2000);
  }

  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin || !e.data) return;
    if (e.data.type === "preview-settings") {
      previewOverride = e.data.settings || null;
      applySettings();
    }
  });

  window.addEventListener("resize", () => { fit(); scheduleMarquee(); });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleMarquee);

  fit();
  connect();
  if (DEMO) startDemo();
})();
