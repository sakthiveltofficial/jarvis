// Jarvis UI — talks to the local Python agent over HTTP + WebSocket.
const API = location.port === "8765" ? location.origin : "http://127.0.0.1:8765";
const WS_URL = API.replace(/^http/, "ws") + "/ws";
const $ = (id) => document.getElementById(id);

let ws = null;
let state = "idle";
let level = 0, targetLevel = 0;
let handsfree = false, voiceMode = "off", wakeReady = false, teaching = false;
let settings = null, presets = {};
const toolEls = {};

// ------------------------------------------------------------------ connection
async function waitForBackend() {
  for (let i = 0; ; i++) {
    try {
      const r = await fetch(API + "/api/health");
      if (r.ok) return;
    } catch (_) {}
    $("status-text").textContent = i < 20 ? "Starting my systems…" :
      "Still waiting for the backend on 127.0.0.1:8765 — see README if this persists.";
    await new Promise((r) => setTimeout(r, 700));
  }
}

function connect() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { $("conn-dot").classList.add("on"); setStatus(); };
  ws.onclose = () => { $("conn-dot").classList.remove("on"); $("status-text").textContent = "Reconnecting…"; setTimeout(connect, 1500); };
  ws.onmessage = (e) => onEvent(JSON.parse(e.data));
}
const send = (obj) => ws && ws.readyState === 1 && ws.send(JSON.stringify(obj));

// ------------------------------------------------------------------ events
function onEvent(ev) {
  orbEvent(ev);
  switch (ev.type) {
    case "state": state = ev.state; setStatus(); break;
    case "level": targetLevel = ev.value; break;
    case "handsfree": handsfree = ev.on; break;
    case "voice_mode": voiceMode = ev.mode; wakeReady = ev.wake_ready; renderHandsfree(); break;
    case "wake": $("t-voice-tile").classList.add("awake"); $("voice-mode").textContent = ev.source === "clap" ? "👏 I'm listening…" : "I'm listening…"; break;
    case "wake_sleep": $("t-voice-tile").classList.remove("awake"); renderHandsfree(); break;
    case "mood": renderMood(ev); break;
    case "plan": if (ev.agent === "main") { agenda.plan = ev.plan; renderAgenda(); } break;
    case "bg": upsertBg(ev); break;
    case "bg_update": bgNote(ev); break;
    case "agenda": loadAgenda(); break;
    case "teach": teaching = ev.on; renderTeach(); break;
    case "skills": if (!$("tab-skills").classList.contains("hidden")) loadSkills(); break;
    case "user": addMsg("user", ev.text); break;
    case "assistant": addMsg("assistant", ev.text, ev.proactive); break;
    case "info": addMsg("info", ev.text); break;
    case "error": addMsg("error", ev.text); break;
    case "tool_start": if (!ev.agent) addTool(ev); break;
    case "tool_end": if (!ev.agent) endTool(ev); break;
    case "confirm": showConfirm(ev); break;
    case "barge": onBarge(ev); break;
    case "memory_search": onMemorySearch(ev); break;
    case "memory_saving": $("graph-query").textContent = "saving what I learned…"; break;
    case "memory_update": onMemoryUpdate(ev); break;
    case "phones": if (!$("tab-connections").classList.contains("hidden")) loadPhones(); break;
    case "phone_paired": clearInterval(qrTimer); $("phone-qr").classList.add("hidden"); loadPhones(); break;
    case "confirm_done": if ($("confirm-box").dataset.id === ev.id) $("confirm-box").classList.add("hidden"); break;
  }
}

const STATUS = {
  idle: "At your service.", listening: "Listening…", transcribing: "Catching that…",
  thinking: "Thinking…", acting: "Working on it…", speaking: "Speaking…",
};
const VOICE = { idle: "Idle", listening: "Listening", transcribing: "Hearing you", thinking: "Thinking", acting: "Working", speaking: "Speaking" };
function setStatus() {
  $("status-text").textContent = STATUS[state] || state;
  $("state-label").textContent = state;
  $("voice-state").textContent = VOICE[state] || state;
  $("state-dot").classList.toggle("busy", state !== "idle");
}

function addMsg(kind, text, proactive) {
  const d = document.createElement("div");
  d.className = "msg " + kind + (proactive ? " proactive" : "");
  if (proactive) d.title = { reminder: "Reminder", offer: "Said on my own", clap: "You clapped", background: "Background task", teach: "Teach mode", skill: "New skill" }[proactive] || proactive;
  d.textContent = text;
  if (kind === "user" || kind === "assistant") $("convo-count").textContent = `${++msgCount} messages`;
  $("transcript").appendChild(d);
  d.scrollIntoView({ behavior: "smooth", block: "end" });
}

function addTool(ev) {
  const d = document.createElement("div");
  d.className = "tool run";
  d.innerHTML = `<div class="head"><b></b> <span></span></div><div class="body hidden"></div>`;
  d.querySelector("b").textContent = ev.tool;
  d.querySelector("span").textContent = ev.summary;
  d.querySelector(".head").onclick = () => d.querySelector(".body").classList.toggle("hidden");
  toolEls[ev.id] = d;
  $("transcript").appendChild(d);
  d.scrollIntoView({ block: "end" });
}
function endTool(ev) {
  const d = toolEls[ev.id];
  if (!d) return;
  d.className = "tool " + (ev.ok ? "ok" : "err");
  ++actionCount;
  const body = d.querySelector(".body");
  const pre = document.createElement("pre");
  pre.textContent = ev.text;
  body.appendChild(pre);
  if (ev.image) {
    const img = document.createElement("img");
    img.src = "data:image/png;base64," + ev.image;
    body.appendChild(img);
  }
  if (!ev.ok) body.classList.remove("hidden");
}

function showConfirm(ev) {
  const box = $("confirm-box");
  box.dataset.id = ev.id;
  box.innerHTML = `<h4>Permission needed</h4><div class="reason"></div><code></code>
    <div class="btns"><button class="pill" data-a="0">Deny</button><button class="pill primary" data-a="1">Allow</button></div>
    <div class="hint" style="margin-top:6px;font-size:12px">You can also just say “yes” or “no”.</div>`;
  box.querySelector(".reason").textContent = `${ev.reason} — ${ev.tool}`;
  box.querySelector("code").textContent = ev.summary;
  box.querySelectorAll("button").forEach((b) => b.onclick = () => {
    send({ type: "confirm", id: ev.id, approve: b.dataset.a === "1" });
    box.classList.add("hidden");
  });
  box.classList.remove("hidden");
}

// ------------------------------------------------------------------ input
$("composer").onsubmit = (e) => {
  e.preventDefault();
  const t = $("input").value.trim();
  if (!t) return;
  send({ type: "user_text", text: t });
  $("input").value = "";
};

let pttDown = false;
function pttStart() { if (pttDown) return; pttDown = true; $("btn-mic").classList.add("active"); send({ type: "ptt_start" }); }
function pttStop() { if (!pttDown) return; pttDown = false; $("btn-mic").classList.remove("active"); send({ type: "ptt_stop" }); }
$("btn-mic").addEventListener("pointerdown", pttStart);
window.addEventListener("pointerup", pttStop);
window.addEventListener("keydown", (e) => {
  if (e.code === "Space" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA"
      && $("settings").classList.contains("hidden")) { e.preventDefault(); pttStart(); }
  if (e.code === "Escape") send({ type: "stop" });
});
window.addEventListener("keyup", (e) => { if (e.code === "Space") pttStop(); });

