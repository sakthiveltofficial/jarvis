// Jarvis screen HUD: a transparent, click-through overlay (one window per monitor, opened by the desktop app).
// It listens to the agent's WebSocket and draws what Jarvis does on screen: a lock-on reticle where it clicks,
// a scan sweep when it looks, an arrow + caption when it points something out, live stats, and your hand.
// Screen coordinates from the backend are physical pixels on the whole desktop; window.__HUD__ (set by the
// desktop app) says which part of the desktop this window covers.
(() => {
  "use strict";
  const API = location.port === "8765" ? location.origin : "http://127.0.0.1:8765";
  const WS_URL = API.replace(/^http/, "ws") + "/ws";
  const $ = (id) => document.getElementById(id);
  const dpr0 = window.devicePixelRatio || 1;
  const MON = window.__HUD__ || { index: 0, x: 0, y: 0, w: Math.round(screen.width * dpr0), h: Math.round(screen.height * dpr0), scale: dpr0, primary: true };
  const CYAN = "79,216,255", AMBER = "255,179,71", RED = "255,92,108";

  let settings = null, agentState = "idle", lastActive = 0, ws = null;
  const marks = [];                 // things being drawn: {kind, x, y, ..., born, ttl}
  let hand = null, handSeen = 0, pointMark = null;

  // ------------------------------------------------------------------ coordinates
  const sc = () => MON.scale || window.devicePixelRatio || 1;
  function toLocal(sx, sy) { return [(sx - MON.x) / sc(), (sy - MON.y) / sc()]; }
  function onThisScreen(sx, sy, pad = 0) { return sx >= MON.x - pad && sx < MON.x + MON.w + pad && sy >= MON.y - pad && sy < MON.y + MON.h + pad; }

  // ------------------------------------------------------------------ canvas
  const cv = $("fx"), cx = cv.getContext("2d");
  let W = 0, H = 0;
  function resize() {
    const d = window.devicePixelRatio || 1;
    W = window.innerWidth; H = window.innerHeight;
    cv.width = Math.round(W * d); cv.height = Math.round(H * d);
    cx.setTransform(d, 0, 0, d, 0, 0);
  }
  window.addEventListener("resize", resize);
  resize();

  const ease = (t) => 1 - Math.pow(1 - Math.min(1, Math.max(0, t)), 3);
  function rgba(c, a) { return `rgba(${c},${Math.max(0, Math.min(1, a))})`; }
  function glow(c, blur = 12) { cx.shadowColor = rgba(c, 0.9); cx.shadowBlur = blur; }
  function noGlow() { cx.shadowBlur = 0; }
  function text(s, x, y, c = CYAN, a = 1, size = 11, align = "left") {
    cx.font = `${size}px "Cascadia Mono", Consolas, "SF Mono", Menlo, monospace`;
    cx.textAlign = align; cx.textBaseline = "middle";
    cx.fillStyle = rgba(c, a); cx.fillText(s, x, y);
  }

  function reticle(x, y, age, m) {
    const lock = ease(age / 0.35);
    const r = 64 - 40 * lock, fade = m.ttl - age < 0.5 ? (m.ttl - age) / 0.5 : 1;
    const col = m.kind === "right_click" ? AMBER : CYAN;
    cx.save(); cx.translate(x, y); glow(col, 14);
    cx.strokeStyle = rgba(col, 0.95 * fade); cx.lineWidth = 2;
    cx.rotate(age * 2.2 + (1 - lock) * 1.5);
    for (let i = 0; i < 4; i++) {          // four brackets closing in
      cx.beginPath(); cx.arc(0, 0, r, i * Math.PI / 2 + 0.2, i * Math.PI / 2 + 1.1); cx.stroke();
    }
    cx.rotate(-(age * 2.2 + (1 - lock) * 1.5));
    cx.lineWidth = 1; cx.strokeStyle = rgba(col, 0.7 * fade);
    cx.beginPath(); cx.moveTo(-r - 14, 0); cx.lineTo(-r + 6, 0); cx.moveTo(r - 6, 0); cx.lineTo(r + 14, 0);
    cx.moveTo(0, -r - 14); cx.lineTo(0, -r + 6); cx.moveTo(0, r - 6); cx.lineTo(0, r + 14); cx.stroke();
    if (lock >= 1) {                        // the click: ripples
      const k = (age - 0.35) / 0.8;
      for (const off of m.kind === "double_click" ? [0, 0.18] : [0]) {
        const kk = k - off;
        if (kk > 0 && kk < 1) { cx.lineWidth = 2; cx.strokeStyle = rgba(col, (1 - kk) * fade); cx.beginPath(); cx.arc(0, 0, 24 + kk * 60, 0, 6.2832); cx.stroke(); }
      }
      cx.fillStyle = rgba(col, 0.9 * fade); cx.beginPath(); cx.arc(0, 0, 3, 0, 6.2832); cx.fill();
    }
    noGlow();
    const label = { click: "CLICK", double_click: "DOUBLE CLICK", right_click: "RIGHT CLICK", move: "TARGET", scroll: "SCROLL" }[m.kind] || m.kind.toUpperCase();
    text(`${label}  ${m.sx},${m.sy}`, Math.max(r, 30) + 26, -Math.max(r, 30) * 0.7, col, 0.9 * fade, 11);
    cx.restore();
  }

  function scrollMark(x, y, age, m) {
    const fade = 1 - age / m.ttl;
    cx.save(); glow(CYAN); cx.strokeStyle = rgba(CYAN, fade); cx.lineWidth = 2;
    const dir = (m.amount || -1) > 0 ? -1 : 1;
    for (let i = 0; i < 3; i++) {
      const yy = y + dir * (((age * 90) + i * 16) % 48);
      cx.beginPath(); cx.moveTo(x - 12, yy - dir * 6); cx.lineTo(x, yy + dir * 4); cx.lineTo(x + 12, yy - dir * 6); cx.stroke();
    }
    noGlow(); cx.restore();
  }

  function dragMark(a, b, age, m) {
    const fade = m.ttl - age < 0.5 ? (m.ttl - age) / 0.5 : 1, k = ease(age / 0.6);
    const ex = a[0] + (b[0] - a[0]) * k, ey = a[1] + (b[1] - a[1]) * k;
    cx.save(); glow(CYAN); cx.strokeStyle = rgba(CYAN, 0.9 * fade); cx.lineWidth = 2; cx.setLineDash([8, 6]); cx.lineDashOffset = -age * 40;
    cx.beginPath(); cx.moveTo(a[0], a[1]); cx.lineTo(ex, ey); cx.stroke(); cx.setLineDash([]);
    const ang = Math.atan2(b[1] - a[1], b[0] - a[0]);
    cx.translate(ex, ey); cx.rotate(ang); cx.fillStyle = rgba(CYAN, fade);
    cx.beginPath(); cx.moveTo(0, 0); cx.lineTo(-14, -7); cx.lineTo(-14, 7); cx.closePath(); cx.fill();
    noGlow(); cx.restore();
    text("DRAG", a[0] + 14, a[1] - 14, CYAN, fade);
  }

  function scanMark(age, m) {
    // the part of the scanned monitor that is on this screen
    const [x0, y0] = toLocal(m.x, m.y), [x1, y1] = toLocal(m.x + m.w, m.y + m.h);
    const L = Math.max(0, x0), T = Math.max(0, y0), R = Math.min(W, x1), B = Math.min(H, y1);
    if (R <= L || B <= T) return;
    const k = age / 0.9, y = T + (B - T) * ease(k), fade = 1 - Math.max(0, (age - 0.7) / (m.ttl - 0.7));
    cx.save();
    const g = cx.createLinearGradient(0, y - 90, 0, y);
    g.addColorStop(0, rgba(CYAN, 0)); g.addColorStop(1, rgba(CYAN, 0.16 * fade));
    cx.fillStyle = g; cx.fillRect(L, Math.max(T, y - 90), R - L, Math.min(90, y - T));
    glow(CYAN, 18); cx.strokeStyle = rgba(CYAN, 0.85 * fade); cx.lineWidth = 2;
    cx.beginPath(); cx.moveTo(L, y); cx.lineTo(R, y); cx.stroke(); noGlow();
    text("SCANNING", L + 24, Math.min(B - 16, y + 14), CYAN, 0.8 * fade, 11);
    cx.restore();
  }

  function pointMarkDraw(x, y, age, m) {
    const fade = Math.min(1, age / 0.3) * (m.ttl - age < 0.6 ? (m.ttl - age) / 0.6 : 1);
    const pulse = (age % 1.2) / 1.2;
    cx.save(); glow(AMBER, 16);
    cx.strokeStyle = rgba(AMBER, fade); cx.lineWidth = 2.5;
    cx.beginPath(); cx.arc(x, y, 22, 0, 6.2832); cx.stroke();
    cx.lineWidth = 2; cx.strokeStyle = rgba(AMBER, (1 - pulse) * fade);
    cx.beginPath(); cx.arc(x, y, 22 + pulse * 46, 0, 6.2832); cx.stroke();
    // the arrow comes in from the side with more room, bobbing toward the target
    const fromRight = x < W - 260, fromBelow = y < 140;
    const dx = fromRight ? 1 : -1, dy = fromBelow ? 1 : -1;
    const bob = Math.sin(age * 5) * 6, d = 34 + bob;
    const tipX = x + dx * d * 0.7, tipY = y + dy * d * 0.7, tailX = tipX + dx * 70, tailY = tipY + dy * 70;
    cx.strokeStyle = rgba(AMBER, fade); cx.lineWidth = 4; cx.lineCap = "round";
    cx.beginPath(); cx.moveTo(tailX, tailY); cx.lineTo(tipX, tipY); cx.stroke();
    const ang = Math.atan2(tipY - tailY, tipX - tailX);
    cx.fillStyle = rgba(AMBER, fade); cx.beginPath();
    cx.moveTo(tipX + Math.cos(ang) * 6, tipY + Math.sin(ang) * 6);
    cx.lineTo(tipX + Math.cos(ang + 2.5) * 18, tipY + Math.sin(ang + 2.5) * 18);
    cx.lineTo(tipX + Math.cos(ang - 2.5) * 18, tipY + Math.sin(ang - 2.5) * 18); cx.closePath(); cx.fill();
    noGlow();
    if (m.label) {                          // caption box at the arrow's tail
      cx.font = `13px "Cascadia Mono", Consolas, "SF Mono", Menlo, monospace`;
      const tw = cx.measureText(m.label).width + 24;
      let bx = dx > 0 ? tailX + 8 : tailX - 8 - tw, by = tailY + (dy > 0 ? 6 : -34);
      bx = Math.max(8, Math.min(W - tw - 8, bx)); by = Math.max(8, Math.min(H - 36, by));
      cx.fillStyle = `rgba(6,18,28,${0.78 * fade})`; cx.strokeStyle = rgba(AMBER, 0.8 * fade); cx.lineWidth = 1;
      cx.beginPath(); cx.rect(bx, by, tw, 28); cx.fill(); cx.stroke();
      text(m.label, bx + 12, by + 14, "255,236,210", fade, 13);
    }
    cx.restore();
  }

  function keysMark(age, m) {
    const fade = m.ttl - age < 0.4 ? (m.ttl - age) / 0.4 : Math.min(1, age / 0.15);
    const s = m.kind === "type" ? `⌨  TYPING · ${m.chars} CHARACTER${m.chars === 1 ? "" : "S"}` : `⌨  ${String(m.keys || "").toUpperCase()}`;
    cx.save();
    cx.font = `12px "Cascadia Mono", Consolas, "SF Mono", Menlo, monospace`;
    const tw = cx.measureText(s).width + 30, x = W / 2 - tw / 2, y = H - 160;
    cx.fillStyle = `rgba(6,18,28,${0.7 * fade})`; cx.strokeStyle = rgba(CYAN, 0.7 * fade);
    cx.beginPath(); cx.rect(x, y, tw, 28); cx.fill(); cx.stroke();
    text(s, W / 2, y + 14, CYAN, fade, 12, "center");
    cx.restore();
  }

  // the four corner brackets glow while Jarvis is working
  let frameGlow = 0;
  function frameDraw(dt) {
    const want = agentState === "acting" ? 1 : agentState === "thinking" ? 0.45 : 0;
    frameGlow += (want - frameGlow) * Math.min(1, dt * 4);
    if (frameGlow < 0.02) return;
    const L = 46, m = 10, a = frameGlow * (0.65 + 0.35 * Math.sin(performance.now() / 300));
    cx.save(); glow(CYAN, 16); cx.strokeStyle = rgba(CYAN, a); cx.lineWidth = 3;
    for (const [x, y, sx, sy] of [[m, m, 1, 1], [W - m, m, -1, 1], [m, H - m, 1, -1], [W - m, H - m, -1, -1]]) {
      cx.beginPath(); cx.moveTo(x, y + sy * L); cx.lineTo(x, y); cx.lineTo(x + sx * L, y); cx.stroke();
    }
    noGlow(); cx.restore();
  }

  // your hand from the webcam, mapped onto the primary screen (the middle 70% of the camera covers the screen)
  function handDraw(now) {
    if (!hand || !MON.primary || settings?.hud?.hand === false) return;
    const fade = Math.max(0, 1 - (now - handSeen) / 600);
    if (fade <= 0) return;
    const x = Math.min(1, Math.max(0, (hand.x - 0.15) / 0.7)) * W, y = Math.min(1, Math.max(0, (hand.y - 0.15) / 0.7)) * H;
    const col = hand.pose === "fist" ? RED : hand.pinch ? AMBER : CYAN;
    cx.save(); glow(col, 14); cx.strokeStyle = rgba(col, 0.85 * fade); cx.lineWidth = 2;
    const r = hand.pinch ? 10 : hand.pose === "palm" ? 26 : 18;
    cx.beginPath(); cx.arc(x, y, r, 0, 6.2832); cx.stroke();
    if (hand.pinch) { cx.fillStyle = rgba(col, 0.6 * fade); cx.fill(); }
    cx.rotate(0); cx.beginPath();
    for (let i = 0; i < 3; i++) { const a0 = now / 500 + i * 2.094; cx.moveTo(x + Math.cos(a0) * (r + 8), y + Math.sin(a0) * (r + 8)); cx.arc(x, y, r + 8, a0, a0 + 0.9); }
    cx.stroke(); noGlow();
    text({ palm: "PALM", fist: "FIST", pinch: "PINCH", point: "POINT" }[hand.pose] || "HAND", x + r + 14, y, col, 0.8 * fade, 10);
    cx.restore();
  }

  let last = performance.now();
  function frame(now) {
    requestAnimationFrame(frame);
    const dt = Math.min(0.1, (now - last) / 1000); last = now;
    cx.clearRect(0, 0, W, H);
    frameDraw(dt);
    for (let i = marks.length - 1; i >= 0; i--) {
      const m = marks[i], age = (now - m.born) / 1000;
      if (age > m.ttl) { marks.splice(i, 1); if (m === pointMark) pointMark = null; continue; }
      const p = m.x !== undefined ? toLocal(m.x, m.y) : [0, 0];
      switch (m.kind) {
        case "click": case "double_click": case "right_click": case "move": reticle(p[0], p[1], age, m); break;
        case "scroll": scrollMark(p[0], p[1], age, m); break;
        case "drag": dragMark(p, toLocal(m.to_x, m.to_y), age, m); break;
        case "scan": scanMark(age, m); break;
        case "point": pointMarkDraw(p[0], p[1], age, m); break;
        case "type": case "keys": keysMark(age, m); break;
      }
    }
    handDraw(now);
  }
  requestAnimationFrame(frame);

  // ------------------------------------------------------------------ events
  function onHud(ev) {
    const now = performance.now();
    lastActive = now;
    const k = ev.kind;
    if (k === "scan") { marks.push({ ...ev, born: now, ttl: 1.3 }); return; }
    if (k === "type" || k === "keys") {
      if (!MON.primary) return;
      for (let i = marks.length - 1; i >= 0; i--) if (marks[i].kind === "type" || marks[i].kind === "keys") marks.splice(i, 1);
      marks.push({ ...ev, born: now, ttl: 1.6 }); return;
    }
    if (ev.x === undefined || ev.x === null) return;
    const here = onThisScreen(ev.x, ev.y) || (k === "drag" && ev.to_x != null && onThisScreen(ev.to_x, ev.to_y));
    if (!here) { if (k === "point" && pointMark) { pointMark.ttl = 0; } return; }
    const m = { ...ev, sx: ev.x, sy: ev.y, born: now, ttl: { move: 0.9, scroll: 1.2, drag: 1.9, point: ev.seconds || 8 }[k] || 1.6 };
    if (k === "point") { if (pointMark) pointMark.ttl = 0; pointMark = m; }
    if (k === "point" && ev.label && MON.primary) toast(ev.label, 2.5);
    marks.push(m);
  }

  let toastTimer = 0;
  function toast(s, secs = 1.6) {
    const t = $("toast"); t.textContent = s; t.classList.add("on");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("on"), secs * 1000);
  }
  const GESTURE_NAMES = { palm: "✋ open palm", fist: "✊ fist", pinch_tap: "🤏 pinch", swipe_left: "⟵ swipe", swipe_right: "swipe ⟶", swipe_up: "swipe ↑", swipe_down: "swipe ↓" };

  function onEvent(ev) {
    switch (ev.type) {
      case "hud": onHud(ev); break;
      case "state": agentState = ev.state; if (ev.state !== "idle") lastActive = performance.now(); renderState(); break;
      case "tool_start": if (!ev.agent) { $("task").textContent = "▶ " + taskText(ev); lastActive = performance.now(); } break;
      case "tool_end": if (!ev.agent && !ev.ok) $("task").textContent = "✕ that step failed"; break;
      case "plan": if (ev.agent === "main") renderPlan(ev.plan); break;
      case "hand": hand = ev.hand; if (ev.hand) handSeen = performance.now(); break;
      case "gesture":
        if (MON.primary && ev.gesture !== "lost" && GESTURE_NAMES[ev.gesture]) toast(GESTURE_NAMES[ev.gesture] + (ev.action ? " · " + ev.action : ""));
        break;
      case "wake": if (MON.primary && ev.source === "gesture") lastActive = performance.now(); break;
    }
  }
  function taskText(ev) {
    if (ev.tool === "keyboard") return "keyboard";     // typed text stays private
    const s = ev.summary ? `${ev.tool} · ${ev.summary}` : ev.tool;
    return s.length > 60 ? s.slice(0, 59) + "…" : s;
  }
  function renderState() {
    const names = { idle: "standby", listening: "listening", transcribing: "hearing you", thinking: "thinking", acting: "working", speaking: "speaking" };
    $("state").textContent = names[agentState] || agentState;
    if (agentState === "idle") setTimeout(() => { if (agentState === "idle") $("task").textContent = ""; }, 4000);
  }
  function renderPlan(plan) {
    const ul = $("plan"); ul.innerHTML = "";
    if (!plan || !plan.steps) return;
    plan.steps.slice(0, 8).forEach((st, i) => {
      const li = document.createElement("li");
      const status = st.status || "pending";
      li.className = status;
      li.textContent = `${status === "done" ? "✓" : status === "in_progress" ? "▸" : status === "failed" ? "✕" : "·"} ${i + 1}. ${st.text || st.step || st}`;
      ul.appendChild(li);
    });
  }

  // ------------------------------------------------------------------ stats panel (primary screen)
  function panelVisible() {
    const h = settings?.hud || {};
    if (!MON.primary || h.stats === false) return false;
    if (h.show === "always") return true;
    return agentState !== "idle" || performance.now() - lastActive < 5000;
  }
  function gauge(id, pct, label) {
    const c = 2 * Math.PI * 31, v = Math.max(0, Math.min(100, pct || 0));
    $("g-" + id).setAttribute("stroke-dasharray", `${(c * v) / 100} ${c}`);
    $("v-" + id).textContent = label;
  }
  const kbs = (v) => (v == null ? "–" : v > 1024 ? (v / 1024).toFixed(1) + " MB/s" : Math.round(v) + " KB/s");
  async function pollStats() {
    const on = panelVisible();
    $("panel").classList.toggle("on", on);
    if (on) {
      try {
        const s = await (await fetch(API + "/api/hud/stats")).json();
        $("clock").textContent = s.time || "";
        gauge("cpu", s.cpu, Math.round(s.cpu) + "%");
        gauge("ram", s.ram, Math.round(s.ram) + "%");
        if (s.battery != null) { gauge("bat", s.battery, s.battery + "%"); $("l-bat").textContent = s.charging ? "CHARGING" : "BATTERY"; }
        else { gauge("bat", 100, "AC"); $("l-bat").textContent = "POWER"; }
        $("v-up").textContent = kbs(s.net_up_kbs); $("v-down").textContent = kbs(s.net_down_kbs);
        const cores = $("cores"); cores.innerHTML = "";
        for (const c of s.cores || []) { const d = document.createElement("div"); d.style.height = Math.max(1, c * 0.16) + "px"; cores.appendChild(d); }
      } catch (_) {}
    }
    setTimeout(pollStats, on ? 1500 : 700);
  }

  // ------------------------------------------------------------------ connection
  async function loadSettings() {
    try { settings = (await (await fetch(API + "/api/settings")).json()).settings; } catch (_) { setTimeout(loadSettings, 2000); }
  }
  function connect() {
    ws = new WebSocket(WS_URL);
    ws.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch (_) {} };
    ws.onopen = loadSettings;
    ws.onclose = () => setTimeout(connect, 2000);
  }
  connect();
  pollStats();
  window.JarvisHud = { onEvent, marks, monitor: MON };   // handy for testing in a browser
})();
