# Jarvis — a voice companion that can use your computer

A desktop prototype of a warm, Jarvis-style AI companion. You talk to it (or type), it talks back,
and it can operate your computer end to end: shell commands (including administrator actions),
files, mouse and keyboard, apps, and a real web browser.

```
┌──────────────── Tauri desktop app (app/) ────────────────┐
│  UI (ui/): animated orb, transcript, push-to-talk,       │
│  hands-free mode, settings, local model downloads        │
└───────────────┬──────────────────────────────────────────┘
                │ HTTP + WebSocket on 127.0.0.1:8765 (origin-locked)
┌───────────────▼──────────── Python agent (backend/) ─────┐
│ agent.py   conversation loop, confirmation gate, voice   │
│ llm.py     Claude (Anthropic SDK) or any OpenAI-          │
│            compatible API: OpenAI, DeepSeek, OpenRouter, │
│            Groq, Ollama, LM Studio, vLLM, custom URL      │
│ tools.py   shell (+admin), screenshot, mouse, keyboard,  │
│            open apps, files, clipboard, Playwright        │
│            browser, memory, system info                  │
│ speech.py  STT: local Whisper (faster-whisper), OpenAI,  │
│            Groq, ElevenLabs                              │
│            TTS: local Piper / Kokoro, ElevenLabs,        │
│            Fish Audio, OpenAI                            │
│ audio.py   mic capture, energy VAD, barge-in, playback   │
│ memory.py  long-term knowledge graph (LadybugDB/Kùzu)    │
│ consolidation.py  nightly memory clean-up                │
│ files.py   your folders indexed into the graph + FTS     │
│ models.py  in-app download of Whisper/Piper/Kokoro/wake  │
│ wakeword.py   "Hey Jarvis" (openWakeWord ONNX)           │
│ scheduler.py  reminders, routines, briefings             │
│ mcp_client.py apps via MCP (stdio + streamable HTTP)     │
│ skills.py     learned workflows + teach mode             │
│ emotion.py    tone from your voice -> how Jarvis speaks  │
│ awareness.py  screen glances -> proactive offers         │
│ nodes.py      control your other PCs                     │
│ phones.py     your Android phone: pairing, encrypted     │
│               link, the `phone` tool                     │
└──────────────────────────────────────────────────────────┘
```

The Tauri shell starts the Python backend automatically and stops it when you quit.

## Requirements

- Python 3.10 – 3.12 (3.11 recommended; some speech wheels lag behind the newest Python)
- Node.js 18+
- Rust (stable) and the Tauri prerequisites for your OS: <https://v2.tauri.app/start/prerequisites/>
  (Windows: WebView2 + MSVC build tools; Linux: `libwebkit2gtk-4.1-dev`, `libgtk-3-dev`, `librsvg2-dev`; Linux also needs `portaudio19-dev` for the mic)

## Setup

```bash
# macOS / Linux
./setup.sh
# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File setup.ps1
```

This creates `backend/.venv`, installs the Python packages, installs Playwright's Chromium and the Tauri CLI.

## Run

```bash
cd app
npm run dev
```

The first launch compiles the Rust shell (a few minutes). Then:

1. Click **⚙ → Brain**, pick a provider, paste your API key, Save.
   - **Claude**: provider `anthropic`, model `claude-opus-5-5` (or `claude-sonnet-5-5` / `claude-haiku-5-5` for faster, cheaper replies).
   - **DeepSeek**: provider `deepseek`, model `deepseek-chat`.
   - **OpenAI**: provider `openai`, e.g. `gpt-4.1`.
   - **Ollama / any OpenAI-compatible server**: provider `ollama` or `custom`, set Base URL (e.g. `http://localhost:11434/v1`) and the model name. Pick a model that supports tool calling (e.g. `qwen2.5`, `llama3.1`).
   - Tick **Model can see images** only for vision models; that turns on screenshots for clicking around the screen.