const MODES = ["off", "wake", "handsfree"];
const setMode = (m) => send({ type: "voice_mode", mode: m });
$("btn-handsfree").onclick = () => setMode(MODES[(MODES.indexOf(voiceMode) + 1) % 3]);
document.querySelectorAll("#voice-seg button").forEach((b) => b.onclick = () => setMode(b.dataset.mode));
function renderHandsfree() {
  const name = settings?.assistant_name || "Jarvis";
  const clap = settings?.voice?.clap !== false ? " or clap twice" : "";
  const label = { off: "push to talk", wake: (wakeReady ? `say “Hey ${name}”` : `say “${name}…”`) + clap, handsfree: "always listening" }[voiceMode];
  $("btn-handsfree").classList.toggle("on", voiceMode !== "off");
  $("btn-handsfree").title = `Listening: ${label} (click to change)`;
  $("hf-label").textContent = label;
  $("voice-mode").textContent = label;
  $("t-voice-tile").classList.remove("awake");
  document.querySelectorAll("#voice-seg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === voiceMode));
}
function renderMood(m) {
  const chip = $("mood-chip");
  if (!m.mood || m.mood === "neutral") { chip.classList.add("hidden"); return; }
  chip.textContent = { stressed: "you sound stressed", excited: "you sound excited", down: "you sound a bit down", tired: "you sound tired" }[m.mood] || m.mood;
  chip.title = (m.cues || []).join("; ");
  chip.classList.remove("hidden");
  clearTimeout(renderMood.t); renderMood.t = setTimeout(() => chip.classList.add("hidden"), 300000);
}

// teach mode
async function toggleTeach() {
  await fetch(API + "/api/teach", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: teaching ? "stop" : "start" }) });
}
$("btn-teach").onclick = toggleTeach;
$("btn-teach2").onclick = toggleTeach;
function renderTeach() {
  $("btn-teach").classList.toggle("rec", teaching);
  $("btn-teach").title = teaching ? "Watching… click when you're done" : "Teach me a workflow (I'll watch)";
  $("btn-teach2").textContent = teaching ? "I'm done, save the skill" : "Teach me something";
}

// ------------------------------------------------------------------ agenda (plan, background tasks, schedule)
let agenda = { plan: null, background: [], schedules: [] };
async function loadAgenda() {
  try { agenda = await (await fetch(API + "/api/agenda")).json(); } catch (_) { return; }
  voiceMode = agenda.voice_mode; wakeReady = agenda.wake_ready; teaching = agenda.teach;
  renderHandsfree(); renderTeach(); renderAgenda();
  if (agenda.mood) renderMood(agenda.mood);
}
function upsertBg(ev) {
  const i = agenda.background.findIndex((b) => b.id === ev.id);
  if (i >= 0) agenda.background[i] = ev; else agenda.background.push(ev);
  renderAgenda();
}
function bgNote(ev) {
  const b = agenda.background.find((x) => x.id === ev.id);
  const d = document.createElement("div");
  d.className = "msg bg";
  d.textContent = `⟳ ${b ? b.goal.slice(0, 40) : "background"}: ${ev.text}`;
  $("transcript").appendChild(d);
  d.scrollIntoView({ block: "end" });
}
function el(tag, cls, text) { const d = document.createElement(tag); if (cls) d.className = cls; if (text !== undefined) d.textContent = text; return d; }
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((new Date(d.toDateString()) - new Date(now.toDateString())) / 864e5);
  return days === 0 ? t : days === 1 ? "tomorrow " + t : d.toLocaleDateString([], { weekday: "short" }) + " " + t;
}
function renderAgenda() {
  const now = $("ag-now"), next = $("ag-next");
  now.replaceChildren(); next.replaceChildren();
  const p = agenda.plan;
  const running = agenda.background.filter((b) => b.status === "running");
  if (p && p.steps && !p.complete) {
    const done = p.steps.filter((x) => x.status === "done" || x.status === "skipped").length;
    now.appendChild(el("div", "ag-title", p.title));
    const bar = el("div", "ag-bar"); const i = el("i"); i.style.width = `${(100 * done) / p.steps.length}%`; bar.appendChild(i); now.appendChild(bar);
    const marks = { done: "✓", in_progress: "→", failed: "✕", skipped: "–", pending: "·" };
    p.steps.forEach((st, k) => {
      const r = el("div", "ag-step " + st.status);
      r.append(el("span", "m", marks[st.status] || "·"), el("span", "", `${st.text}${st.note ? " — " + st.note : ""}`));
      now.appendChild(r);
    });
  }
  for (const b of agenda.background.slice().reverse().slice(0, 4)) {
    const r = el("div", "ag-item " + b.status);
    r.append(el("span", "t", b.goal), el("span", "w", b.status === "running" ? `${b.steps} actions` : b.status));
    r.title = b.result || b.goal;
    if (b.status === "running") {
      const x = el("button", "", "✕"); x.title = "Cancel";
      x.onclick = () => fetch(API + `/api/background/${b.id}/cancel`, { method: "POST" });
      r.appendChild(x);
    }
    now.appendChild(r);
  }
  if (!now.children.length) now.appendChild(el("div", "ag-empty", "Nothing in progress. Big jobs show their plan here."));
  const up = agenda.schedules.filter((j) => j.enabled !== false).slice(0, 6);
  for (const j of up) {
    const r = el("div", "ag-item");
    r.append(el("span", "t", (j.kind === "reminder" ? "⏰ " : j.kind === "briefing" ? "☀ " : "▶ ") + j.title), el("span", "w", when(j.next_run)));
    r.title = `${j.kind}: ${j.message} (${j.when})`;
    next.appendChild(r);
  }
  if (!up.length) next.appendChild(el("div", "ag-empty", "Nothing scheduled. Try “remind me at 6 to call Mum”."));
  $("agenda-sub").textContent = running.length ? `${running.length} running in background` : "";
}
$("btn-stop").onclick = () => send({ type: "stop" });
$("btn-new").onclick = async () => {
  await fetch(API + "/api/reset", { method: "POST" });
  $("transcript").innerHTML = "";
  msgCount = 0; $("convo-count").textContent = "";
  addMsg("info", "New conversation.");
};

