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
│ audio.py   mic capture, energy VAD, playback             │
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

Hands-free mode uses a simple energy-based voice detector and mutes the mic while Jarvis speaks, so
headphones aren't required. While a permission prompt is open you can answer by voice ("yes" / "no").

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

Everything lives in `~/.jarvis/`: `settings.json` (including API keys, stored in plain text with user-only permissions), `memory.md` (what Jarvis remembers about you, editable in ⚙ → Memory), `models/` (downloaded speech models) and `browser-profile/` (the automation browser's logins).
Environment variables like `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ELEVENLABS_API_KEY` and `FISH_AUDIO_API_KEY` also work.

## Building an installer

`cd app && npm run build` produces an installer that includes the backend source. The installed app still
needs Python with the requirements installed: set `JARVIS_PYTHON` to that interpreter (or keep the repo's
`backend/.venv`). Freezing the backend into a single sidecar binary (PyInstaller) is the next step for a
one-click install.

## Prototype limits / next steps

- Replies aren't streamed token by token yet; speech starts once the model's turn finishes (sentences are then synthesized and played in a pipeline).
- No wake word yet ("Hey Jarvis"); openWakeWord would slot into `audio.py`.
- One screen is captured (the primary monitor).
- Switching LLM provider starts a fresh conversation.
