"""Custom voice: clone any voice from a short sample (6 to 30 seconds of clean speech).

The sample lives in ~/.jarvis/voice/sample.wav (+ sample.txt, what is said in it, which helps Fish Speech).
Engines (settings["tts"]["clone_engine"]):

  fish_speech   Fish Speech / OpenAudio running locally (its `api_server`, default http://127.0.0.1:8080).
                Fully offline, very natural, needs a decent GPU.
  xtts          Coqui XTTS-v2. In-process when the `coqui-tts` package is installed (dev installs), otherwise a
                local xtts-api-server (default http://127.0.0.1:8020). Offline; CPU works but is slow.
  fish_audio    Fish Audio's cloud with the same sample (no install, needs a Fish Audio API key).

If cloning fails (server not running, no sample yet) Jarvis falls back to the regular voice
(settings["tts"]["clone_fallback"], Piper by default) and says why once.
"""
from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import httpx
import numpy as np

from . import config

VOICE_DIR = config.HOME / "voice"
SAMPLE = VOICE_DIR / "sample.wav"
SAMPLE_TEXT = VOICE_DIR / "sample.txt"
SR = 24000
ENGINES = ("fish_speech", "xtts", "fish_audio")
XTTS_LANGS = {"en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"}

_xtts = {"model": None}
_xtts_lock = threading.Lock()


# --------------------------------------------------------------------------- audio helpers
def decode(data: bytes) -> tuple[np.ndarray, int]:
    """Any common audio file (wav, flac, ogg, mp3, m4a, webm) -> mono float32."""
    try:
        import soundfile as sf
        arr, sr = sf.read(io.BytesIO(data), dtype="float32")
        if arr.ndim > 1:
            arr = arr.mean(axis=1)
        return arr, sr
    except Exception:  # noqa: BLE001
        from faster_whisper.audio import decode_audio        # PyAV: handles mp3 / m4a / webm
        return decode_audio(io.BytesIO(data), sampling_rate=SR).astype(np.float32), SR


def resample(x: np.ndarray, sr: int, to: int) -> np.ndarray:
    if sr == to or len(x) == 0:
        return x.astype(np.float32)
    n = int(len(x) * to / sr)
    return np.interp(np.linspace(0, len(x), n, endpoint=False), np.arange(len(x)), x).astype(np.float32)