// ------------------------------------------------------------------ presence (soft gradient + wireframe)
const cv = $("orb"), cx = cv.getContext("2d");
const blob = document.createElement("canvas"); blob.width = blob.height = 64;
const bx = blob.getContext("2d");
const PALETTE = {               // two blob colours per state
  idle: [[126, 176, 104], [233, 214, 106]], listening: [[200, 236, 80], [240, 205, 90]],
  transcribing: [[200, 236, 80], [240, 205, 90]], thinking: [[150, 165, 185], [236, 160, 112]],
  acting: [[236, 150, 100], [242, 196, 82]], speaking: [[120, 186, 96], [214, 243, 74]],
};
let t0 = performance.now(), pal = PALETTE.idle.map((c) => c.slice());
function ink(a) { return document.documentElement.dataset.theme === "dark" ? `rgba(236,239,236,${a})` : `rgba(23,25,26,${a})`; }
function draw(now) {
  const t = (now - t0) / 1000;
  level += (targetLevel - level) * 0.18;
  targetLevel *= 0.93;
  if (orb3d) { drawWave(t); requestAnimationFrame(draw); return; }   // the particle orb draws itself
  const target = PALETTE[state] || PALETTE.idle;
  pal = pal.map((c, j) => c.map((v, i) => v + (target[j][i] - v) * 0.04));
  const busy = state === "thinking" || state === "acting" || state === "transcribing";
  const W = cv.width, H = cv.height, C = W / 2;

  // 1) soft blurred blob: paint gradients small, then upscale (cheap, smooth blur)
  bx.clearRect(0, 0, 64, 64);
  const blobs = [
    [32 + Math.sin(t * 0.6) * 6, 30 + Math.cos(t * 0.5) * 5, 20 + level * 8, pal[0], 0.95],
    [30 + Math.cos(t * 0.45) * 7, 36 + Math.sin(t * 0.7) * 6, 17 + level * 6, pal[1], 0.85],
    [34 + Math.sin(t * 0.9 + 1) * 5, 32 + Math.cos(t * 0.8) * 5, 12, [255, 255, 255], 0.55],
  ];
  for (const [x, y, r, c, a] of blobs) {
    const g = bx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, `rgba(${c.map(Math.round)},${a})`);
    g.addColorStop(1, `rgba(${c.map(Math.round)},0)`);
    bx.fillStyle = g; bx.fillRect(0, 0, 64, 64);
  }
  cx.clearRect(0, 0, W, H);
  cx.imageSmoothingEnabled = true; cx.imageSmoothingQuality = "high";
  const S = W * (0.92 + level * 0.12);
  cx.globalAlpha = 0.95;
  cx.drawImage(blob, C - S / 2, C - S / 2, S, S);
  cx.globalAlpha = 1;

  // 2) thin wireframe: two stacked spheres (hourglass), slowly rotating
  const R = W * 0.17 * (1 + level * 0.06);
  const spin = t * (busy ? 0.9 : 0.25);
  cx.lineWidth = 1;
  for (const cy of [C - R * 0.98, C + R * 0.98]) {
    for (let i = 1; i < 9; i++) {           // latitude rings
      const lat = (i / 9) * Math.PI - Math.PI / 2;
      const ry = Math.cos(lat) * R, yy = cy + Math.sin(lat) * R;
      cx.beginPath(); cx.ellipse(C, yy, ry, ry * 0.22, 0, 0, Math.PI * 2);
      cx.strokeStyle = ink(0.10 + 0.05 * Math.abs(Math.sin(lat))); cx.stroke();
    }
    for (let i = 0; i < 10; i++) {          // meridians
      const a = spin + (i / 10) * Math.PI;
      cx.beginPath(); cx.ellipse(C, cy, Math.abs(Math.cos(a)) * R, R, 0, 0, Math.PI * 2);
      cx.strokeStyle = ink(0.12); cx.stroke();
    }
  }
  // 3) lime focal dot with a level halo
  const halo = 10 + level * 40 + (busy ? 6 * (0.5 + 0.5 * Math.sin(t * 6)) : 0);
  const g = cx.createRadialGradient(C, C, 0, C, C, halo * 2);
  g.addColorStop(0, "rgba(214,243,74,0.55)"); g.addColorStop(1, "rgba(214,243,74,0)");
  cx.fillStyle = g; cx.beginPath(); cx.arc(C, C, halo * 2, 0, Math.PI * 2); cx.fill();
  cx.fillStyle = "#d6f34a"; cx.beginPath(); cx.arc(C, C, 6, 0, Math.PI * 2); cx.fill();
  cx.strokeStyle = ink(0.35); cx.lineWidth = 1; cx.beginPath(); cx.arc(C, C, 10, 0, Math.PI * 2); cx.stroke();
  // busy: thin orbit arc
  if (busy) {
    cx.strokeStyle = ink(0.45); cx.lineWidth = 1.2;
    cx.beginPath(); cx.arc(C, C, R * 2.25, spin * 2, spin * 2 + 0.9); cx.stroke();
  }
  drawWave(t);
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);

// ------------------------------------------------------------------ particle orb (orb.js; falls back to the glow above)
let orb3d = null, orbStarting = false, orbFailed = false;
const curTheme = () => (document.documentElement.dataset.theme === "dark" ? "dark" : "light");
async function applyOrb() {
  const ui = settings?.ui || {};
  const want = ui.orb !== false && !orbFailed && !!window.JarvisOrb;
  if (want && !orb3d && !orbStarting) {
    orbStarting = true;
    try { orb3d = await window.JarvisOrb.start($("stage"), { quality: ui.orb_quality, theme: curTheme() }); }
    catch (e) { console.warn("[orb]", e); orb3d = null; }
    orbStarting = false;
    if (!orb3d) { orbFailed = true; return; }          // no WebGL: keep the glow
    orb3d.setState(state);
    if (settings?.ui?.orb === false) { orb3d.destroy(); orb3d = null; }   // turned off while it was loading
  } else if (!want && orb3d) {
    orb3d.destroy(); orb3d = null;
  }
  if (orb3d) orb3d.setQuality(ui.orb_quality || "auto");
  $("orb").classList.toggle("hidden", !!orb3d);
}
function orbEvent(ev) {
  if (!orb3d || ev.agent) return;
  switch (ev.type) {
    case "state": orb3d.setState(ev.state); break;
    case "level": orb3d.level(ev.source, ev.value); break;
    case "mood": orb3d.userMood(ev.mood); break;
    case "ai_mood": orb3d.aiMood(ev.mood); break;
    case "tool_start": orb3d.task(ev.tool); break;
    case "tool_end": if (!ev.ok) orb3d.flash(); break;
    case "error": orb3d.flash(); break;
    case "memory_search": orb3d.task("memory_search", 2.0); break;
  }
}

// voice tile: thin layered sine waves that follow the audio level
const wv = $("wave"), wx = wv.getContext("2d");
const levels = new Array(60).fill(0);
let lastPush = 0;
function drawWave(t) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(wv.clientWidth * dpr), h = Math.round(wv.clientHeight * dpr);
  if (!w || !h) return;
  if (wv.width !== w || wv.height !== h) { wv.width = w; wv.height = h; }
  if (t - lastPush > 0.05) { levels.push(level); levels.shift(); lastPush = t; }
  wx.clearRect(0, 0, w, h);
  const amp = levels.reduce((a, b) => a + b, 0) / levels.length;
  for (let k = 0; k < 5; k++) {
    wx.beginPath();
    for (let x = 0; x <= w; x += 4 * dpr) {
      const p = x / w;
      const env = Math.sin(p * Math.PI);
      const y = h * 0.5 + Math.sin(p * (8 + k * 1.3) + t * (1.5 + k * 0.4) + k) * env * h * (0.06 + amp * 0.42) * (1 - k * 0.12);
      x ? wx.lineTo(x, y) : wx.moveTo(x, y);
    }
    wx.strokeStyle = `rgba(255,255,255,${0.85 - k * 0.14})`; wx.lineWidth = (1.4 - k * 0.15) * dpr; wx.stroke();
  }
  // moving dot like a playhead
  const px = w * (0.15 + 0.7 * ((t * 0.08) % 1)), py = h * 0.5;
  wx.fillStyle = "#d6f34a"; wx.beginPath(); wx.arc(px, py, 4 * dpr, 0, Math.PI * 2); wx.fill();
}