2. **⚙ → Local models**: download **Whisper small** and the **Alan** Piper voice (they also download on first use).
3. Hold **Space** (or the mic button) and talk, or switch on **Hands-free** and just speak.

Without the desktop shell you can run the backend alone and use the UI in a browser:

```bash
cd backend && .venv/bin/python -m jarvis.server     # Windows: .venv\Scripts\python -m jarvis.server
# open http://127.0.0.1:8765
```

## Voice

| | Local (offline, default) | Cloud |
|---|---|---|
| Speech to text | faster-whisper: tiny / base / **small** (default) / medium / distil-large-v3 / large-v3-turbo, int8 on CPU, CUDA if available | OpenAI Whisper, Groq Whisper, ElevenLabs Scribe |
| Text to speech | **Piper** (default, en_GB "Alan" — light enough for old laptops), **Kokoro** 82M (more natural, int8 ≈ 90 MB), **your own cloned voice** (Fish Speech or XTTS-v2) | ElevenLabs, Fish Audio (incl. cloning), OpenAI TTS, Edge neural Tamil voice |

Low-end PC tips: Whisper `base` or `tiny.en` + Piper. With a GPU, set STT device to `cuda` and use `distil-large-v3`.

Hands-free mode uses an adaptive energy-based voice detector. While a permission prompt is open you can
answer by voice ("yes" / "no").

### Custom voice (voice cloning)

Give Jarvis any voice. In ⚙ → Voice → **Custom voice**, upload or record 6 to 30 seconds of clean speech
(and, ideally, type what you said), then choose **Custom voice** as the text-to-speech provider. Engines:

- **Fish Speech** (default, fully local): run Fish Speech / OpenAudio's API server
  (`python -m tools.api_server --listen 127.0.0.1:8080` from the fish-speech repo). Jarvis sends your sample
  with every request, so there's nothing to register. A GPU makes it real-time.
- **XTTS-v2** (local): runs inside Jarvis if the `coqui-tts` package is installed (source installs), otherwise
  start [xtts-api-server](https://github.com/daswer123/xtts-api-server) on port 8020. Note the XTTS model's
  non-commercial licence.
- **Fish Audio** (cloud): no install; uses your Fish Audio API key and the same sample.

If the engine isn't reachable, Jarvis says so once and keeps talking in the fallback voice (Piper by default).
The sample stays in `~/.jarvis/voice/`.

### Tamil and English

Talk in Tamil, English, or both mixed freely (⚙ → Voice → Language, default "Tamil and English"). Whisper is
held to Tamil or English, whichever it hears more of, so Tanglish isn't misheard as another language, and
Jarvis answers in kind: Tamil script for Tamil, Tanglish for Tanglish, a natural Chennai-style mix for a mix,
English for English (or force English/Tamil). Sentences in Tamil script are spoken by a Tamil voice: Microsoft
Edge's free neural "Valluvar" or "Pallavi" (online), or your ElevenLabs/OpenAI voice if that's your main voice.
"சரி" / "வேண்டாம்" answer permission prompts, and "நிறுத்து" or "போதும்" interrupt. For Tamil, Whisper
**medium** or **large-v3-turbo** hears much better than small.

### Natural interruptions (barge-in)

In hands-free mode Jarvis keeps listening while it talks:

1. Sustained sound louder than what the speakers are playing (an echo guard) becomes a candidate.
   Short bumps and coughs are ignored.
2. After ~0.9 s of speech it transcribes that snippet and decides:
   - **Real interruption** (two or more words of new speech, or "stop", "wait", "hold on", "Jarvis",
     "actually"…): it stops talking at once, keeps recording you, and answers what you said.
   - **Not an interruption** ("mm-hmm", "yeah", "okay", noise, or its own voice coming back through the
     speakers): it carries on talking and shows "(heard … kept talking)" in the transcript.

Tune it in ⚙ → Memory → Interruptions. With loud speakers and no headphones, raise **Echo guard** if Jarvis
interrupts itself.

## The orb: Jarvis's face

The home screen shows a GPU particle orb (Three.js, about 14,000 particles) that shows what Jarvis is doing
and feeling:

- **Shape follows the state.** A slow breathing sphere when idle, a ring that leans toward you while it
  listens, a swirling violet vortex while it thinks, and a sphere that ripples outward while it speaks.
  While a tool runs, the particles form an icon for it: a phone, a clock (reminders), a node cluster
  (memory), a globe (web and apps) or a gear (everything else).
- **Sound moves it.** Jarvis's voice pushes the particles out in waves; your voice draws them in.
- **Mood sets colour and pace.** Jarvis tags each reply with the mood it's speaking in (calm, happy,
  excited, curious, focused, concerned, apologetic, playful); the tag is hidden from the chat and the voice.
  Between replies, the emotion detector's read of you takes over: stressed turns a slow, settling teal,
  tired a soft lavender. Errors flash red.

