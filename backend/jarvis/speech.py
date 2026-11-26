"""Speech-to-text and text-to-speech providers (local + cloud)."""
from __future__ import annotations

import io
import os
import re
import threading
import wave

import httpx
import numpy as np

from . import config, lang, models

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

    last_language = ""          # language Whisper heard last (ta / en / …), for "answer in kind"

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
        opt = lang.stt_options(self.s)
        language, prompt = opt["language"], opt["prompt"]
        if opt["restrict"] and len(audio) > SR // 2 and not name.endswith(".en"):
            language = self._pick_language(model, audio, opt["restrict"])
            prompt = lang.STT_PROMPT_TA if language == "ta" else None
        if name.endswith(".en"):
            language, prompt = "en", None
        segs, info = model.transcribe(audio, language=language, beam_size=1, vad_filter=True,
                                      condition_on_previous_text=False, initial_prompt=prompt)
        text = " ".join(s.text.strip() for s in segs).strip()
        STT.last_language = getattr(info, "language", language or "") or ""
        if prompt and text.strip() == prompt.strip():       # Whisper echoing its prompt on silence
            return ""
        return text

    @staticmethod
    def _pick_language(model, audio: np.ndarray, allowed: tuple) -> str:
        """Tamil or English (whichever is likelier), so Tanglish isn't misheard as Malayalam, Hindi, etc."""
        try:
            if hasattr(model, "detect_language"):
                _, _, probs = model.detect_language(audio)
            else:
                _, info = model.transcribe(audio[: SR * 30], language=None, beam_size=1)
                probs = info.all_language_probs or []
            p = dict(probs)
            return max(allowed, key=lambda k: p.get(k, 0.0))
        except Exception:  # noqa: BLE001
            return allowed[0]

    def _openai_like(self, audio: np.ndarray, p: str) -> str:
        from openai import OpenAI
        base = "https://api.groq.com/openai/v1" if p == "groq" else None
        model = "whisper-large-v3-turbo" if p == "groq" else self.cfg.get("openai_model", "whisper-1")
        client = OpenAI(api_key=config.api_key(self.s, p), base_url=base)
        opt = lang.stt_options(self.s)
        kw = {"prompt": opt["prompt"] or (lang.STT_PROMPT_MIX if opt["restrict"] else None)}
        r = client.audio.transcriptions.create(model=model, file=("speech.wav", to_wav_bytes(audio, SR)),
                                               language=opt["language"] or None, **{k: v for k, v in kw.items() if v})
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
        if os.environ.get("JARVIS_NO_PRELOAD"):      # tests/CI: no 0.5 GB download racing interpreter exit
            return
        if settings["stt"].get("provider") == "local_whisper":
            threading.Thread(target=lambda: cls(settings)._local_whisper(np.zeros(SR // 2, np.float32)),
                             daemon=True).start()


# =========================================================================== TTS
class TTS:
    _cache: dict = {}
    _lock = threading.Lock()
    notice = None               # callable(text): tell the user once when a voice falls back (set by the session)
    _noticed: set = set()

    def __init__(self, settings: dict):
        from . import character
        self.s = settings
        self.base = settings["tts"]
        self.cfg = character.tts_cfg(settings)     # the character's voice laid over the chosen provider

    def synthesize(self, text: str, style: dict | None = None) -> tuple[np.ndarray, int]:
        try:
            return self._synthesize(text, style)
        except Exception as e:  # noqa: BLE001
            if self.cfg is self.base or self.cfg == self.base:
                raise
            # the character's voice isn't available (e.g. not downloaded yet, offline): use the user's own voice
            self._notice(f"The character's voice isn't available ({e}); using your usual voice.")
            self.cfg = self.base
            return self._synthesize(text, style)

    def _synthesize(self, text: str, style: dict | None = None) -> tuple[np.ndarray, int]:
        """style (optional, from emotion.STYLES): speed multiplier, instructions, stability, style."""
        self.style = style or {}
        p = self.cfg.get("provider", "piper")
        if p != "none" and lang.has_tamil(text):
            route = lang.tamil_route(self.s)
            if route:
                try:
                    return self._run(route, text)
                except Exception as e:  # noqa: BLE001
                    self._notice(f"The Tamil voice ({route}) isn't available ({e}); using the main voice.")
        if p == "clone":
            try:
                from . import voiceclone
                return voiceclone.synthesize(self.s, text, "hi" if re.search(r"[\u0900-\u097F]", text) else "en",
                                             self._speed())
            except Exception as e:  # noqa: BLE001
                fb = self.cfg.get("clone_fallback") or "piper"
                self._notice(f"Custom voice unavailable ({e}). Using {fb} for now.")
                return self._run(fb if fb not in ("clone", "none") else "piper", text)
        return self._run(p, text)

    def _run(self, p: str, text: str):
        fn = {"piper": self._piper, "kokoro": self._kokoro, "elevenlabs": self._elevenlabs,
              "fish_audio": self._fish, "openai": self._openai, "edge": self._edge}.get(p)
        if fn is None:
            raise ValueError(f"Unknown TTS provider {p}")
        return fn(text)

    def _notice(self, msg: str):
        key = msg[:60]
        if key in TTS._noticed:
            return
        TTS._noticed.add(key)
        print(f"[jarvis] {msg}")
        if TTS.notice:
            try:
                TTS.notice(msg)
            except Exception:  # noqa: BLE001
                pass

    def _speed(self) -> float:
        return (float(self.cfg.get("speed", 1.0)) or 1.0) * float(getattr(self, "style", {}).get("speed", 1.0))

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
        speed = self._speed()
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
        samples, sr = k.create(text, voice=voice, speed=self._speed(), lang=lang)
        return np.asarray(samples, dtype=np.float32), sr

    # cloud ------------------------------------------------------------------
    def _elevenlabs(self, text: str):
        vid = self.cfg.get("elevenlabs_voice_id")
        r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=pcm_22050",
                       headers={"xi-api-key": config.api_key(self.s, "elevenlabs")},
                       json=self._el_body(text), timeout=60)
        r.raise_for_status()
        return pcm16_to_float(r.content), 22050

    def _el_body(self, text: str) -> dict:
        body = {"text": text, "model_id": self.cfg.get("elevenlabs_model", "eleven_flash_v2_5")}
        st = getattr(self, "style", {})
        if st:
            body["voice_settings"] = {"stability": st.get("stability", 0.5), "similarity_boost": 0.75,
                                      "style": st.get("style", 0.0), "speed": max(0.7, min(1.2, self._speed()))}
        return body

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

    def _edge(self, text: str):
        """Microsoft Edge's free online neural voices (used for Tamil: ta-IN-ValluvarNeural)."""
        import asyncio

        import edge_tts
        voice = (self.s.get("language") or {}).get("edge_voice") or "ta-IN-ValluvarNeural"

        async def go() -> bytes:
            buf = bytearray()
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
            async for chunk in edge_tts.Communicate(text, voice, rate=lang.edge_rate(self._speed()), proxy=proxy).stream():
                if chunk.get("type") == "audio":
                    buf += chunk["data"]
            return bytes(buf)
        data = asyncio.run(go())
        if not data:
            raise RuntimeError("no audio from the Edge voice")
        from .voiceclone import decode
        return decode(data)

    def _openai(self, text: str):
        from openai import OpenAI
        client = OpenAI(api_key=config.api_key(self.s, "openai"))
        model = self.cfg.get("openai_model", "gpt-4o-mini-tts")
        kw = {}
        if self.cfg is not self.base:
            from . import character
            instr = character.voice_instructions(self.s, getattr(self, "style", {}))
        else:
            instr = getattr(self, "style", {}).get("instructions") or ""
        if instr and "gpt-4o" in model:
            kw["instructions"] = instr
        if abs(self._speed() - 1.0) > 0.01:
            kw["speed"] = self._speed()
        r = client.audio.speech.create(model=model, voice=self.cfg.get("openai_voice", "onyx"), input=text,
                                       response_format="pcm", **kw)
        return pcm16_to_float(r.read()), 24000
