"""Settings storage for Jarvis. Everything lives in ~/.jarvis (override with JARVIS_HOME)."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

HOME = Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis"))
MODELS_DIR = HOME / "models"
SETTINGS_FILE = HOME / "settings.json"
MEMORY_FILE = HOME / "memory.md"
BROWSER_PROFILE = HOME / "browser-profile"

for d in (HOME, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Presets for the LLM provider picker. "kind" decides which client is used:
#   anthropic -> Anthropic SDK, everything else -> OpenAI-compatible chat completions.
LLM_PRESETS = {
    "anthropic": {"kind": "anthropic", "base_url": "", "model": "claude-opus-5-5", "vision": True},
    "openai": {"kind": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1", "vision": True},
    "deepseek": {"kind": "openai", "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "vision": False},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet-5-5", "vision": True},
    "groq": {"kind": "openai", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile", "vision": False},
    "ollama": {"kind": "openai", "base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b", "vision": False},
    "custom": {"kind": "openai", "base_url": "http://localhost:8000/v1", "model": "my-model", "vision": False},
}

DEFAULTS = {
    "assistant_name": "Jarvis",
    "user_name": "",
    "llm": {
        "provider": "anthropic",
        "kind": "anthropic",
        "base_url": "",
        "model": "claude-opus-5-5",
        "vision": True,
        "effort": "low",          # Anthropic only: low keeps voice replies snappy
        "max_tokens": 8000,
        "temperature": 0.7,       # OpenAI-compatible only
    },
    # API keys per provider id (llm presets + voice providers)
    "api_keys": {},
    "stt": {
        "provider": "local_whisper",   # local_whisper | openai | groq | elevenlabs
        "local_model": "small",        # tiny | base | small | medium | large-v3 | distil-large-v3 (+ .en variants)
        "language": "",                # "" = auto detect
        "compute_type": "int8",
        "device": "auto",              # auto | cpu | cuda
        "openai_model": "whisper-1",
    },
    "tts": {
        "provider": "piper",           # piper | kokoro | elevenlabs | fish_audio | openai | none
        "piper_voice": "en_GB-alan-medium",
        "kokoro_voice": "bm_george",
        "kokoro_quality": "int8",      # int8 (~90MB) | fp32 (~310MB)
        "speed": 1.0,
        "elevenlabs_voice_id": "JBFqnCBsd6RMkjVDRZzb",  # "George"
        "elevenlabs_model": "eleven_flash_v2_5",
        "fish_reference_id": "",
        "openai_voice": "onyx",
        "openai_model": "gpt-4o-mini-tts",
    },
    "audio": {
        "input_device": None,
        "output_device": None,
        "vad_threshold": 2.5,          # speech = RMS above noise floor * this
        "silence_ms": 900,             # end of utterance after this much silence
        "barge_in": True,              # keep listening while speaking; real speech interrupts
        "barge_check_ms": 900,         # how much speech to hear before checking it's a real interruption
        "echo_gain": 0.6,              # raise if Jarvis interrupts itself through your speakers
    },
    "memory": {
        "auto_save": True,             # extract and save knowledge after every turn
    },
    "agent": {
        # always: confirm every action that changes something
        # risky:  confirm only destructive / elevated / sensitive actions (default)
        # never:  full autonomy, no prompts
        "confirm_mode": "risky",
        "max_steps": 40,
        "speak_replies": True,
        "headless_browser": False,
    },
}

_lock = threading.Lock()


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    with _lock:
        data = {}
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text("utf-8"))
            except Exception:
                data = {}
        return _deep_merge(DEFAULTS, data)


def save(new: dict) -> dict:
    merged = _deep_merge(load(), new)
    with _lock:
        SETTINGS_FILE.write_text(json.dumps(merged, indent=2), "utf-8")
        try:
            os.chmod(SETTINGS_FILE, 0o600)  # holds API keys
        except OSError:
            pass
    return merged


def api_key(settings: dict, provider: str) -> str:
    env_names = {
        "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
        "openrouter": "OPENROUTER_API_KEY", "groq": "GROQ_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY",
        "fish_audio": "FISH_AUDIO_API_KEY",
    }
    return settings.get("api_keys", {}).get(provider) or os.environ.get(env_names.get(provider, ""), "") or ""


def public(settings: dict) -> dict:
    """Settings safe to send to the UI (keys masked)."""
    s = copy.deepcopy(settings)
    s["api_keys"] = {k: ("••••" + v[-4:] if v else "") for k, v in s.get("api_keys", {}).items()}
    return s
