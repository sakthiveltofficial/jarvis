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
    case "wake": $("t-voice-tile").classList.add("awake"); $("voice-mode").textContent = ev.source === "clap" ? "👏 I'm listening…" : ev.source === "gesture" ? "✋ I'm listening…" : "I'm listening…"; break;
    case "gesture": onGesture(ev); break;
    case "wake_sleep": $("t-voice-tile").classList.remove("awake"); renderHandsfree(); break;
    case "mood": renderMood(ev); break;
    case "plan": if (ev.agent === "main") { agenda.plan = ev.plan; renderAgenda(); } break;
    case "bg": upsertBg(ev); break;
    case "bg_update": bgNote(ev); break;
    case "agenda": loadAgenda(); break;
    case "teach": teaching = ev.on; renderTeach(); break;
    case "skills": if (!$("tab-skills").classList.contains("hidden")) loadSkills(); break;
    case "user": addMsg("user", ev.text); break;
    case "assistant": addMsg("assistant", ev.text, ev.proactive, ev.character); break;
    case "settings_changed": loadSettings(); break;            // changed by voice/chat (the `self` tool)
    case "open_settings": openSettings().then(() => document.querySelector(`.tabs button[data-tab="${ev.tab}"]`)?.click()); break;
    case "spectrum": drawSpectrum(ev.bands); break;
    case "info": addMsg("info", ev.text); break;
    case "error": addMsg("error", ev.text); break;
    case "tool_start": if (!ev.agent) addTool(ev); break;
    case "tool_end": if (!ev.agent) endTool(ev); break;
    case "confirm": showConfirm(ev); break;
    case "barge": onBarge(ev); break;
    case "memory_search": onMemorySearch(ev); break;
    case "memory_saving": $("graph-query").textContent = "saving what I learned…"; break;
    case "memory_update": onMemoryUpdate(ev); break;
    case "memory_consolidating": $("graph-query").textContent = "tidying up my memory…"; if (consVisible()) loadConsolidation(); break;
    case "memory_consolidated": onConsolidated(ev); break;
    case "files_status": if (filesVisible()) renderFiles(ev); break;
    case "files_entities": if (ev.nodes && ev.nodes.length) loadGraph(); break;
    case "file_search": onFileSearch(ev); break;
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