// decorative wireframes in the small tiles
function wire(id, kind) {
  const g = $(id); let out = "";
  if (kind === "onion") for (let i = 1; i <= 9; i++) out += `<ellipse cx="50" cy="55" rx="${i * 4.5}" ry="${i * 5.5}" transform="rotate(${i * 6} 50 55)"/>`;
  else for (let i = 0; i < 10; i++) out += `<ellipse cx="50" cy="${25 + i * 6}" rx="${40 - i * 4}" ry="${5 - i * .35}"/>`;
  g.innerHTML = out;
}
wire("wire-a", "onion");

// greeting + labels
function greet() {
  const h = new Date().getHours();
  const part = h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  const who = settings?.user_name ? `,\n${settings.user_name}` : "";
  $("greeting").textContent = part + who;
  $("greeting").style.whiteSpace = "pre-line";
}
let actionCount = 0, recallCount = 0, msgCount = 0;
function renderLabels() {
  if (!settings) return;
  greet();
  $("brand-name").textContent = settings.assistant_name || "Jarvis";
  $("model-label").textContent = settings.llm.model || settings.llm.provider;
  const stt = settings.stt.provider === "local_whisper" ? `Whisper ${settings.stt.local_model}` : settings.stt.provider;
  const tts = { piper: "Piper", kokoro: "Kokoro", clone: "custom voice", elevenlabs: "ElevenLabs", fish_audio: "Fish Audio", openai: "OpenAI", none: "silent" }[settings.tts.provider] || settings.tts.provider;
  $("stt-label").textContent = "hear · " + stt;
  $("tts-label").textContent = "speak · " + tts;
  const mode = settings.agent.confirm_mode;
  $("stat-confirm").textContent = { risky: "Careful", always: "Cautious", never: "Autonomous" }[mode] || mode;
  $("stat-confirm-sub").textContent = { risky: "asks before risky actions", always: "asks before every change", never: "acts without asking" }[mode] || "";
}
const toggleTheme = () => {
  const dark = document.documentElement.dataset.theme !== "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  if (orb3d) orb3d.setTheme(dark ? "dark" : "light");
  try { localStorage.setItem("jarvis-theme", dark ? "dark" : "light"); } catch (_) {}
};
try { const th = localStorage.getItem("jarvis-theme"); if (th) document.documentElement.dataset.theme = th; } catch (_) {}
$("btn-theme").onclick = toggleTheme;

// ------------------------------------------------------------------ settings
$("btn-settings").onclick = openSettings;
$("btn-close-settings").onclick = () => $("settings").classList.add("hidden");
document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("hidden", x.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "models") refreshModels();
  if (b.dataset.tab === "skills") loadSkills();
  if (b.dataset.tab === "connections") loadConnections();
  if (b.dataset.tab === "proactive") renderSchedules();
});

function opt(sel, items, value) {
  sel.innerHTML = "";
  for (const it of items) {
    const o = document.createElement("option");
    o.value = it.value ?? it; o.textContent = it.label ?? it;
    sel.appendChild(o);
  }
  if (value !== undefined && value !== null) sel.value = String(value);
}

async function loadSettings() {
  const r = await (await fetch(API + "/api/settings")).json();
  settings = r.settings; presets = r.presets;
  renderLabels();
  applyOrb();
}

async function openSettings() {
  await loadSettings();
  const s = settings, l = s.llm;
  opt($("llm-provider"), Object.keys(presets).map((k) => ({ value: k, label: k })), l.provider);
  $("llm-base").value = l.base_url || ""; $("llm-model").value = l.model || "";
  $("llm-key").value = s.api_keys[l.provider] || "";
  $("llm-vision").checked = !!l.vision; $("llm-effort").value = l.effort || ""; $("llm-temp").value = l.temperature;
  $("llm-provider").onchange = () => {
    const p = presets[$("llm-provider").value];
    $("llm-base").value = p.base_url; $("llm-model").value = p.model; $("llm-vision").checked = p.vision;
    $("llm-key").value = settings.api_keys[$("llm-provider").value] || "";
  };

  const m = await (await fetch(API + "/api/models")).json();
  $("stt-provider").value = s.stt.provider;
  opt($("stt-model"), m.whisper.map((w) => ({ value: w.id, label: `${w.id} (${w.size_mb} MB)${w.installed ? " ✓" : ""}` })), s.stt.local_model);
  $("stt-lang").value = s.stt.language || ""; $("stt-device").value = s.stt.device;
  $("tts-provider").value = s.tts.provider;
  opt($("tts-piper"), m.piper.map((v) => ({ value: v.id, label: v.label + (v.installed ? " ✓" : "") })), s.tts.piper_voice);
  opt($("tts-kokoro"), m.kokoro_voices, s.tts.kokoro_voice);
  $("tts-el-voice").value = s.tts.elevenlabs_voice_id || ""; $("tts-fish").value = s.tts.fish_reference_id || "";
  $("tts-oa-voice").value = s.tts.openai_voice || ""; $("tts-speed").value = s.tts.speed;
  for (const k of ["elevenlabs", "fish_audio", "openai", "groq"]) $("key-" + k).value = s.api_keys[k] || "";
  $("clone-engine").value = s.tts.clone_engine || "fish_speech"; $("clone-url").value = s.tts.clone_url || "";
  $("clone-fallback").value = s.tts.clone_fallback || "piper";
  const lg = s.language || {};
  $("lang-mode").value = lg.mode || "ta_en"; $("lang-reply").value = lg.reply || "match";
  $("lang-tvoice").value = lg.tamil_voice || "auto"; $("lang-edge").value = lg.edge_voice || "ta-IN-ValluvarNeural";
  loadClone();

  const d = await (await fetch(API + "/api/audio/devices")).json();
  opt($("audio-in"), [{ value: "", label: "System default" }, ...d.inputs.map((x) => ({ value: x.id, label: x.name }))], s.audio.input_device ?? "");
  opt($("audio-out"), [{ value: "", label: "System default" }, ...d.outputs.map((x) => ({ value: x.id, label: x.name }))], s.audio.output_device ?? "");

  $("beh-name").value = s.assistant_name; $("beh-user").value = s.user_name || "";
  $("beh-confirm").value = s.agent.confirm_mode; $("beh-steps").value = s.agent.max_steps;
  $("beh-silence").value = s.audio.silence_ms; $("beh-speak").checked = s.agent.speak_replies;
  $("beh-headless").checked = s.agent.headless_browser;
  $("ui-orb").checked = s.ui?.orb !== false; $("ui-orb-q").value = s.ui?.orb_quality || "auto";
  $("mem-auto").checked = s.memory?.auto_save !== false;
  $("barge-on").checked = s.audio.barge_in !== false;
  $("barge-echo").value = s.audio.echo_gain ?? 0.6;
  $("barge-ms").value = s.audio.barge_check_ms ?? 900;
  const v = s.voice || {}, br = s.briefings || {}, aw = s.awareness || {}, nd = s.node || {};
  $("pv-mode").value = v.mode || "wake"; $("pv-thr").value = v.wake_threshold ?? 0.5;
  $("pv-follow").value = v.follow_up_s ?? 10; $("pv-listen").value = v.listen_s ?? 8;
  $("pv-chime").checked = v.chime !== false; $("pv-emotion").checked = v.emotion !== false;
  $("pv-clap").checked = v.clap !== false; $("pv-clap-s").value = v.clap_sensitivity ?? 0.5; $("pv-clap-ack").checked = v.clap_ack !== false;
  const am = s.ambient || {};
  $("am-on").checked = am.enabled !== false; $("am-quiet").checked = am.quiet_enabled !== false;
  $("am-q-start").value = am.quiet_start || "23:00"; $("am-q-end").value = am.quiet_end || "07:00";
  $("am-code").checked = am.coding_break !== false; $("am-code-h").value = am.coding_break_h ?? 2;
  $("am-screen").checked = am.screen_break !== false; $("am-screen-h").value = am.screen_break_h ?? 3;
  $("am-welcome").checked = am.welcome_back !== false; $("am-gap").value = am.min_gap_min ?? 45; $("am-late").checked = !!am.late_night;
  $("pv-selfcheck").checked = s.agent.self_check !== false;
  $("br-loc").value = br.location || "";
  for (const [k, id] of [["morning", "m"], ["evening", "e"]]) {
    const b = br[k] || {};
    $(`br-${id}-on`).checked = !!b.enabled; $(`br-${id}-time`).value = b.time || (k === "morning" ? "08:00" : "18:30");
    $(`br-${id}-wd`).checked = !!b.weekdays_only;
  }
  $("aw-on").checked = !!aw.enabled; $("aw-int").value = aw.interval_s ?? 60; $("aw-cool").value = aw.cooldown_min ?? 10; $("aw-model").value = aw.model || "";
  $("mcp-json").value = JSON.stringify((s.mcp && s.mcp.servers) || [], null, 2);
  const ha = s.home_assistant || {};
  $("ha-on").checked = ha.enabled !== false; $("ha-url").value = ha.url || ""; $("ha-token").value = ha.token || "";
  $("ha-ssl").checked = ha.verify_ssl !== false; $("ha-status").textContent = ""; $("ha-status").className = "hint";
  const ph = s.phones || {};
  $("phones-on").checked = !!ph.enabled; $("phones-port").value = ph.port || 8767; $("phones-hosts").value = (ph.extra_hosts || []).join(", ");
  $("node-on").checked = !!nd.enabled; $("node-name").value = nd.name || ""; $("node-port").value = nd.port || 8766; $("node-elev").checked = !!nd.allow_elevated;
  pcs = (s.nodes || []).map((x) => ({ ...x })); renderPcs();
  renderSchedules();
  try {
    const g = await (await fetch(API + "/api/graph?include_invalid=true")).json();
    const valid = g.edges.filter((e) => e.valid).length;
    $("mem-stats").textContent = `${g.nodes.length} entities, ${valid} current facts, ${g.edges.length - valid} outdated facts kept as history.`;
  } catch (_) { $("mem-stats").textContent = "Graph memory unavailable."; }
  $("save-msg").textContent = "";
  $("settings").classList.remove("hidden");
}