⚙ → Behaviour → Look turns it off (back to the simple glow) or sets the quality. **Auto** switches to a
5,000-particle, 30 fps low-power mode on battery, on 4-core machines, or when the first seconds run slowly.
Without WebGL the app quietly keeps the glow.

## Screen HUD and hand gestures (desktop)

**Screen HUD** (⚙ → Desktop, on by default). A transparent, Iron Man style layer over every monitor:

- **Shows what Jarvis does.** A lock-on reticle wherever it clicks, drags or scrolls, a scan line sweeping the
  screen when it takes a screenshot, the keys it presses (never the text it types), and glowing corner brackets
  while it's working.
- **Teaches you.** Ask "show me how to export this as PDF" and Jarvis points instead of clicking: an animated
  arrow and a caption on each button, one step at a time (the `point` tool).
- **Live stats.** CPU (with per-core bars), memory, network, battery, the current step and the plan, in the top
  right corner of your main screen: only while Jarvis is working, or always.
- **Never in the way.** The overlay is click-through, stays off the taskbar, and is excluded from screen capture,
  so it doesn't appear in Jarvis's own screenshots or when you share your screen.

**Hand gestures** (⚙ → Desktop, off until you turn it on). Your normal webcam and MediaPipe hand tracking,
running on the CPU at about 15 frames a second (slower while no hand is in view):

| Gesture | What happens |
| --- | --- |
| ✋ open palm, hold ½ s | wake Jarvis (or stop it talking). With the mic off, it listens while your palm is up |
| ✊ fist, hold ½ s | stop what Jarvis is doing |
| 🤏 pinch and move | spin the orb; a quick pinch shows or hides the Jarvis window |
| swipe left / right | switch windows (Alt+Esc on Windows) |
| swipe up / down | maximise / minimise the active window |

The orb leans toward your hand, blooms on a palm and shrinks on a fist; the HUD shows a cursor where your hand is.
Frames never leave the PC and nothing is recorded: each one becomes 21 hand points and is dropped. The first start
downloads the 7.5 MB hand model to `~/.jarvis/models/gestures/`.

## Always ready: "Hey Jarvis"

