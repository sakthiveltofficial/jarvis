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
│ models.py  in-app download of Whisper/Piper/Kokoro       │
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
| Text to speech | **Piper** (default, en_GB "Alan" — light enough for old laptops), **Kokoro** 82M (more natural, int8 ≈ 90 MB) | ElevenLabs, Fish Audio, OpenAI TTS |

Low-end PC tips: Whisper `base` or `tiny.en` + Piper. With a GPU, set STT device to `cuda` and use `distil-large-v3`.

Hands-free mode uses an adaptive energy-based voice detector. While a permission prompt is open you can
answer by voice ("yes" / "no").

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

Everything lives in `~/.jarvis/`: `settings.json` (including API keys, stored in plain text with user-only permissions), `graph.db` (the knowledge graph), `models/` (downloaded speech models) and `browser-profile/` (the automation browser's logins).
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
- No wake word yet ("Hey Jarvis"); openWakeWord would slot into `audio.py`.
- One screen is captured (the primary monitor).
- Switching LLM provider starts a fresh conversation.