function collect() {
  const prov = $("llm-provider").value;
  const keys = {};
  for (const k of ["elevenlabs", "fish_audio", "groq", "openai"]) keys[k] = $("key-" + k).value;
  if ($("llm-key").value) keys[prov] = $("llm-key").value;
  const dev = (v) => (v === "" ? null : Number(v));
  return {
    assistant_name: $("beh-name").value || "Jarvis",
    user_name: $("beh-user").value,
    llm: {
      provider: prov, kind: presets[prov].kind, base_url: $("llm-base").value.trim(), model: $("llm-model").value.trim(),
      vision: $("llm-vision").checked, effort: $("llm-effort").value, temperature: Number($("llm-temp").value || 0.7),
    },
    api_keys: keys,
    stt: { provider: $("stt-provider").value, local_model: $("stt-model").value, language: $("stt-lang").value.trim(), device: $("stt-device").value },
    tts: {
      provider: $("tts-provider").value, piper_voice: $("tts-piper").value, kokoro_voice: $("tts-kokoro").value,
      elevenlabs_voice_id: $("tts-el-voice").value.trim(), fish_reference_id: $("tts-fish").value.trim(),
      openai_voice: $("tts-oa-voice").value.trim() || "onyx", speed: Number($("tts-speed").value || 1),
      clone_engine: $("clone-engine").value, clone_url: $("clone-url").value.trim(), clone_fallback: $("clone-fallback").value,
    },
    language: { mode: $("lang-mode").value, reply: $("lang-reply").value, tamil_voice: $("lang-tvoice").value, edge_voice: $("lang-edge").value },
    audio: { input_device: dev($("audio-in").value), output_device: dev($("audio-out").value), silence_ms: Number($("beh-silence").value || 900),
             barge_in: $("barge-on").checked, echo_gain: Number($("barge-echo").value || 0.6), barge_check_ms: Number($("barge-ms").value || 900) },
    memory: { auto_save: $("mem-auto").checked },
    ui: { orb: $("ui-orb").checked, orb_quality: $("ui-orb-q").value },
    agent: { confirm_mode: $("beh-confirm").value, max_steps: Number($("beh-steps").value || 40),
             speak_replies: $("beh-speak").checked, headless_browser: $("beh-headless").checked, self_check: $("pv-selfcheck").checked },
    voice: { mode: $("pv-mode").value, wake_threshold: Number($("pv-thr").value || 0.5), follow_up_s: Number($("pv-follow").value || 10),
             listen_s: Number($("pv-listen").value || 8), chime: $("pv-chime").checked, emotion: $("pv-emotion").checked,
             clap: $("pv-clap").checked, clap_sensitivity: Number($("pv-clap-s").value || 0.5), clap_ack: $("pv-clap-ack").checked },
    ambient: { enabled: $("am-on").checked, quiet_enabled: $("am-quiet").checked, quiet_start: $("am-q-start").value || "23:00",
               quiet_end: $("am-q-end").value || "07:00", coding_break: $("am-code").checked, coding_break_h: Number($("am-code-h").value || 2),
               screen_break: $("am-screen").checked, screen_break_h: Number($("am-screen-h").value || 3), welcome_back: $("am-welcome").checked,
               min_gap_min: Number($("am-gap").value || 45), late_night: $("am-late").checked },
    home_assistant: { enabled: $("ha-on").checked, url: $("ha-url").value.trim(), token: $("ha-token").value.trim(), verify_ssl: $("ha-ssl").checked },
    briefings: {
      location: $("br-loc").value.trim(),
      morning: { enabled: $("br-m-on").checked, time: $("br-m-time").value || "08:00", weekdays_only: $("br-m-wd").checked },
      evening: { enabled: $("br-e-on").checked, time: $("br-e-time").value || "18:30", weekdays_only: $("br-e-wd").checked },
    },
    awareness: { enabled: $("aw-on").checked, interval_s: Number($("aw-int").value || 60), cooldown_min: Number($("aw-cool").value || 10), model: $("aw-model").value.trim() },
    mcp: { servers: parseMcp() },
    phones: { enabled: $("phones-on").checked, port: Number($("phones-port").value || 8767),
      extra_hosts: $("phones-hosts").value.split(",").map((x) => x.trim()).filter(Boolean) },
    node: { enabled: $("node-on").checked, name: $("node-name").value.trim(), port: Number($("node-port").value || 8766), allow_elevated: $("node-elev").checked },
    nodes: pcs.filter((p) => p.name && p.url),
  };
}