function addMsg(kind, text, proactive, who) {
  const d = document.createElement("div");
  // a line from the other character (Jarvis and Ultron talking) gets their name and colour
  const other = kind === "assistant" && who && who !== charKey();
  d.className = "msg " + kind + (proactive ? " proactive" : "") + (other ? " other" : "");
  if (other) d.style.setProperty("--other", who === "ultron" ? "#e0413a" : "#3d8bff");
  if (proactive) d.title = { reminder: "Reminder", offer: "Said on my own", clap: "You clapped", background: "Background task", teach: "Teach mode", skill: "New skill", presence: "At the desk", health: "Health nudge", report: "Weekly life report" }[proactive] || proactive;
  d.textContent = text;
  if (other) { const w = document.createElement("span"); w.className = "who"; w.textContent = CHAR_NAMES[who] || who; d.prepend(w); }
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
  const name = aname();
  const clap = settings?.voice?.clap !== false ? " or clap twice" : "";
  // the wake word model is always "Hey Jarvis"; without it, the name is spotted in what you say
  const label = { off: "push to talk", wake: (wakeReady || settings?.voice?.wake_phrase ? `say “${wakePhrase()}”` : `say “${name}…”`) + clap, handsfree: "always listening" }[voiceMode];
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
// ------------------------------------------------------------------ character (Jarvis / Ultron, see character.py)
const CHAR_NAMES = { jarvis: "Jarvis", ultron: "Ultron" };
const wakePhrase = () => (settings?.voice?.wake_phrase || "").trim() || "Hey Jarvis";
const charKey = () => (CHAR_NAMES[settings?.character] ? settings.character : "jarvis");
// a custom assistant name wins; the stock names follow the character
function aname() {
  const n = (settings?.assistant_name || "").trim();
  return n && !Object.values(CHAR_NAMES).some((x) => x.toLowerCase() === n.toLowerCase()) ? n : CHAR_NAMES[charKey()];
}

const cv = $("orb"), cx = cv.getContext("2d");
const blob = document.createElement("canvas"); blob.width = blob.height = 64;
const bx = blob.getContext("2d");
const GLOW = {                  // simple glow (orb off / no WebGL): two blob colours per state + the focal dot
  jarvis: {
    dot: [79, 157, 255],
    idle: [[61, 139, 255], [127, 212, 255]], listening: [[56, 217, 255], [138, 240, 255]],
    transcribing: [[56, 217, 255], [138, 240, 255]], thinking: [[107, 107, 255], [169, 139, 255]],
    acting: [[47, 107, 255], [90, 209, 255]], speaking: [[79, 157, 255], [0, 229, 255]],
  },
  ultron: {
    dot: [255, 42, 42],
    idle: [[255, 42, 42], [255, 122, 77]], listening: [[255, 59, 59], [255, 154, 107]],
    transcribing: [[255, 59, 59], [255, 154, 107]], thinking: [[176, 0, 42], [255, 61, 110]],
    acting: [[209, 10, 26], [255, 140, 26]], speaking: [[255, 26, 26], [255, 77, 46]],
  },
};
const glow = () => GLOW[charKey()];
let t0 = performance.now(), pal = GLOW.jarvis.idle.map((c) => c.slice());
function ink(a) { return document.documentElement.dataset.theme === "dark" ? `rgba(236,239,236,${a})` : `rgba(23,25,26,${a})`; }
function draw(now) {
  const t = (now - t0) / 1000;
  level += (targetLevel - level) * 0.18;
  targetLevel *= 0.93;
  if (orb3d) { drawWave(t); requestAnimationFrame(draw); return; }   // the particle orb draws itself
  const target = glow()[state] || glow().idle;
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
  // 3) focal dot in the character's colour with a level halo
  const halo = 10 + level * 40 + (busy ? 6 * (0.5 + 0.5 * Math.sin(t * 6)) : 0);
  const g = cx.createRadialGradient(C, C, 0, C, C, halo * 2), dot = glow().dot.join(",");
  g.addColorStop(0, `rgba(${dot},0.55)`); g.addColorStop(1, `rgba(${dot},0)`);
  cx.fillStyle = g; cx.beginPath(); cx.arc(C, C, halo * 2, 0, Math.PI * 2); cx.fill();
  cx.fillStyle = `rgb(${dot})`; cx.beginPath(); cx.arc(C, C, 6, 0, Math.PI * 2); cx.fill();
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
    try { orb3d = await window.JarvisOrb.start($("stage"), { quality: ui.orb_quality, theme: curTheme(), character: charKey() }); }
    catch (e) { console.warn("[orb]", e); orb3d = null; }
    orbStarting = false;
    if (!orb3d) { orbFailed = true; return; }          // no WebGL: keep the glow
    orb3d.setState(state);
    if (settings?.ui?.orb === false) { orb3d.destroy(); orb3d = null; }   // turned off while it was loading
  } else if (!want && orb3d) {
    orb3d.destroy(); orb3d = null;
  }
  if (orb3d) { orb3d.setQuality(ui.orb_quality || "auto"); if (state !== "speaking") orb3d.setCharacter(charKey()); }
  $("orb").classList.toggle("hidden", !!orb3d);
}
function orbEvent(ev) {
  if (!orb3d || ev.agent) return;
  switch (ev.type) {
    case "state": orb3d.setState(ev.state); if (ev.state !== "speaking") orb3d.setCharacter(charKey()); break;
    case "speaker": orb3d.setCharacter(ev.character); break;
    case "mirror": if (settings?.gestures?.mirror !== false) orb3d.mirror(ev); break;
    case "level": orb3d.level(ev.source, ev.value); break;
    case "mood": orb3d.userMood(ev.mood); break;
    case "ai_mood": orb3d.aiMood(ev.mood); break;
    case "tool_start": orb3d.task(ev.tool); break;
    case "tool_end": if (!ev.ok) orb3d.flash(); break;
    case "error": orb3d.flash(); break;
    case "memory_search": orb3d.task("memory_search", 2.0); break;
    case "hand": if (settings?.gestures?.orb !== false) orb3d.hand(ev.hand); break;
    case "gesture": if (settings?.gestures?.orb !== false || ev.gesture === "palm" || ev.gesture === "fist") orb3d.gesture(ev.gesture); break;
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
  $("brand-name").textContent = aname();
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
  if (b.dataset.tab === "memory") loadConsolidation();
  if (b.dataset.tab === "files") loadFiles();
  if (b.dataset.tab === "desktop") loadGestureStatus();
  if (b.dataset.tab === "presence") loadPresence();
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
  applyHud();
}

// ------------------------------------------------------------------ v0.8: screen HUD + hand gestures (desktop app only)
const tauri = window.__TAURI__ && window.__TAURI__.core;
let hudApplied = null;
async function applyHud(force) {
  if (!tauri || !settings) return;
  const h = settings.hud || {};
  const key = JSON.stringify(h);
  if (key === hudApplied && !force) return;
  hudApplied = key;
  try { await tauri.invoke("hud_sync", { enabled: h.enabled !== false }); }
  catch (e) { console.warn("[hud]", e); hudApplied = null; }
}
function onGesture(ev) {
  if (ev.gesture === "pinch_tap" && settings?.gestures?.orb !== false && tauri) {
    tauri.invoke("toggle_main").catch((e) => console.warn("[gesture]", e));
  }
  if (!$("tab-desktop").classList.contains("hidden")) loadGestureStatus();
}
let gsTimer = 0;
async function loadGestureStatus() {
  clearTimeout(gsTimer);
  if ($("settings").classList.contains("hidden") || $("tab-desktop").classList.contains("hidden")) return;
  try {
    const g = await (await fetch(API + "/api/gestures")).json();
    const names = { off: "Off.", loading: "Starting the hand tracker…", downloading: "Downloading the hand-tracking model (7.5 MB)…",
      running: `Watching for gestures (${g.fps} fps)${g.hand_visible ? " · I can see your hand ✋" : ""}.`, error: "Problem: " + g.error };
    let t = names[g.state] || g.state;
    if (g.last_gesture && g.last_gesture_ago != null && g.last_gesture_ago < 120) t += ` Last gesture: ${g.last_gesture.replace("_", " ")} (${Math.round(g.last_gesture_ago)} s ago).`;
    $("gs-status").textContent = t;
  } catch (_) { $("gs-status").textContent = ""; }
  gsTimer = setTimeout(loadGestureStatus, 1500);
}
$("hud-test").onclick = async () => {
  if (!tauri) { $("hud-status").textContent = "The HUD needs the Jarvis desktop app."; return; }
  if (!$("hud-on").checked) { $("hud-status").textContent = "Turn the HUD on and save first."; return; }
  await applyHud(true);
  setTimeout(() => fetch(API + "/api/hud/test", { method: "POST" }), 600);
  $("hud-status").textContent = "Look at the middle of your main screen.";
  setTimeout(() => ($("hud-status").textContent = ""), 5000);
};

// ------------------------------------------------------------------ v0.10: presence, health nudges, life report
let prTimer = 0;
async function loadPresence() {
  clearTimeout(prTimer);
  if ($("settings").classList.contains("hidden") || $("tab-presence").classList.contains("hidden")) return;
  try {
    const r = await (await fetch(API + "/api/presence")).json();
    const p = r.presence, h = r.health;
    const names = { off: "Camera off.", loading: "Starting the face tracker…", downloading: "Downloading the face models (37 MB)…",
      running: "Watching the desk.", error: "Problem: " + p.error };
    let t = names[p.state] || p.state;
    if (p.state === "running") {
      const seen = p.seen || [];
      t += seen.includes("owner") ? " I can see you ✓" : seen.includes("stranger") ? " I see someone I don't know." :
        seen.includes("face") ? " I can see a face." : " Nobody in view.";
      if (p.posture) t += p.posture.bad ? ` Posture: ${p.posture.reason}.` : " Posture: good.";
    }
    t += p.enrolled ? ` Face learned${p.enrolled_at ? " " + p.enrolled_at.slice(0, 10) : ""}.` : " I haven't learned your face yet, so anyone counts as you.";
    if (!p.has_baseline) t += " No posture baseline yet.";
    $("pr-status").textContent = t;
    const e = p.enroll || {};
    if (e.message && !e.done) $("pr-enroll-msg").textContent = `${e.message} ${Math.round((e.progress || 0) * 100)}%`;
    else if (e.message && e.done) { (e.posture_only ? $("he-cal-msg") : $("pr-enroll-msg")).textContent = e.message; }
    $("he-status").textContent = `Screen time since your last break: ${h.screen_min} min · water today: ${h.water_today} glass${h.water_today === 1 ? "" : "es"}. Tell me “I drank some water” and I'll count it.`;
    const last = (r.life.reports || [])[0];
    if (last && !$("lf-msg").textContent) $("lf-msg").textContent = `Last report: ${last.week}`;
  } catch (_) { $("pr-status").textContent = ""; }
  prTimer = setTimeout(loadPresence, 1500);
}
async function presenceEnroll(postureOnly) {
  const msg = postureOnly ? $("he-cal-msg") : $("pr-enroll-msg");
  msg.textContent = postureOnly ? "Sit up straight and look at the screen…" : "Look at the camera and turn your head a little…";
  try {
    await fetch(API + "/api/presence/enroll", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds: postureOnly ? 5 : 8, posture_only: postureOnly }) });
  } catch (e) { msg.textContent = "Couldn't start: " + e; }
  loadPresence();
}
$("pr-enroll").onclick = () => presenceEnroll(false);
$("he-calibrate").onclick = () => presenceEnroll(true);
$("pr-forget").onclick = async () => {
  await fetch(API + "/api/presence/face", { method: "DELETE" });
  $("pr-enroll-msg").textContent = "Forgotten. Your face data is deleted from this PC.";
  loadPresence();
};
$("lf-now").onclick = async () => {
  $("lf-msg").textContent = "Putting your week together…";
  try {
    const r = await (await fetch(API + "/api/life/report", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ speak: true }) })).json();
    $("lf-last").textContent = r.text || "";
    $("lf-msg").textContent = `Saved to ${r.path}`;
  } catch (e) { $("lf-msg").textContent = "Couldn't make the report: " + e; }
};
$("lf-forget").onclick = async () => {
  if ($("lf-forget").dataset.armed !== "1") {
    $("lf-forget").dataset.armed = "1"; $("lf-forget").textContent = "Click again to delete";
    setTimeout(() => { $("lf-forget").dataset.armed = ""; $("lf-forget").textContent = "Delete my life log"; }, 4000);
    return;
  }
  await fetch(API + "/api/life", { method: "DELETE" });
  $("lf-forget").dataset.armed = ""; $("lf-forget").textContent = "Delete my life log";
  $("lf-msg").textContent = "Life log deleted.";
};

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
  loadClone(); loadTrain();

  const d = await (await fetch(API + "/api/audio/devices")).json();
  opt($("audio-in"), [{ value: "", label: "System default" }, ...d.inputs.map((x) => ({ value: x.id, label: x.name }))], s.audio.input_device ?? "");
  opt($("audio-out"), [{ value: "", label: "System default" }, ...d.outputs.map((x) => ({ value: x.id, label: x.name }))], s.audio.output_device ?? "");

  $("beh-name").value = s.assistant_name; $("beh-user").value = s.user_name || "";
  $("beh-character").value = CHAR_NAMES[s.character] ? s.character : "jarvis";
  $("beh-charvoice").checked = s.character_voice !== false;
  $("beh-confirm").value = s.agent.confirm_mode; $("beh-steps").value = s.agent.max_steps;
  $("beh-silence").value = s.audio.silence_ms; $("beh-speak").checked = s.agent.speak_replies;
  $("beh-headless").checked = s.agent.headless_browser;
  $("ui-orb").checked = s.ui?.orb !== false; $("ui-orb-q").value = s.ui?.orb_quality || "auto";
  const hd = s.hud || {}, gs = s.gestures || {};
  $("hud-on").checked = hd.enabled !== false; $("hud-show").value = hd.show || "active";
  $("hud-stats").checked = hd.stats !== false; $("hud-hand").checked = hd.hand !== false;
  $("gs-on").checked = !!gs.enabled; $("gs-cam").value = gs.camera ?? 0; $("gs-sens").value = gs.sensitivity ?? 0.5;
  for (const k of ["palm", "fist", "windows", "orb", "mirror"]) $("gs-" + k).checked = gs[k] !== false;
  loadGestureStatus();
  const pr = s.presence || {}, he = s.health || {}, lf = s.life || {};
  $("pr-on").checked = !!pr.enabled; $("pr-greet").checked = pr.greet !== false; $("pr-greet-min").value = pr.greet_after_min ?? 20;
  $("pr-lock").checked = pr.lock_on_leave !== false; $("pr-lock-s").value = pr.lock_after_s ?? 45;
  $("pr-stranger").checked = pr.stranger_alert !== false; $("pr-stranger-lock").checked = pr.stranger_lock !== false;
  $("pr-photo").checked = !!pr.visitor_photo; $("pr-thr").value = pr.match_threshold ?? 0.38;
  $("he-on").checked = he.enabled !== false; $("he-posture").checked = he.posture !== false; $("he-posture-min").value = he.posture_min ?? 3;
  $("he-posture-s").value = he.posture_strictness ?? 0.5; $("he-eyes").checked = he.eyes !== false; $("he-eyes-min").value = he.eyes_min ?? 20;
  $("he-water").checked = he.water !== false; $("he-water-min").value = he.water_min ?? 60; $("he-sleep").checked = he.sleep !== false;
  $("he-bedtime").value = he.bedtime || "23:30"; $("he-sleep-h").value = he.sleep_goal_h ?? 7.5; $("he-gap").value = he.min_gap_min ?? 15;
  $("lf-track").checked = lf.track !== false; $("lf-report").checked = lf.report !== false; $("lf-day").value = lf.report_day || "sun";
  $("lf-time").value = lf.report_time || "19:00"; $("lf-speak").checked = lf.speak !== false; $("lf-keep").value = lf.keep_days ?? 120;
  $("lf-model").value = lf.model || "";
  loadPresence();
  $("mem-auto").checked = s.memory?.auto_save !== false;
  $("mem-cons").checked = s.memory?.consolidate !== false; $("mem-cons-time").value = s.memory?.consolidate_time || "03:00";
  $("mem-half").value = s.memory?.half_life_days ?? 120; $("mem-keep").value = s.memory?.keep_history_days ?? 730;
  $("mem-model").value = s.memory?.model || "";
  const fs = s.files || {};
  $("files-recall").checked = fs.auto_recall !== false; $("files-entities").checked = fs.extract_entities !== false;
  $("files-max").value = fs.max_file_mb ?? 25;
  if (!$("tab-memory").classList.contains("hidden")) loadConsolidation();
  if (filesVisible()) loadFiles();
  $("barge-on").checked = s.audio.barge_in !== false;
  $("barge-echo").value = s.audio.echo_gain ?? 0.6;
  $("barge-ms").value = s.audio.barge_check_ms ?? 900;
  const v = s.voice || {}, br = s.briefings || {}, aw = s.awareness || {}, nd = s.node || {};
  $("pv-mode").value = v.mode || "wake"; $("pv-thr").value = v.wake_threshold ?? 0.5;
  $("pv-phrase").value = v.wake_phrase || "";
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

