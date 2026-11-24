// Jarvis UI — talks to the local Python agent over HTTP + WebSocket.
const API = location.port === "8765" ? location.origin : "http://127.0.0.1:8765";
const WS_URL = API.replace(/^http/, "ws") + "/ws";
const $ = (id) => document.getElementById(id);

let ws = null;
let state = "idle";
let level = 0, targetLevel = 0;
let handsfree = false;
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
  switch (ev.type) {
    case "state": state = ev.state; setStatus(); break;
    case "level": targetLevel = ev.value; break;
    case "handsfree": handsfree = ev.on; renderHandsfree(); break;
    case "user": addMsg("user", ev.text); break;
    case "assistant": addMsg("assistant", ev.text); break;
    case "info": addMsg("info", ev.text); break;
    case "error": addMsg("error", ev.text); break;
    case "tool_start": addTool(ev); break;
    case "tool_end": endTool(ev); break;
    case "confirm": showConfirm(ev); break;
    case "barge": onBarge(ev); break;
    case "memory_search": onMemorySearch(ev); break;
    case "memory_saving": $("mem-count").textContent = "· saving…"; break;
    case "memory_update": onMemoryUpdate(ev); break;
    case "confirm_done": if ($("confirm-box").dataset.id === ev.id) $("confirm-box").classList.add("hidden"); break;
  }
}

const STATUS = {
  idle: "At your service.", listening: "Listening…", transcribing: "Catching that…",
  thinking: "Thinking…", acting: "Working on it…", speaking: "Speaking…",
};
function setStatus() {
  $("status-text").textContent = STATUS[state] || state;
  $("state-label").textContent = state;
}