// ------------------------------------------------------------------ custom voice sample + smart home test
async function loadClone() {
  const st = $("clone-status");
  try {
    const r = await (await fetch(API + "/api/voice/clone")).json();
    if (document.activeElement !== $("clone-text")) $("clone-text").value = r.text || "";
    const sample = r.has_sample ? `Sample: ${r.seconds}s${r.advice ? " — " + r.advice : ""}.` : "No sample yet.";
    st.textContent = `${sample} ${r.message || ""}`;
    st.className = "hint " + (r.has_sample && r.ready ? "ok" : r.has_sample ? "bad" : "");
  } catch (e) { st.textContent = "Couldn't check the custom voice: " + e.message; }
}
async function postJson(path, body, method = "POST") {
  const r = await fetch(API + path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
$("clone-file").onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  $("clone-status").textContent = "Saving sample…";
  const b64 = await new Promise((ok, bad) => { const fr = new FileReader(); fr.onload = () => ok(String(fr.result).split(",")[1]); fr.onerror = bad; fr.readAsDataURL(f); });
  try { await postJson("/api/voice/sample", { audio_b64: b64, text: $("clone-text").value }); } catch (err) { $("clone-status").textContent = err.message; }
  e.target.value = ""; loadClone();
};
$("clone-rec").onclick = async () => {
  const b = $("clone-rec"); b.disabled = true;
  let left = 15; const tick = setInterval(() => { b.textContent = `● Recording… ${--left}s`; }, 1000);
  $("clone-status").textContent = "Read anything aloud in your natural voice…";
  try { await postJson("/api/voice/record", { seconds: 15 }); } catch (err) { $("clone-status").textContent = err.message; }
  clearInterval(tick); b.disabled = false; b.textContent = "● Record 15 s"; loadClone();
};
$("clone-play").onclick = async () => {
  try { const r = await (await fetch(API + "/api/voice/sample")).json(); if (r.wav_b64) new Audio("data:audio/wav;base64," + r.wav_b64).play(); } catch (_) {}
};
$("clone-del").onclick = async () => { await postJson("/api/voice/sample", null, "DELETE"); loadClone(); };
$("clone-text").onchange = () => postJson("/api/voice/sample", { text: $("clone-text").value }).catch(() => {});
$("ha-test").onclick = async () => {
  const st = $("ha-status"); st.className = "hint"; st.textContent = "Checking…";
  try {
    const r = await postJson("/api/homeassistant/test", { url: $("ha-url").value.trim(), token: $("ha-token").value.trim(), verify_ssl: $("ha-ssl").checked });
    if (r.ok) {
      const c = r.counts || {}, n = (k) => c[k] || 0;
      st.textContent = `Connected${r.location ? " to " + r.location : ""}${r.version ? " (Home Assistant " + r.version + ")" : ""}: ` +
        `${n("light")} lights, ${n("climate")} AC/thermostats, ${n("media_player")} TVs/speakers, ${n("switch")} switches, ${n("fan")} fans. Save to use it.`;
      st.className = "hint ok";
    } else { st.textContent = r.message; st.className = "hint bad"; }
  } catch (e) { st.textContent = e.message; st.className = "hint bad"; }
};

// ------------------------------------------------------------------ connections: MCP apps + other PCs
const MCP_TEMPLATES = {
  "Gmail + Calendar": { name: "google", command: "uvx", args: ["workspace-mcp", "--tools", "gmail", "calendar"],
    env: { GOOGLE_OAUTH_CLIENT_ID: "<client id>", GOOGLE_OAUTH_CLIENT_SECRET: "<client secret>" } },
  "GitHub": { name: "github", url: "https://api.githubcopilot.com/mcp/", headers: { Authorization: "Bearer <github token>" } },
  "Notion": { name: "notion", command: "npx", args: ["-y", "@notionhq/notion-mcp-server"], env: { NOTION_TOKEN: "<ntn_… integration token>" } },
  "Spotify": { name: "spotify", command: "uvx", args: ["spotify-mcp"],
    env: { SPOTIFY_CLIENT_ID: "<id>", SPOTIFY_CLIENT_SECRET: "<secret>", SPOTIFY_REDIRECT_URI: "http://127.0.0.1:8888/callback" } },
  "Files": { name: "files", command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "<C:/Users/you/Documents>"] },
};
for (const [label, tpl] of Object.entries(MCP_TEMPLATES)) {
  const b = el("button", "pill", "+ " + label); b.type = "button";
  b.onclick = () => {
    const list = parseMcp(true) || [];
    if (!list.some((x) => x.name === tpl.name)) list.push({ ...tpl, enabled: true });
    $("mcp-json").value = JSON.stringify(list, null, 2);
  };
  $("mcp-templates").appendChild(b);
}
function parseMcp(quiet) {
  try {
    const v = JSON.parse($("mcp-json").value || "[]");
    $("mcp-json-err").textContent = "";
    return Array.isArray(v) ? v : [v];
  } catch (e) {
    if (!quiet) $("mcp-json-err").textContent = "Servers JSON isn't valid: " + e.message;
    return quiet ? null : (settings?.mcp?.servers || []);
  }
}
// ------------------------------------------------------------------ phones
const PHONE_CAPS = { accessibility: "screen", notifications: "notifications", sms: "SMS", calls: "calls", contacts: "contacts", files: "files", location: "location", shizuku: "power mode" };
async function loadPhones() {
  try {
    const r = await (await fetch(API + "/api/phones")).json();
    const rows = r.devices.map((d) => {
      const row = el("div", "mrow");
      const n = el("span", "name");
      const caps = Object.entries(PHONE_CAPS).filter(([k]) => d.caps && d.caps[k]).map(([, v]) => v);
      n.append(el("b", "", d.name), el("span", "desc", `${d.model || "Android"}${d.online ? ` · ${d.host}` : d.last_seen ? ` · last seen ${when(new Date(d.last_seen * 1000).toISOString())}` : ""}${caps.length ? ` · can use: ${caps.join(", ")}` : ""}`));
      const st = el("span", "state " + (d.online ? "ready" : "error"), d.online ? "online" : "offline");
      const x = el("button", "pill", "Remove");
      x.onclick = async () => { if (!confirm(`Remove ${d.name}? It will need to pair again.`)) return; await fetch(API + "/api/phones/" + encodeURIComponent(d.id), { method: "DELETE" }); loadPhones(); };
      row.append(n, st, x);
      return row;
    });
    $("phone-rows").replaceChildren(...(rows.length ? rows : [el("div", "ag-empty hint", r.enabled ? "No phone paired yet." : "Phone link is off. Pair a phone to turn it on.")]));
  } catch (_) {}
}
let qrTimer = null;
$("phone-pair").onclick = async () => {
  $("phone-pair").disabled = true;
  try {
    const r = await (await fetch(API + "/api/phones/pair", { method: "POST" })).json();
    $("phones-on").checked = true;
    $("phone-qr-img").innerHTML = r.svg || "";
    if (!r.svg) $("phone-qr-img").textContent = "QR unavailable: use Copy link and paste it in the app.";
    $("phone-qr-addr").textContent = `Addresses: ${r.addresses.map((a) => `${a}:${r.port}`).join(", ") || "none found"}`;
    $("phone-qr-copy").onclick = () => navigator.clipboard.writeText(r.uri).then(() => ($("phone-qr-copy").textContent = "Copied"));
    $("phone-qr").classList.remove("hidden");
    let left = r.expires_in;
    clearInterval(qrTimer);
    qrTimer = setInterval(() => {
      left -= 1;
      $("phone-qr-time").textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
      if (left <= 0) { clearInterval(qrTimer); $("phone-qr").classList.add("hidden"); }
    }, 1000);
  } finally {
    $("phone-pair").disabled = false;
  }
};
$("phone-qr-close").onclick = () => { clearInterval(qrTimer); $("phone-qr").classList.add("hidden"); loadPhones(); };