$("beh-character").onchange = () => {        // a stock name follows the character; a custom one stays
  const n = $("beh-name").value.trim().toLowerCase();
  if (!n || Object.values(CHAR_NAMES).some((x) => x.toLowerCase() === n)) $("beh-name").value = CHAR_NAMES[$("beh-character").value];
};
function collect() {
  const prov = $("llm-provider").value;
  const keys = {};
  for (const k of ["elevenlabs", "fish_audio", "groq", "openai"]) keys[k] = $("key-" + k).value;
  if ($("llm-key").value) keys[prov] = $("llm-key").value;
  const dev = (v) => (v === "" ? null : Number(v));
  return {
    assistant_name: $("beh-name").value || CHAR_NAMES[$("beh-character").value] || "Jarvis",
    character: $("beh-character").value,
    character_voice: $("beh-charvoice").checked,
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
    memory: { auto_save: $("mem-auto").checked, consolidate: $("mem-cons").checked, consolidate_time: $("mem-cons-time").value || "03:00",
              half_life_days: Number($("mem-half").value || 0), keep_history_days: Number($("mem-keep").value || 0), model: $("mem-model").value.trim() },
    files: { auto_recall: $("files-recall").checked, extract_entities: $("files-entities").checked, max_file_mb: Number($("files-max").value || 25) },
    ui: { orb: $("ui-orb").checked, orb_quality: $("ui-orb-q").value },
    hud: { enabled: $("hud-on").checked, show: $("hud-show").value, stats: $("hud-stats").checked, hand: $("hud-hand").checked },
    gestures: { enabled: $("gs-on").checked, camera: Number($("gs-cam").value || 0), sensitivity: Number($("gs-sens").value || 0.5),
                palm: $("gs-palm").checked, fist: $("gs-fist").checked, windows: $("gs-windows").checked, orb: $("gs-orb").checked, mirror: $("gs-mirror").checked },
    presence: { enabled: $("pr-on").checked, greet: $("pr-greet").checked, greet_after_min: Number($("pr-greet-min").value || 20),
                lock_on_leave: $("pr-lock").checked, lock_after_s: Number($("pr-lock-s").value || 45), stranger_alert: $("pr-stranger").checked,
                stranger_lock: $("pr-stranger-lock").checked, visitor_photo: $("pr-photo").checked, match_threshold: Number($("pr-thr").value || 0.38) },
    health: { enabled: $("he-on").checked, posture: $("he-posture").checked, posture_min: Number($("he-posture-min").value || 3),
              posture_strictness: Number($("he-posture-s").value || 0.5), eyes: $("he-eyes").checked, eyes_min: Number($("he-eyes-min").value || 20),
              water: $("he-water").checked, water_min: Number($("he-water-min").value || 60), sleep: $("he-sleep").checked,
              bedtime: $("he-bedtime").value || "23:30", sleep_goal_h: Number($("he-sleep-h").value || 7.5), min_gap_min: Number($("he-gap").value || 15) },
    life: { track: $("lf-track").checked, report: $("lf-report").checked, report_day: $("lf-day").value, report_time: $("lf-time").value || "19:00",
            speak: $("lf-speak").checked, keep_days: Number($("lf-keep").value || 120), model: $("lf-model").value.trim() },
    agent: { confirm_mode: $("beh-confirm").value, max_steps: Number($("beh-steps").value || 40),
             speak_replies: $("beh-speak").checked, headless_browser: $("beh-headless").checked, self_check: $("pv-selfcheck").checked },
    voice: { mode: $("pv-mode").value, wake_threshold: Number($("pv-thr").value || 0.5), wake_phrase: $("pv-phrase").value.trim(), follow_up_s: Number($("pv-follow").value || 10),
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
// ------------------------------------------------------------------ v0.11: train the wake word and the clap
let train = null;   // { kind, steps: [{step, say, secs}], i }
async function loadTrain() {
  try {
    const r = await (await fetch(API + "/api/voice/train")).json();
    const parts = [];
    if (r.wake) parts.push(`Wake phrase “${r.phrase}”: trained on ${r.wake.takes} takes (${r.wake.trained_at})${r.wake.quality === "weak" ? ", on the safe side" : ""}.`);
    else if (r.custom) parts.push(`Your wake phrase “${r.phrase}” isn't trained yet: until it is, I listen for it in what I transcribe (slower). Train it below.`);
    if (r.clap) parts.push(`Claps: trained on ${r.clap.takes} takes (${r.clap.trained_at}).`);
    $("train-status").textContent = parts.length ? parts.join(" ") : "Record your own wake word and claps so I catch them in your room, with your mic.";
    $("train-status").className = "hint" + (parts.length ? " ok" : "");
    $("train-forget-wake").classList.toggle("hidden", !r.wake);
    $("train-forget-clap").classList.toggle("hidden", !r.clap);
  } catch (_) {}
}
function trainSteps(kind) {
  if (kind === "clap") return Array.from({ length: 4 }, () => ({ step: "pos", say: "👏👏 Clap twice, then stay quiet", secs: 3 }));
  const phrase = wakePhrase(), secs = phrase.split(/\s+/).length > 3 ? 3.5 : 2.5;
  const s = Array.from({ length: 5 }, () => ({ step: "pos", say: `Say “${phrase}” once, like you normally would`, secs }));
  s.push({ step: "neg", say: "Now talk normally for a few seconds (anything except the wake phrase)", secs: 6 });
  return s;
}
function renderTrain(msg, cls = "") {
  const run = $("train-run");
  run.classList.toggle("hidden", !train);
  if (!train) return;
  const st = train.steps[train.i];
  $("train-say").textContent = st ? st.say : "All takes recorded.";
  $("train-say").classList.remove("live");
  $("train-dots").innerHTML = train.steps.map((_, k) => `<span class="${k < train.i ? "done" : k === train.i ? "now" : ""}"></span>`).join("");
  if (msg !== undefined) { $("train-msg").textContent = msg; $("train-msg").className = "hint " + cls; }
  $("train-go").textContent = st ? `● Record take ${train.i + 1} of ${train.steps.length}` : "Save";
}
async function startTrain(kind) {
  if (kind === "wake") {          // train the phrase typed in the box, even if Settings wasn't saved yet
    const typed = $("pv-phrase").value.trim();
    if (typed !== (settings?.voice?.wake_phrase || "").trim()) {
      await postJson("/api/settings", { voice: { wake_phrase: typed } }).catch(() => {});
      await loadSettings();
    }
  }
  clearSpectrum();
  train = { kind, steps: trainSteps(kind), i: 0 };
  postJson("/api/voice/train/cancel").catch(() => {});
  renderTrain(kind === "clap" ? "Press Record, wait for the countdown, then clap twice." : "Press Record, then speak when it turns red.");
}
// live spectrum while a take records: a scrolling spectrogram (time →, pitch ↑) and the current bars
const SPEC_COLS = 200;
let specCols = [];
function specColour(v) {            // dark → character colour → white, by loudness
  const base = charKey() === "ultron" ? [255, 42, 42] : [61, 139, 255];
  const t = Math.max(0, Math.min(1, v));
  const k = t < 0.7 ? t / 0.7 : 1, w = t < 0.7 ? 0 : (t - 0.7) / 0.3;
  return base.map((c) => Math.round(11 + (c - 11) * k + (255 - c) * w));
}
function clearSpectrum() {
  specCols = [];
  for (const id of ["train-spectro", "train-bars"]) { const c = $(id); c.getContext("2d").clearRect(0, 0, c.width, c.height); }
}
function drawSpectrum(bands) {
  if (!bands || $("train-run").classList.contains("hidden")) return;
  specCols.push(bands); if (specCols.length > SPEC_COLS) specCols.shift();
  const sp = $("train-spectro"), g = sp.getContext("2d"), W = sp.width, H = sp.height, n = bands.length;
  const cw = W / SPEC_COLS, bh = H / n;
  g.clearRect(0, 0, W, H);
  specCols.forEach((col, x) => col.forEach((v, i) => {
    const [r, gg, b] = specColour(v);
    g.fillStyle = `rgb(${r},${gg},${b})`; g.fillRect(x * cw, H - (i + 1) * bh, Math.ceil(cw), Math.ceil(bh));
  }));
  const bc = $("train-bars"), h = bc.getContext("2d"), BW = bc.width, BH = bc.height, w = BW / n;
  h.clearRect(0, 0, BW, BH);
  bands.forEach((v, i) => {
    const [r, gg, b] = specColour(Math.min(1, v + 0.25));
    h.fillStyle = `rgb(${r},${gg},${b})`; const bhh = Math.max(1, v * BH); h.fillRect(i * w + 0.5, BH - bhh, Math.max(1, w - 1), bhh);
  });
}
$("train-wake").onclick = () => startTrain("wake");
$("train-clap").onclick = () => startTrain("clap");
$("train-cancel").onclick = () => { train = null; postJson("/api/voice/train/cancel").catch(() => {}); renderTrain(); };
$("train-go").onclick = async () => {
  if (!train) return;
  const b = $("train-go"), st = train.steps[train.i];
  b.disabled = true;
  try {
    if (!st) {
      $("train-msg").textContent = "Working it out…";
      const r = await postJson("/api/voice/train/finish", { kind: train.kind });
      train = null; renderTrain();
      $("train-status").textContent = r.message; $("train-status").className = "hint ok";
      setTimeout(loadTrain, 4000);
      return;
    }
    for (let n = 3; n > 0; n--) { $("train-say").textContent = `${st.say}… ${n}`; await new Promise((ok) => setTimeout(ok, 600)); }
    $("train-say").textContent = "● " + st.say; $("train-say").classList.add("live");
    const r = await postJson("/api/voice/train/record", { kind: train.kind, step: st.step, seconds: st.secs });
    if (r.ok) train.i++;
    let extra = "";
    if (train.kind === "clap" && r.ok && !r.default_fired) extra = " (Before training I would have missed that one.)";
    renderTrain((r.ok ? "✓ " : "✗ ") + r.message + extra, r.ok ? "ok" : "bad");
  } catch (err) {
    renderTrain(err.message, "bad");
  } finally { b.disabled = false; }
};
$("train-forget-wake").onclick = async () => { await postJson("/api/voice/train/wake", null, "DELETE").catch(() => {}); loadTrain(); };
$("train-forget-clap").onclick = async () => { await postJson("/api/voice/train/clap", null, "DELETE").catch(() => {}); loadTrain(); };
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
  if (!$("tab-desktop").classList.contains("hidden")) setTimeout(loadGestureStatus, 800);
  if (!$("tab-presence").classList.contains("hidden")) setTimeout(loadPresence, 800);
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
    $("mem-count").textContent = g.nodes.filter((n) => n.type !== "Document").length;
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
  if (r.entity.type === "Document") {
    panel.querySelector(".np-type").textContent = `File · ${r.entity.chunks || 0} passages · indexed ${String(r.entity.updated).slice(0, 10)}`;
    const p = el("div", "np-fact", r.entity.path); p.title = r.entity.path;
    panel.querySelector(".np-facts").appendChild(p);
    const fb = panel.querySelector(".np-forget");
    fb.textContent = "Open file"; fb.classList.remove("danger");
    fb.onclick = () => openFile(r.entity.path);
    panel.querySelector(".icon-btn").onclick = () => { panel.classList.add("hidden"); graph.selected = null; };
    panel.classList.remove("hidden");
    return;
  }
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

// ------------------------------------------------------------------ v0.8: nightly memory clean-up
const consVisible = () => !$("settings").classList.contains("hidden") && !$("tab-memory").classList.contains("hidden");
async function loadConsolidation() {
  try {
    const r = await (await fetch(API + "/api/memory/consolidation?days=7")).json();
    const last = r.last_report;
    let t = r.running ? "Cleaning up now…" : last ? `Last clean-up ${when(r.last_run)}: ${consSummary(last)}` : "Not run yet.";
    if (!r.running && r.next_run && r.enabled) t += ` Next: ${when(r.next_run)}.`;
    $("mem-cons-status").textContent = t;
    $("mem-cons-run").disabled = !!r.running;
    const rows = (r.digests || []).map((d) => { const x = el("div", "mrow digest"); const n = el("span", "name"); n.append(el("b", "", d.date), document.createTextNode(d.text)); x.append(n); return x; });
    $("mem-digests").replaceChildren(...(rows.length ? [el("div", "hint", "Recent days, as I remember them:"), ...rows] : []));
  } catch (_) { $("mem-cons-status").textContent = "Memory clean-up unavailable."; }
}
function consSummary(r) {
  const bits = [];
  if (r.days_summarised?.length) bits.push(`summarised ${r.days_summarised.length} day${r.days_summarised.length === 1 ? "" : "s"}`);
  if (r.entities_merged) bits.push(`merged ${r.entities_merged} duplicate${r.entities_merged === 1 ? "" : "s"}`);
  if (r.facts_merged) bits.push(`folded ${r.facts_merged} repeated fact${r.facts_merged === 1 ? "" : "s"}`);
  if (r.facts_faded) bits.push(`faded ${r.facts_faded} stale fact${r.facts_faded === 1 ? "" : "s"}`);
  const pruned = (r.history_pruned || 0) + (r.orphans_removed || 0);
  if (pruned) bits.push(`pruned ${pruned} old item${pruned === 1 ? "" : "s"}`);
  return (bits.join(", ") || "already tidy") + "." + (r.notes?.length ? ` (${r.notes[0]})` : "");
}
function onConsolidated(ev) {
  $("graph-query").textContent = "";
  loadGraph();
  if (consVisible()) loadConsolidation();
  if (ev.report?.manual) addMsg("info", "🧹 Memory tidied: " + consSummary(ev.report));
}
$("mem-cons-run").onclick = async () => {
  $("mem-cons-run").disabled = true;
  await fetch(API + "/api/memory/consolidate", { method: "POST" });
  loadConsolidation();
};

// ------------------------------------------------------------------ v0.8: your files
const filesVisible = () => !$("settings").classList.contains("hidden") && !$("tab-files").classList.contains("hidden");
async function loadFiles() {
  try { renderFiles(await (await fetch(API + "/api/files")).json()); }
  catch (_) { $("files-list").replaceChildren(el("div", "hint", "File index unavailable.")); }
}
function renderFiles(st) {
  const STATE = { ready: "up to date", indexing: "reading…", queued: "queued", missing: "folder not found" };
  const rows = (st.folders || []).map((f) => {
    const r = el("div", "mrow");
    const n = el("span", "name");
    let d = `${f.files} file${f.files === 1 ? "" : "s"} · ${f.chunks} passages · ${STATE[f.state] || f.state}`;
    if (f.state === "indexing" && f.total) d += ` ${f.done}/${f.total}`;
    if (f.skipped) d += ` · ${f.skipped} without readable text`;
    n.append(el("b", "", f.path), el("span", "desc", d));
    const re = el("button", "pill", "Re-read");
    re.onclick = async () => { renderFiles(await (await fetch(API + "/api/files/reindex", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: f.path }) })).json()); };
    const del = el("button", "pill danger", "Remove");
    del.onclick = async () => {
      if (del.dataset.armed !== "1") { del.dataset.armed = "1"; del.textContent = "Forget its files?"; return; }
      renderFiles(await (await fetch(API + "/api/files/folders?path=" + encodeURIComponent(f.path), { method: "DELETE" })).json());
      loadGraph();
    };
    r.append(n, re, del);
    return r;
  });
  $("files-list").replaceChildren(...(rows.length ? rows : [el("div", "hint", "No folders yet. Add your notes, documents or projects folder.")]));
  const cur = st.current ? st.current.split(/[\\/]/).pop() : "";
  $("files-now").textContent = (cur ? `Reading ${cur}… ` : "") + (st.queued ? `${st.queued} waiting. ` : "") +
    (st.folders?.length ? (st.watching ? "Watching for changes." : "Checking for changes every 20 minutes.") : "");
}
async function addFolder(path) {
  $("files-err").textContent = "";
  if (!path) return;
  const r = await fetch(API + "/api/files/folders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
  const j = await r.json();
  if (!r.ok) { $("files-err").textContent = j.detail || "Couldn't add that folder."; return; }
  $("files-path").value = "";
  renderFiles(j);
}
$("files-add").onclick = () => addFolder($("files-path").value.trim());
$("files-path").onkeydown = (e) => { if (e.key === "Enter") addFolder($("files-path").value.trim()); };
$("files-browse").onclick = async () => {
  $("files-browse").textContent = "Choose in the dialog…";
  try {
    const r = await (await fetch(API + "/api/files/pick", { method: "POST" })).json();
    if (r.path) { $("files-path").value = r.path; await addFolder(r.path); }
    else if (r.error) $("files-err").textContent = "No folder picker available here: type or paste the path instead.";
  } finally { $("files-browse").textContent = "Browse…"; }
};
$("files-q").onkeydown = async (e) => {
  if (e.key !== "Enter" || !$("files-q").value.trim()) return;
  const r = await (await fetch(API + "/api/files/search?q=" + encodeURIComponent($("files-q").value.trim()))).json();
  const rows = r.hits.map((h) => {
    const row = el("div", "mrow");
    const n = el("span", "name");
    n.append(el("b", "", h.name + (h.loc ? ` · ${h.loc}` : "")), el("span", "snip", h.snippet));
    const o = el("button", "pill", "Open"); o.onclick = () => openFile(h.path);
    row.append(n, o);
    return row;
  });
  $("files-hits").replaceChildren(...(rows.length ? rows : [el("div", "hint", "Nothing found.")]));
};
function openFile(path) {
  fetch(API + "/api/files/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
}
function onFileSearch(ev) {
  if (ev.nodes?.length) graph.highlight(ev.nodes, [], "search");
  if (!ev.sources?.length) return;
  const box = el("div", "sources");
  box.append(el("span", "lbl", ev.auto ? "from your files:" : "sources:"));
  for (const s of ev.sources) {
    const b = el("button", "src"); b.type = "button"; b.title = `${s.path}\n\n${s.snippet}…`;
    b.append(el("span", "", "📄 " + s.name)); if (s.loc) b.append(el("small", "", s.loc));
    b.onclick = () => openFile(s.path);
    box.append(b);
  }
  $("transcript").appendChild(box);
  box.scrollIntoView({ block: "end" });
}

// ------------------------------------------------------------------ boot
(async () => {
  await waitForBackend();
  await loadSettings();
  connect();
  loadGraph();
  loadAgenda();
  setInterval(renderAgenda, 60000);
  const name = aname();
  addMsg("info", `Say “${settings.voice?.wake_phrase ? wakePhrase() : "Hey " + name}”, hold Space (or the mic) to talk, or type. Open ⚙ to pick your model, voices and apps.`);
})();