def clean(x: np.ndarray, sr: int, max_s: float = 30.0) -> np.ndarray:
    """Trim leading/trailing silence, cap the length, normalise to -1 dBFS peak."""
    x = resample(np.asarray(x, np.float32), sr, SR)
    if len(x) == 0:
        return x
    frame = SR // 50
    rms = np.array([np.sqrt(np.mean(x[i:i + frame] ** 2)) for i in range(0, len(x), frame)] or [0.0])
    thr = max(0.004, float(np.percentile(rms, 90)) * 0.08)
    voiced = np.where(rms > thr)[0]
    if len(voiced):
        a = max(0, voiced[0] * frame - SR // 10)
        b = min(len(x), (voiced[-1] + 1) * frame + SR // 5)
        x = x[a:b]
    x = x[: int(max_s * SR)]
    peak = float(np.max(np.abs(x))) or 1.0
    return (x / peak * 0.89).astype(np.float32)


def info() -> dict:
    if not SAMPLE.exists():
        return {"has_sample": False}
    try:
        import soundfile as sf
        i = sf.info(str(SAMPLE))
        secs = i.frames / i.samplerate
    except Exception:  # noqa: BLE001
        secs = 0.0
    return {"has_sample": True, "seconds": round(secs, 1), "text": sample_text(),
            "ok": secs >= 6, "advice": "" if secs >= 6 else "A sample of 6 to 30 seconds clones much better."}


def sample_text() -> str:
    try:
        return SAMPLE_TEXT.read_text("utf-8").strip()
    except OSError:
        return ""


def save_sample(data: bytes | None = None, text: str | None = None, audio: np.ndarray | None = None,
                sr: int = SR) -> dict:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    if data is not None or audio is not None:
        if audio is None:
            audio, sr = decode(data)
        x = clean(audio, sr)
        if len(x) < SR * 2:
            raise ValueError("That sample is too short or silent. Record 6 to 30 seconds of clear speech.")
        import soundfile as sf
        sf.write(str(SAMPLE), x, SR, subtype="PCM_16")
    if text is not None:
        SAMPLE_TEXT.write_text(text.strip(), "utf-8")
    return info()


def record(seconds: float, device=None) -> dict:
    import sounddevice as sd
    seconds = max(4.0, min(40.0, float(seconds)))
    rec = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="float32", device=device)
    sd.wait()
    return save_sample(audio=rec[:, 0], sr=SR)


def delete_sample():
    for p in (SAMPLE, SAMPLE_TEXT):
        try:
            p.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- engines
def synthesize(settings: dict, text: str, language: str = "en", speed: float = 1.0,
               transport: httpx.BaseTransport | None = None) -> tuple[np.ndarray, int]:
    if not SAMPLE.exists():
        raise RuntimeError("no voice sample yet (Settings → Voice → Custom voice)")
    cfg = settings.get("tts") or {}
    engine = cfg.get("clone_engine", "fish_speech")
    if engine == "xtts":
        return _xtts_synth(cfg, text, language, speed, transport)
    if engine == "fish_audio":
        key = config.api_key(settings, "fish_audio")
        if not key:
            raise RuntimeError("add a Fish Audio API key for cloud cloning")
        return _fish(text, "https://api.fish.audio", {"Authorization": f"Bearer {key}", "model": "s1"}, transport)
    url = (cfg.get("clone_url") or "http://127.0.0.1:8080").rstrip("/")
    return _fish(text, url, {}, transport)


def fish_request(text: str) -> bytes:
    import msgpack
    ref = {"audio": SAMPLE.read_bytes(), "text": sample_text()}
    body = {"text": text, "format": "wav", "references": [ref], "reference_id": None, "normalize": True,
            "chunk_length": 200, "streaming": False, "latency": "normal", "max_new_tokens": 1024,
            "top_p": 0.7, "repetition_penalty": 1.2, "temperature": 0.7}
    return msgpack.packb(body, use_bin_type=True)


def _fish(text: str, base: str, headers: dict, transport) -> tuple[np.ndarray, int]:
    with httpx.Client(timeout=120, transport=transport) as c:
        r = c.post(f"{base}/v1/tts", content=fish_request(text),
                   headers={"Content-Type": "application/msgpack", **headers})
    if r.status_code >= 400:
        raise RuntimeError(f"Fish Speech error {r.status_code}: {r.text[:200]}")
    return decode(r.content)


def _xtts_synth(cfg: dict, text: str, language: str, speed: float, transport) -> tuple[np.ndarray, int]:
    lang = language if language in XTTS_LANGS else "en"
    try:
        from TTS.api import TTS as Coqui        # coqui-tts (optional, heavy: PyTorch)
    except Exception:  # noqa: BLE001
        Coqui = None
    if Coqui is not None:
        with _xtts_lock:
            if _xtts["model"] is None:
                os.environ.setdefault("COQUI_TOS_AGREED", "1")
                m = Coqui("tts_models/multilingual/multi-dataset/xtts_v2")
                try:
                    import torch
                    if torch.cuda.is_available():
                        m = m.to("cuda")
                except Exception:  # noqa: BLE001
                    pass
                _xtts["model"] = m
            wav = _xtts["model"].tts(text=text, speaker_wav=str(SAMPLE), language=lang, speed=speed)
        return np.asarray(wav, np.float32), 24000
    # xtts-api-server (https://github.com/daswer123/xtts-api-server) on this PC: it reads the sample by path.
    url = (cfg.get("clone_url") or "http://127.0.0.1:8020").rstrip("/")
    with httpx.Client(timeout=120, transport=transport) as c:
        r = c.post(f"{url}/tts_to_audio/", json={"text": text, "speaker_wav": str(Path(SAMPLE).resolve()),
                                                 "language": lang})
    if r.status_code >= 400:
        raise RuntimeError(f"XTTS server error {r.status_code}: {r.text[:200]}")
    return decode(r.content)


def status(settings: dict, transport: httpx.BaseTransport | None = None) -> dict:
    """Is the chosen engine reachable? (for the Settings page)"""
    cfg = settings.get("tts") or {}
    engine = cfg.get("clone_engine", "fish_speech")
    out = {"engine": engine, **info()}
    if engine == "fish_audio":
        out["ready"] = bool(config.api_key(settings, "fish_audio"))
        out["message"] = "Fish Audio cloud" if out["ready"] else "Add a Fish Audio API key"
        return out
    if engine == "xtts":
        try:
            import importlib.util
            if importlib.util.find_spec("TTS") is not None:
                out.update(ready=True, message="XTTS-v2 runs inside Jarvis")
                return out
        except Exception:  # noqa: BLE001
            pass
    default = "http://127.0.0.1:8020" if engine == "xtts" else "http://127.0.0.1:8080"
    url = (cfg.get("clone_url") or default).rstrip("/")
    try:
        with httpx.Client(timeout=3, transport=transport) as c:
            r = c.get(url + ("/docs" if engine == "xtts" else "/v1/health"))
        out.update(ready=r.status_code < 500, message=f"{engine.replace('_', ' ')} server at {url}")
    except Exception as e:  # noqa: BLE001
        out.update(ready=False, message=f"Can't reach {url} ({type(e).__name__}). Start the server, or pick another engine.")
    return out