async function loadConnections() {
  loadPhones();
  try {
    const m = await (await fetch(API + "/api/mcp")).json();
    $("mcp-status").replaceChildren(...m.servers.map((s) => {
      const r = el("div", "mrow");
      r.append(el("span", "name", s.name), el("span", "state " + s.state, s.state === "ready" ? `connected · ${s.tools.length} tools` : s.state));
      if (s.error) r.title = s.error;
      return r;
    }));
    if (!m.servers.length) $("mcp-status").replaceChildren(el("div", "ag-empty hint", "No apps connected yet."));
    const n = await (await fetch(API + "/api/node")).json();
    const box = $("node-info"); box.replaceChildren();
    if (n.enabled) {
      box.append(`${n.running ? "Listening" : "Not running"} as “${n.name}” on `, el("b", "", n.addresses.map((a) => `${a}:${n.port}`).join(", ") || `port ${n.port}`), ". Token: ", el("span", "node-token", n.token));
    } else box.textContent = "Off. Tick the box and Save to let your other PCs control this one.";
  } catch (_) {}
}
let pcs = [];
function renderPcs(status = {}) {
  $("pc-rows").replaceChildren(...pcs.map((p, i) => {
    const r = el("div", "mrow");
    for (const [k, ph] of [["name", "Name (e.g. Laptop)"], ["url", "Address, e.g. 192.168.1.20:8766"], ["token", "Token"]]) {
      const inp = el("input"); inp.placeholder = ph; inp.value = p[k] || ""; if (k === "token") inp.type = "password";
      inp.oninput = () => (p[k] = inp.value); r.appendChild(inp);
    }
    const st = status[p.name];
    if (st) r.appendChild(el("span", "state " + (st.ok ? "ready" : "error"), st.ok ? "online" : "offline"));
    if (st && !st.ok) r.title = st.error;
    const x = el("button", "pill", "✕"); x.onclick = () => { pcs.splice(i, 1); renderPcs(); }; r.appendChild(x);
    return r;
  }));
}
$("pc-add").onclick = () => { pcs.push({ name: "", url: "", token: "" }); renderPcs(); };
$("pc-check").onclick = async () => {
  $("btn-save").click();
  await new Promise((r) => setTimeout(r, 600));
  const r = await (await fetch(API + "/api/nodes/status")).json();
  renderPcs(Object.fromEntries(r.pcs.map((x) => [x.name, x])));
};

// ------------------------------------------------------------------ schedules + skills (settings tabs)
async function renderSchedules() {
  await loadAgenda();
  const rows = agenda.schedules.map((j) => {
    const r = el("div", "mrow");
    const n = el("span", "name"); n.append(el("b", "", j.title), el("span", "desc", `${j.kind} · ${j.when} · next ${when(j.next_run)}`));
    const x = el("button", "pill", j.builtin ? "turn off below" : "Cancel"); x.disabled = !!j.builtin;
    x.onclick = async () => { await fetch(API + "/api/schedules/" + j.id, { method: "DELETE" }); renderSchedules(); };
    r.append(n, x);
    return r;
  });
  $("sched-list").replaceChildren(...(rows.length ? rows : [el("div", "hint", "Nothing scheduled.")]));
}
async function loadSkills() {
  const r = await (await fetch(API + "/api/skills")).json();
  const rows = r.skills.map((sk) => {
    const row = el("div", "mrow");
    const n = el("span", "name"); n.append(el("b", "", sk.name), el("span", "desc", `${sk.description} · v${sk.version} · used ${sk.uses}×${sk.source === "teach" ? " · taught" : ""}`));
    const view = el("button", "pill", "View");
    view.onclick = async () => {
      const d = await (await fetch(API + "/api/skills/" + sk.id)).json();
      $("skill-view").textContent = d.body; $("skill-view").classList.remove("hidden");
    };
    const del = el("button", "pill danger", "Delete");
    del.onclick = async () => {
      if (del.dataset.armed !== "1") { del.dataset.armed = "1"; del.textContent = "Sure?"; return; }
      await fetch(API + "/api/skills/" + sk.id, { method: "DELETE" }); loadSkills();
    };
    row.append(n, view, del);
    return row;
  });
  $("skill-list").replaceChildren(...(rows.length ? rows : [el("div", "hint", "No skills yet.")]));
}

