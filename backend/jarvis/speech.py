"""Speech-to-text and text-to-speech providers (local + cloud)."""
from __future__ import annotations

import io
import threading
import wave

import httpx
import numpy as np

from . import config, models

SR = 16000  # mic sample rate used everywhere for STT


def to_wav_bytes(samples: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def from_wav_bytes(data: bytes) -> tuple[np.ndarray, int]:
    import soundfile as sf
    arr, sr = sf.read(io.BytesIO(data), dtype="float32")
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    return arr, sr


def pcm16_to_float(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


# =========================================================================== STT
class STT:
    _local = {"key": None, "model": None}
    _lock = threading.Lock()

    def __init__(self, settings: dict):
        self.s = settings
        self.cfg = settings["stt"]

    def transcribe(self, audio: np.ndarray) -> str:
        p = self.cfg.get("provider", "local_whisper")
        if p == "local_whisper":
            return self._local_whisper(audio)
        if p in ("openai", "groq"):
            return self._openai_like(audio, p)
        if p == "elevenlabs":
            return self._elevenlabs(audio)
        raise ValueError(f"Unknown STT provider {p}")

    def _local_whisper(self, audio: np.ndarray) -> str:
        from faster_whisper import WhisperModel
        name = self.cfg.get("local_model", "small")
        device = self.cfg.get("device", "auto")
        compute = self.cfg.get("compute_type", "int8")
        key = (name, device, compute)
        with self._lock:
            if self._local["key"] != key:
                models.ensure("whisper", name)
                self._local["model"] = WhisperModel(str(models.whisper_path(name)), device=device, compute_type=compute)
                self._local["key"] = key
            model = self._local["model"]
        segs, _ = model.transcribe(audio, language=self.cfg.get("language") or None, beam_size=1,
                                   vad_filter=True, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segs).strip()

    def _openai_like(self, audio: np.ndarray, p: str) -> str:
        from openai import OpenAI
        base = "https://api.groq.com/openai/v1" if p == "groq" else None
        model = "whisper-large-v3-turbo" if p == "groq" else self.cfg.get("openai_model", "whisper-1")
        client = OpenAI(api_key=config.api_key(self.s, p), base_url=base)
        r = client.audio.transcriptions.create(model=model, file=("speech.wav", to_wav_bytes(audio, SR)),
                                               language=self.cfg.get("language") or None)
        return r.text.strip()

    def _elevenlabs(self, audio: np.ndarray) -> str:
        r = httpx.post("https://api.elevenlabs.io/v1/speech-to-text",
                       headers={"xi-api-key": config.api_key(self.s, "elevenlabs")},
                       data={"model_id": "scribe_v1"},
                       files={"file": ("speech.wav", to_wav_bytes(audio, SR), "audio/wav")}, timeout=60)
        r.raise_for_status()
        return r.json().get("text", "").strip()

    @classmethod
    def preload(cls, settings: dict):
        """Warm the local model in the background so the first utterance is fast."""
        if settings["stt"].get("provider") == "local_whisper":
            threading.Thread(target=lambda: cls(settings)._local_whisper(np.zeros(SR // 2, np.float32)),
                             daemon=True).start()


# =========================================================================== TTS
class TTS:
    _cache: dict = {}
    _lock = threading.Lock()

    def __init__(self, settings: dict):
        self.s = settings
        self.cfg = settings["tts"]

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        p = self.cfg.get("provider", "piper")
        fn = {"piper": self._piper, "kokoro": self._kokoro, "elevenlabs": self._elevenlabs,
              "fish_audio": self._fish, "openai": self._openai}.get(p)
        if fn is None:
            raise ValueError(f"Unknown TTS provider {p}")
        return fn(text)

    # local ------------------------------------------------------------------
    def _piper(self, text: str):
        from piper import PiperVoice
        voice_id = self.cfg.get("piper_voice", "en_GB-alan-medium")
        with self._lock:
            key = ("piper", voice_id)
            if key not in self._cache:
                models.ensure("piper", voice_id)
                onnx, _ = models.piper_paths(voice_id)
                self._cache[key] = PiperVoice.load(str(onnx))
            voice = self._cache[key]
        speed = float(self.cfg.get("speed", 1.0)) or 1.0
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            try:  # piper-tts >= 1.3
                from piper import SynthesisConfig
                voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=1.0 / speed))
            except ImportError:  # piper-tts 1.2
                voice.synthesize(text, w, length_scale=1.0 / speed)
        return from_wav_bytes(buf.getvalue())

    def _kokoro(self, text: str):
        from kokoro_onnx import Kokoro
        q = self.cfg.get("kokoro_quality", "int8")
        with self._lock:
            key = ("kokoro", q)
            if key not in self._cache:
                models.ensure("kokoro", q)
                m, v = models.kokoro_paths(q)
                self._cache[key] = Kokoro(str(m), str(v))
            k = self._cache[key]
        voice = self.cfg.get("kokoro_voice", "bm_george")
        lang = "en-gb" if voice.startswith("b") else "en-us"
        samples, sr = k.create(text, voice=voice, speed=float(self.cfg.get("speed", 1.0)), lang=lang)
        return np.asarray(samples, dtype=np.float32), sr

    # cloud ------------------------------------------------------------------
    def _elevenlabs(self, text: str):
        vid = self.cfg.get("elevenlabs_voice_id")
        r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=pcm_22050",
                       headers={"xi-api-key": config.api_key(self.s, "elevenlabs")},
                       json={"text": text, "model_id": self.cfg.get("elevenlabs_model", "eleven_flash_v2_5")},
                       timeout=60)
        r.raise_for_status()
        return pcm16_to_float(r.content), 22050

    def _fish(self, text: str):
        body = {"text": text, "format": "wav", "latency": "balanced"}
        if self.cfg.get("fish_reference_id"):
            body["reference_id"] = self.cfg["fish_reference_id"]
        r = httpx.post("https://api.fish.audio/v1/tts",
                       headers={"Authorization": f"Bearer {config.api_key(self.s, 'fish_audio')}",
                                "model": "s1"},
                       json=body, timeout=60)
        r.raise_for_status()
        return from_wav_bytes(r.content)

    def _openai(self, text: str):
        from openai import OpenAI
        client = OpenAI(api_key=config.api_key(self.s, "openai"))
        r = client.audio.speech.create(model=self.cfg.get("openai_model", "gpt-4o-mini-tts"),
                                       voice=self.cfg.get("openai_voice", "onyx"), input=text,
                                       response_format="pcm")
        return pcm16_to_float(r.read()), 24000