The default listening mode is the wake word. Jarvis runs the open-source
[openWakeWord](https://github.com/dscripka/openWakeWord) "hey jarvis" model locally on ONNX Runtime
(about 2% of one CPU core, ~4 MB downloaded on first use). Say "Hey Jarvis", hear a soft chime, then talk.
After it answers it keeps listening for about 10 seconds, so follow-ups don't need the wake word. Saying
"Hey Jarvis" while it's talking interrupts it. Switch modes in the Voice tile: **Off** (push to talk),
**Wake word**, or **Always** (always listening). Tune sensitivity in ⚙ → Proactive.

**Or just clap twice**, like in the films. A double clap (two sharp claps 0.1 to 0.8 s apart) wakes Jarvis in
Wake word and Always modes, and it answers "Yes?" (in Tamil if that's what you last spoke). The detector looks
for short, broadband impulses after a moment of quiet with nothing sustained around them, so speech, music,
applause, a third clap, desk thumps and door slams don't count. Turn it off or change the sensitivity in
⚙ → Proactive → Listening.

## Proactive

- **Reminders and routines.** "Remind me at 6 to call Mum", "every Monday at 9, clean my Downloads",
  "every weekday at 8:30 check the server". Reminders are spoken; tasks run through the agent (with your usual
  confirmation rules). Jobs live in `~/.jarvis/schedule.json` and show in the Agenda tile.
- **Morning and evening briefings.** Turn them on in ⚙ → Proactive: weather (Open-Meteo, no key), your calendar
  and email when an app is connected, today's reminders and what Jarvis remembers.
- **Ambient mode** (on by default). Jarvis chimes in on its own like a friend in the room: "You've been coding
  for two hours, fancy a break?", a stretch reminder after long screen time, a welcome back (with what's coming
  up) after you've been away. It only uses the active window title and keyboard/mouse idle time, stays quiet
  in meetings, while you're away, while it's busy and during **quiet hours** (23:00 to 07:00 by default), and
  speaks at most once every 45 minutes. All of it is adjustable in ⚙ → Proactive → Ambient mode.
- **Screen awareness** (off by default). Every minute or so while idle, Jarvis glances at the active window
  (and a small screenshot if the model can see images) and, only when there's a clear problem like an error
  dialog or a failed build, offers to help. Offers are rate-limited and never repeated. Nothing is stored.

## A smarter agent

- **Plans with checkpoints.** Multi-step jobs start with a visible plan in the Agenda tile; each step is ticked off
  as it completes and saved, so an interrupted job can be resumed later.
- **Self-check.** After every click, keystroke or app launch Jarvis gets the active window title and (for vision
  models) a fresh screenshot, and confirms the action worked before moving on.
- **Skills it learns.** Jarvis saves reusable workflows to `~/.jarvis/skills/*.md` after multi-step jobs, when you
  say "remember how to do this", or in **teach mode**: press the eye button (or say "watch me"), do the task, say
  "I'm done". It records window changes, clicks, typing (masked in password/sign-in windows), screenshots and your
  narration, then writes the steps. Skills are versioned and improved whenever Jarvis learns something new.
- **Apps via MCP.** ⚙ → Connections takes any [Model Context Protocol](https://modelcontextprotocol.io) server:
  local ones started with a command (`npx`, `uvx`…) or remote ones by URL with a token header. Templates are
  included for Gmail + Calendar (workspace-mcp), GitHub, Notion, Spotify and your files. Read-only tools run freely;
  tools that change things ask first (unless you mark a server `"trusted": true`). OAuth-only remote servers aren't
  supported yet; use a token or a local server.
- **Background agents.** "Research the best NAS under 30k in the background" starts a separate agent with its own
  conversation. It uses the shell, files, browser (its own profile) and apps, but never your mouse or keyboard, so
  you can keep working and chatting. You hear about it when it finishes; cancel it from the Agenda tile.
- **Emotion-aware voice.** Jarvis compares each utterance's loudness, pitch, pitch variation and speaking rate with
  your own running baseline, plus word choice, and estimates a mood (stressed, excited, down, tired). The agent gets
  a short `<tone>` hint and the voice adapts: calmer and slower when you're stressed, brighter when you're excited.
  ElevenLabs and OpenAI `gpt-4o-mini-tts` also get matching style settings.
- **Multi-monitor.** Screenshots can target any monitor (or all at once); clicks map to the right screen.
- **Multi-PC.** Install Jarvis on another PC and tick ⚙ → Connections → "Let my other PCs control this one". It shows
  an address and token. Add them on your main PC under "Other PCs I control" and ask "on the laptop, open Spotify".
  Each request needs the token; admin commands from other PCs are refused unless you allow them. Use it on your
  home network or a private network like Tailscale, not the open internet.

## Smart home (Home Assistant)

Control lights, AC, TV, fans, plugs, blinds and scenes by voice through [Home Assistant](https://www.home-assistant.io/).
In Home Assistant, open your profile → Security → **Long-lived access tokens**, create one, then paste it with your
Home Assistant URL (e.g. `http://homeassistant.local:8123`) into ⚙ → Connections → Smart home and press
**Test connection**. Then just talk: "bedroom lights to 40 percent, warm", "AC 24 degrees, cool mode",
"turn off the TV", "TV volume 20", "switch the TV to Netflix", "all lights off", "லைட் ஆஃப் பண்ணு".

Jarvis matches devices by their names and rooms (so it never needs entity ids), knows your device list, and can
hand any sentence to Home Assistant's own Assist. Unlocking doors, disarming alarms and opening covers (garage)
always ask first. The token stays in `~/.jarvis/settings.json` and is masked in the app.

## Your phone (Android companion)

[Jarvis for Android](https://github.com/sakthiveltofficial/jarvis-mobile) pairs with this PC by QR code. Download the
APK from its Releases, then on the PC open ⚙ → Connections → **My phone** → **Pair a phone** and scan the code.

- **Talk or chat from the phone.** Hold the mic to talk; this PC transcribes it and can speak the reply through the
  phone. Confirmations ("Shall I go ahead?") can be answered on either device.
- **Jarvis uses the phone** through a `phone` tool: read the screen as a numbered list of elements and tap, swipe,
  scroll and type; open apps and links; read, open, reply to and dismiss notifications; read and send SMS; call,
  answer, hang up and read the call log; search contacts; browse files and copy them between phone and PC; location,
  ring (find my phone), torch, volume and media keys; and shell commands in the optional Shizuku "power mode".
  You choose what's allowed in the app's Phone tab. Sending messages, calling, deleting or writing files and shell
  commands follow your "Ask before acting" setting.
- **Private.** The phone connects straight to this PC on port 8767 over your Wi-Fi or Tailscale; there's no cloud
  relay. Pairing uses a one-time secret in the QR code (10 minutes, single use) to hand the phone its own device key;
  every message after that is AES-256-GCM encrypted and bound to the connection. Remove a phone in Connections at any
  time. The phone link only listens while "Let my phone connect" is on (pairing turns it on).
- **Jarvis AR (v0.9.0, phone app v0.2.0+).** The app's AR tab (Google ARCore) brings Jarvis into the room:
  - **The orb in your room**: the same particle orb and mood colours as this screen, floating where you put it and
    rippling as it talks.
  - **"What am I looking at?"**: point the camera at a device, a document, a plant or an error on a screen and ask;
    the photo goes to Jarvis (needs a model with vision), which explains it or acts on it with its tools.
  - **Smart-home overlay**: scan a room and floating controls appear on the lights, AC and TV it recognises from
    Home Assistant. Pin a control to have it reappear there next time.
  - **Spatial notes**: pin a note to your desk or fridge; a photo of the spot lets ARCore find it again when you come
    back (nothing leaves your PC and phone), with an optional reminder.
  - **Object finder**: with "Remember what I see" on, the phone glances every 20 s and Jarvis catalogues what's
    where for two weeks. Ask "where did I leave my keys?" (the `spatial` tool) to get the last sighting and a photo.
    Sightings live in `~/.jarvis/ar/`.
- Prefer not to install an app? [mobile-mcp](https://github.com/mobile-next/mobile-mcp) works as an MCP server under
  Connections → Apps while the phone is connected over (wireless) ADB.

## Presence, health nudges and the weekly life report (v0.10.0)

Settings → **Presence**. Everything here stays on this PC.

- **At the desk** (webcam, off until you turn it on). Press **Learn my face** and look at the camera for 8 seconds.
  Jarvis then greets you when you sit down (first time today, or after 20+ minutes away), **locks the PC** when you
  walk away (no face and no keyboard/mouse for 45 s), and when **someone else sits down** while you're away it says
  hello, locks the PC and posts an alert (mirrored to your paired phone). If someone looks over your shoulder you get
  a quiet notice instead. Faces are found with OpenCV YuNet and recognised with SFace on the CPU; your face is stored
  only as numbers in `~/.jarvis/presence/owner.npz`, never as a photo, and never sent to the AI model. "Forget my face"
  deletes it. Visitor photos are opt-in (30 days, `~/.jarvis/presence/visitors`).
- **One camera stream.** Hand gestures and Presence share a single webcam stream (`camera.py`), so they can both be on.
- **Health nudges**: posture (slouching, leaning in, tilting or craning for 3+ minutes, compared with a "sit up
  straight" baseline you calibrate), 20-20-20 eye breaks, water every hour at the desk ("I drank some water" resets it),
  and a bedtime nudge. Short spoken lines, at most one every 15 minutes, never in meetings or quiet hours (the bedtime
  nudge excepted). Without the camera, screen time comes from keyboard and mouse activity.
- **Weekly life report**: once a minute Jarvis notes which app is in front (its name and a category like coding or
  meetings, never the window title) in `~/.jarvis/life.db`. Every Sunday at 19:00 it reads you a short report in its
  own voice: where the time went, your goals and habits, water, posture, overnight time off the PC, compared with the
  week before. Set goals by talking ("I want to exercise 3 times a week", "code at most 40 hours a week", "8 glasses of
  water a day") and log habits ("I went for a run"). Ask "how was my week?" any time. Reports are saved as Markdown
  in `~/.jarvis/reports`. Only the totals go to the model to phrase it.

## Long-term memory (knowledge graph)

Jarvis builds a personal knowledge graph in an embedded graph database
([LadybugDB](https://github.com/LadybugDB/ladybug), the maintained fork of Kùzu; Cypher, no server):

- **Saving:** after every turn, the configured LLM extracts durable knowledge (people, relationships,
  projects, preferences, plans, dates, corrections) into `Entity` nodes and `Fact` edges. When something
  changes ("I moved to Bangalore"), the old fact is marked outdated rather than deleted, so the graph keeps
  a timeline over months and years. Saying "remember that…" saves right away.
- **Recall:** each message searches the graph (keyword match on entities and facts, then a one-hop
  expansion) and attaches the relevant subgraph to the prompt. The most recent facts about you are always
  in the system prompt, and the agent can call `memory_search` to dig deeper.
- **See it:** switch the left pane to **Memory graph**. Nodes being searched pulse cyan and new memories
  flash green. Drag nodes, scroll to zoom, double-click to fit, and click a node to see its facts
  (including outdated ones) or forget it. "show outdated facts" shows the history.
- v1's `memory.md` is imported into the graph automatically on first start.

### Nightly clean-up

Once a night (⚙ → Memory → Nightly clean-up, default 03:00; if the PC is off then, it catches up at the next
start once Jarvis is idle) and whenever you press **Clean up now**, Jarvis:

- **summarises each day** you talked into a short diary entry (a `Digest` node linked to the people and things
  it covers), so "what did we do last Tuesday?" still works after old raw snippets are pruned;
- **merges duplicates**: entities with the same name in different spellings ("the Acme Corp" / "Acme Corp")
  automatically, near misses ("Acme" / "Acme Corporation", "Mom" / "Mum") after asking the model; facts that
  repeat each other are folded into one;
- **fades stale facts**: every recall or repeat reinforces a fact; an unused fact's strength halves every
  *half-life* (120 days by default) and below a threshold it is marked outdated (reason "faded"), kept as
  history. Names, family, birthdays, home, work and allergies never fade;
- **prunes** outdated history older than 2 years (configurable, 0 = forever), raw conversation snippets older
  than 30 days whose day has a summary, and orphan entities nothing points to.

Merging, fading and pruning work without a model; summaries and the smart merges use the Brain model, or a
cheaper one set under "Model for clean-up and reading files" (e.g. `claude-haiku-5-5`).

## Your files

⚙ → **Files**: add folders (type a path or **Browse…**). Jarvis reads notes (`.md`, `.txt`…), PDFs, Word
(`.docx`), PowerPoint (`.pptx`), HTML, notebooks and code, splits them into passages (per page, heading or
block of lines) and stores them in the graph as `Document` and `Chunk` nodes linked to the people, companies and
projects they mention, plus a local full-text index (SQLite FTS5, `~/.jarvis/files.db`). Folders are watched
for changes, and re-checked every 20 minutes and at startup. `node_modules`, `.git`, build folders, hidden
folders and files over 25 MB are skipped.

Ask "what did that contract say about payment?" and matching passages are attached to the question
automatically (or the agent calls `search_files`); answers name the file and page, and the sources show up as
chips in the conversation that open the file. Documents linked to things Jarvis knows appear in the memory
graph as blue nodes. Linking documents to entities costs one quick model call per document (code is
skipped) and can be turned off. Scanned PDFs without a text layer are listed as "without readable text".

## Computer control and safety

Jarvis has the same power over your machine as your user account, and more when you approve admin actions:

- `run_shell` with `elevated: true` shows the OS's own admin prompt (UAC on Windows, the macOS password dialog, `pkexec` on Linux).
- **⚙ → Behaviour → Ask before acting**:
  - *Only for risky actions* (default): deleting, moving, killing processes, registry/service/system changes, admin commands, overwriting files, running JavaScript in pages.
  - *For every action that changes something*.
  - *Never*: full autonomy, no prompts. Use with care: a web page the agent reads could try to talk it into doing something you didn't ask for.
- Press **Esc** or **Stop** to interrupt at any time. Moving the mouse into a screen corner aborts pyautogui actions.
- The backend only listens on 127.0.0.1 and rejects requests from other web origins, so web pages in your normal browser can't drive it.
- macOS: grant your terminal (or the Jarvis app) **Accessibility**, **Screen Recording** and **Microphone** permissions in System Settings → Privacy & Security.

## Data

Everything lives in `~/.jarvis/`: `settings.json` (including API keys, app tokens and paired phones' device keys, stored in plain text with user-only permissions), `graph.db` (the knowledge graph, including indexed files), `files.db` (full-text index of your files),
`consolidation.json` (clean-up history), `models/` (downloaded speech and wake word models), `skills/`, `schedule.json`, `plans.json`, `voice_profile.json` (your voice baseline for tone detection), `logs/` (MCP server output) and `browser-profile/` (the automation browser's logins).
Environment variables like `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ELEVENLABS_API_KEY` and `FISH_AUDIO_API_KEY` also work.

## Download (Windows)

Grab the latest `Jarvis_*_x64-setup.exe` from [Releases](../../releases). It includes everything (no Python
or Node needed). The installer isn't code-signed yet, so SmartScreen may ask you to confirm ("More info → Run anyway").

## Releases and CI

- `.github/workflows/ci.yml` runs on every push and PR: Python lint and smoke tests (graph memory,
  interruption rules, API routes), UI syntax checks, and a Windows `cargo check` of the Tauri shell.
- `.github/workflows/release.yml` runs when you push a tag like `v0.3.0` (or by hand from the Actions tab).
  It freezes the Python backend with PyInstaller (`backend/build_backend.py`), smoke-tests it, builds the
  Tauri NSIS installer with the frozen backend inside (`app/src-tauri/tauri.release.conf.json`), and publishes
  it as a GitHub Release.

To cut a release: bump the version in `app/package.json`, `app/src-tauri/tauri.conf.json` and
`app/src-tauri/Cargo.toml`, commit, then `git tag v0.x.y && git push origin v0.x.y`.

To build the same installer locally on Windows:

```powershell
cd backend; .venv\Scripts\python -m pip install pyinstaller; .venv\Scripts\python build_backend.py
cd ..\app; npx tauri build --config src-tauri/tauri.release.conf.json
```

## Prototype limits / next steps

- Replies aren't streamed token by token yet; speech starts once the model's turn finishes (sentences are then synthesized and played in a pipeline).
- Memory recall is lexical plus graph expansion. Adding embeddings would also match paraphrases ("my sibling" for "sister").
- One screen is captured (the primary monitor).
- Switching LLM provider starts a fresh conversation.