$("btn-save").onclick = async () => {
  const body = collect();
  if ($("mcp-json-err").textContent) { $("save-msg").textContent = ""; return; }
  await fetch(API + "/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  await loadSettings();
  if (body.voice.mode !== voiceMode) setMode(body.voice.mode);
  loadAgenda();
  if (!$("tab-connections").classList.contains("hidden")) setTimeout(loadConnections, 1500);
  $("save-msg").textContent = "Saved ✓";
  setTimeout(() => ($("save-msg").textContent = ""), 2500);
};

$("btn-test-voice").onclick = async () => {
  $("btn-save").click();
  $("btn-test-voice").textContent = "… synthesizing (first run downloads the voice)";
  const r = await fetch(API + "/api/tts/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  $("btn-test-voice").textContent = r.ok ? "▶ Test voice" : "✕ " + (await r.json()).detail;
};

// ------------------------------------------------------------------ local models
let modelPoll = null;
async function refreshModels() {
  const m = await (await fetch(API + "/api/models")).json();
  const row = (kind, id, label, size, installed) => {
    const p = m.progress[`${kind}:${id}`];
    const d = document.createElement("div");
    d.className = "mrow";
    d.innerHTML = `<span class="name"></span><span class="size">${size} MB</span>`;
    d.querySelector(".name").textContent = label;
    if (installed || p?.status === "done") d.insertAdjacentHTML("beforeend", `<span class="badge">installed ✓</span>`);
    else if (p && (p.status === "downloading" || p.status === "queued"))
      d.insertAdjacentHTML("beforeend", `<span class="bar"><i style="width:${Math.round(p.progress * 100)}%"></i></span>`);
    else {
      if (p?.status === "error") d.insertAdjacentHTML("beforeend", `<span class="size" style="color:var(--bad)" title="${p.error.replace(/"/g, "'")}">failed</span>`);
      const b = document.createElement("button");
      b.className = "pill"; b.textContent = "Download";
      b.onclick = async () => {
        await fetch(API + "/api/models/download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, name: id }) });
        refreshModels();
      };
      d.appendChild(b);
    }
    return d;
  };
  $("list-whisper").replaceChildren(...m.whisper.map((w) => row("whisper", w.id, w.id, w.size_mb, w.installed)));
  $("list-piper").replaceChildren(...m.piper.map((v) => row("piper", v.id, v.label, v.size_mb, v.installed)));
  $("list-kokoro").replaceChildren(...m.kokoro.map((k) => row("kokoro", k.id, `Kokoro v1.0 (${k.id})`, k.size_mb, k.installed)));
  const active = Object.values(m.progress).some((p) => p.status === "downloading" || p.status === "queued");
  clearTimeout(modelPoll);
  if (active && !$("tab-models").classList.contains("hidden")) modelPoll = setTimeout(refreshModels, 1000);
}

// ------------------------------------------------------------------ memory graph
const graph = new MemoryGraph($("graph"), { onSelect: showNode });
window.jarvisGraph = graph;   // handy for debugging in devtools
let graphView = false, graphLoaded = false;
graph.visible = true;             // the memory tile always shows the live graph

async function loadGraph() {
  try {
    const g = await (await fetch(API + "/api/graph" + ($("graph-history").checked ? "?include_invalid=true" : ""))).json();
    graph.setData(g);
    $("mem-count").textContent = g.nodes.length;
    if (!graphLoaded) { graphLoaded = true; setTimeout(() => graph.fit(), 1500); }
  } catch (_) { $("mem-count").textContent = "—"; }
}

// expanded = the memory tile takes over the right side (conversation stays visible)
function setView(g) {
  graphView = g;
  $("tiles").classList.toggle("expanded", g);
  $("graph-hud").classList.toggle("hidden", !g);
  if (!g) { $("node-panel").classList.add("hidden"); graph.selected = null; }
  $("view-graph").classList.toggle("on", g);
  $("view-orb").innerHTML = `<svg><use href="#${g ? "i-collapse" : "i-arrow"}"/></svg>`;
  setTimeout(() => graph.fit(), 60);
  if (g) loadGraph();
}
$("view-orb").onclick = () => setView(!graphView);
$("view-graph").onclick = () => setView(!graphView);
$("graph-fit").onclick = () => graph.fit();
$("graph-history").onchange = loadGraph;
$("graph-search").onkeydown = async (e) => {
  if (e.key !== "Enter") return;
  const q = e.target.value.trim();
  if (!q) return;
  const r = await (await fetch(API + "/api/graph/search?q=" + encodeURIComponent(q))).json();
  onMemorySearch({ query: q, nodes: r.nodes, edges: r.edges, manual: true });
};

function onMemorySearch(ev) {
  if (!ev.nodes.length) return;
  if (!ev.manual) {
    const d = document.createElement("div");
    d.className = "tool ok mem";
    d.innerHTML = `<div class="head"><b>memory</b> <span></span></div>`;
    d.querySelector("span").textContent = `recalled ${ev.nodes.length} related memories`;
    recallCount += ev.nodes.length; $("recall-label").textContent = recallCount;
    d.onclick = () => { setView(true); graph.highlight(ev.nodes, ev.edges); };
    $("transcript").appendChild(d);
    d.scrollIntoView({ block: "end" });
  }
  $("graph-query").textContent = "searching · " + ev.query.slice(0, 140);
  graph.highlight(ev.nodes, ev.edges, "search");
}

async function onMemoryUpdate(ev) {
  $("graph-query").textContent = "";
  await loadGraph();
  if (ev.error) { $("mem-count").textContent = "· save failed"; return; }
  if (ev.nodes.length || ev.edges.length) {
    graph.highlight(ev.nodes, ev.edges, "new", 6000);
    const n = ev.edges.length, inv = (ev.invalidated || []).length;
    const d = document.createElement("div");
    d.className = "msg info";
    d.textContent = `🧠 remembered ${n} new fact${n === 1 ? "" : "s"}${inv ? `, updated ${inv} old one${inv === 1 ? "" : "s"}` : ""}`;
    d.style.cursor = "pointer";
    d.onclick = () => { setView(true); graph.highlight(ev.nodes, ev.edges, "new"); };
    $("transcript").appendChild(d);
    d.scrollIntoView({ block: "end" });
  }
}

function onBarge(ev) {
  if (ev.accepted) return;   // the interruption simply shows up as your next message
  const d = document.createElement("div");
  d.className = "msg info";
  d.textContent = `(heard “${ev.text || "…"}” — ${ev.reason}, kept talking)`;
  $("transcript").appendChild(d);
}

async function showNode(n) {
  const panel = $("node-panel");
  const r = await (await fetch(API + "/api/graph/node/" + encodeURIComponent(n.id))).json();
  const names = Object.fromEntries([...graph.nodes.values()].map((x) => [x.id, x.name]));
  panel.innerHTML = `<div class="np-head"><b></b><button class="icon-btn" title="Close">✕</button></div>
    <div class="np-type"></div><p class="np-sum"></p><div class="np-facts"></div>
    <button class="pill danger np-forget">Forget this</button>`;
  panel.querySelector("b").textContent = r.entity.name;
  panel.querySelector(".np-type").textContent = `${r.entity.type} · mentioned ${r.entity.mentions}× · updated ${r.entity.updated.slice(0, 10)}`;
  panel.querySelector(".np-sum").textContent = r.entity.summary || "";
  const facts = panel.querySelector(".np-facts");
  r.facts.sort((a, b) => (b.valid - a.valid) || b.created.localeCompare(a.created));
  for (const f of r.facts) {
    const d = document.createElement("div");
    d.className = "np-fact" + (f.valid ? "" : " old");
    d.textContent = f.fact;
    d.title = `${names[f.source] || f.source} —${f.relation}→ ${names[f.target] || f.target}\nnoted ${f.created}` + (f.valid ? "" : `\noutdated since ${f.invalidated}`);
    facts.appendChild(d);
  }
  panel.querySelector(".icon-btn").onclick = () => { panel.classList.add("hidden"); graph.selected = null; };
  const fb = panel.querySelector(".np-forget");
  if (n.id === "user") fb.remove();
  else fb.onclick = async () => {
    if (fb.dataset.armed !== "1") { fb.dataset.armed = "1"; fb.textContent = "Click again to forget"; return; }
    await fetch(API + "/api/graph/node/" + encodeURIComponent(n.id), { method: "DELETE" });
    panel.classList.add("hidden"); graph.selected = null; loadGraph();
  };
  panel.classList.remove("hidden");
}

$("mem-clear").onclick = async () => {
  const b = $("mem-clear");
  if (b.dataset.armed !== "1") { b.dataset.armed = "1"; b.textContent = "Click again to erase all memories"; return; }
  await fetch(API + "/api/graph/clear", { method: "POST" });
  b.dataset.armed = ""; b.textContent = "Forget everything";
  $("mem-stats").textContent = "All memories erased.";
  loadGraph();
};

// ------------------------------------------------------------------ boot
(async () => {
  await waitForBackend();
  await loadSettings();
  connect();
  loadGraph();
  loadAgenda();
  setInterval(renderAgenda, 60000);
  const name = settings.assistant_name || "Jarvis";
  addMsg("info", `Say “Hey ${name}”, hold Space (or the mic) to talk, or type. Open ⚙ to pick your model, voices and apps.`);
})();