function addMsg(kind, text) {
  const d = document.createElement("div");
  d.className = "msg " + kind;
  d.textContent = text;
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
  box.innerHTML = `<h4>⚠ Permission needed</h4><div class="reason"></div><code></code>
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

$("btn-handsfree").onclick = () => send({ type: "handsfree", on: !handsfree });
function renderHandsfree() {
  $("btn-handsfree").textContent = "Hands-free: " + (handsfree ? "on" : "off");
  $("btn-handsfree").classList.toggle("on", handsfree);
}
$("btn-stop").onclick = () => send({ type: "stop" });
$("btn-new").onclick = async () => {
  await fetch(API + "/api/reset", { method: "POST" });
  $("transcript").innerHTML = "";
  addMsg("info", "New conversation.");
};

// ------------------------------------------------------------------ orb
const cv = $("orb"), cx = cv.getContext("2d");
const COLORS = {
  idle: [60, 200, 255], listening: [122, 240, 255], transcribing: [150, 200, 255],
  thinking: [140, 120, 255], acting: [255, 181, 71], speaking: [75, 227, 160],
};
let t0 = performance.now(), col = [60, 200, 255];
function draw(now) {
  const t = (now - t0) / 1000;
  level += (targetLevel - level) * 0.2;
  targetLevel *= 0.92;
  const target = COLORS[state] || COLORS.idle;
  col = col.map((c, i) => c + (target[i] - c) * 0.06);
  const [r, g, b] = col.map(Math.round);
  const W = cv.width, H = cv.height, R = W * 0.26;
  cx.clearRect(0, 0, W, H);
  const busy = state === "thinking" || state === "acting" || state === "transcribing";
  const glow = cx.createRadialGradient(W / 2, H / 2, R * 0.2, W / 2, H / 2, R * 2);
  glow.addColorStop(0, `rgba(${r},${g},${b},${0.35 + level * 0.4})`);
  glow.addColorStop(1, "rgba(0,0,0,0)");
  cx.fillStyle = glow; cx.fillRect(0, 0, W, H);
  // rings
  for (let k = 0; k < 3; k++) {
    cx.beginPath();
    const pts = 120;
    for (let i = 0; i <= pts; i++) {
      const a = (i / pts) * Math.PI * 2;
      const wob = Math.sin(a * (3 + k) + t * (1.2 + k * 0.7)) * (4 + level * 40) +
                  Math.sin(a * 7 - t * 2.3) * (busy ? 6 : 1.5);
      const rr = R * (0.82 + k * 0.16) + wob;
      const x = W / 2 + Math.cos(a) * rr, y = H / 2 + Math.sin(a) * rr;
      i ? cx.lineTo(x, y) : cx.moveTo(x, y);
    }
    cx.strokeStyle = `rgba(${r},${g},${b},${0.75 - k * 0.22})`;
    cx.lineWidth = 2.2 - k * 0.5;
    cx.stroke();
  }
  // core
  const core = cx.createRadialGradient(W / 2, H / 2, 0, W / 2, H / 2, R * (0.55 + level * 0.25));
  core.addColorStop(0, `rgba(255,255,255,0.9)`);
  core.addColorStop(0.25, `rgba(${r},${g},${b},0.85)`);
  core.addColorStop(1, `rgba(${r},${g},${b},0)`);
  cx.fillStyle = core; cx.beginPath(); cx.arc(W / 2, H / 2, R * 0.8, 0, Math.PI * 2); cx.fill();
  // spinner arc while busy
  if (busy) {
    cx.beginPath();
    cx.arc(W / 2, H / 2, R * 1.35, t * 3, t * 3 + 1.2);
    cx.strokeStyle = `rgba(${r},${g},${b},0.9)`; cx.lineWidth = 3; cx.stroke();
  }
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);

// ------------------------------------------------------------------ settings
$("btn-settings").onclick = openSettings;
$("btn-close-settings").onclick = () => $("settings").classList.add("hidden");
document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("hidden", x.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "models") refreshModels();
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
  $("brand-name").textContent = (settings.assistant_name || "Jarvis").toUpperCase();
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

  const d = await (await fetch(API + "/api/audio/devices")).json();
  opt($("audio-in"), [{ value: "", label: "System default" }, ...d.inputs.map((x) => ({ value: x.id, label: x.name }))], s.audio.input_device ?? "");
  opt($("audio-out"), [{ value: "", label: "System default" }, ...d.outputs.map((x) => ({ value: x.id, label: x.name }))], s.audio.output_device ?? "");

  $("beh-name").value = s.assistant_name; $("beh-user").value = s.user_name || "";
  $("beh-confirm").value = s.agent.confirm_mode; $("beh-steps").value = s.agent.max_steps;
  $("beh-silence").value = s.audio.silence_ms; $("beh-speak").checked = s.agent.speak_replies;
  $("beh-headless").checked = s.agent.headless_browser;
  $("mem-auto").checked = s.memory?.auto_save !== false;
  $("barge-on").checked = s.audio.barge_in !== false;
  $("barge-echo").value = s.audio.echo_gain ?? 0.6;
  $("barge-ms").value = s.audio.barge_check_ms ?? 900;
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
    },
    audio: { input_device: dev($("audio-in").value), output_device: dev($("audio-out").value), silence_ms: Number($("beh-silence").value || 900),
             barge_in: $("barge-on").checked, echo_gain: Number($("barge-echo").value || 0.6), barge_check_ms: Number($("barge-ms").value || 900) },
    memory: { auto_save: $("mem-auto").checked },
    agent: { confirm_mode: $("beh-confirm").value, max_steps: Number($("beh-steps").value || 40),
             speak_replies: $("beh-speak").checked, headless_browser: $("beh-headless").checked },
  };
}

$("btn-save").onclick = async () => {
  await fetch(API + "/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collect()) });
  await loadSettings();
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

async function loadGraph() {
  try {
    const g = await (await fetch(API + "/api/graph" + ($("graph-history").checked ? "?include_invalid=true" : ""))).json();
    graph.setData(g);
    $("mem-count").textContent = `· ${g.nodes.length}`;
    if (!graphLoaded && graphView) { graphLoaded = true; setTimeout(() => graph.fit(), 1200); }
  } catch (_) { $("mem-count").textContent = "· off"; }
}

function setView(g) {
  graphView = g;
  graph.visible = g;
  $("stage").classList.toggle("graph-mode", g);
  $("graph").classList.toggle("hidden", !g);
  $("graph-hud").classList.toggle("hidden", !g);
  if (!g) $("node-panel").classList.add("hidden");
  $("view-orb").classList.toggle("active", !g);
  $("view-graph").classList.toggle("active", g);
  if (g) loadGraph();
}
$("view-orb").onclick = () => setView(false);
$("view-graph").onclick = () => setView(true);
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
    d.onclick = () => { setView(true); graph.highlight(ev.nodes, ev.edges); };
    $("transcript").appendChild(d);
    d.scrollIntoView({ block: "end" });
  }
  $("graph-query").textContent = "🔎 " + ev.query.slice(0, 140);
  graph.highlight(ev.nodes, ev.edges, "search");
}

async function onMemoryUpdate(ev) {
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
  addMsg("info", "Hold Space (or the mic) to talk, or turn on hands-free. Open ⚙ to pick your model and voices.");
})();
