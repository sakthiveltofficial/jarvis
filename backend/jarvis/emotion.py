"""Emotion-aware voice: estimate how the user sounds and adapt how Jarvis speaks.

Prosody (loudness, pitch, pitch variation, speaking rate) is compared with the user's own running
baseline, so a naturally loud or fast speaker isn't always "stressed". Word choice adds valence
(positive vs negative). The result is a coarse mood with the cues behind it:

    stressed | excited | down | tired | neutral

It is a hint, not a diagnosis: the LLM gets it as a <tone> note and the TTS gets a matching style
(calmer and slower for stress, brighter for excitement, softer when someone sounds down).
"""
from __future__ import annotations

import json
import re
import threading

import numpy as np

from . import config

SR = 16000
PROFILE = config.HOME / "voice_profile.json"

POS = set("""great awesome amazing love loved nice cool yay finally excellent perfect brilliant happy glad excited
fantastic wonderful thanks thank yes woohoo wow sweet best fun beautiful lovely won win works worked""".split())
NEG = set("""ugh damn dammit hate stuck broken broke error errors fail failed failing stressed stress deadline late
annoying annoyed angry frustrated frustrating terrible awful worst problem problems crash crashed bug bugs tired
exhausted sad upset worried anxious nervous ridiculous useless wrong cant can't nothing hurry urgent asap""".split())

STYLES = {
    "stressed": {"speed": 0.93, "instructions": "Speak calmly and warmly, unhurried and reassuring, with a steady low pace.",
                 "stability": 0.75, "style": 0.1},
    "excited": {"speed": 1.05, "instructions": "Speak brightly and upbeat, with a smile in your voice and a lively pace.",
                "stability": 0.35, "style": 0.45},
    "down": {"speed": 0.92, "instructions": "Speak gently and softly, kind and supportive, with a slower pace.",
             "stability": 0.7, "style": 0.15},
    "tired": {"speed": 0.95, "instructions": "Speak softly and simply, relaxed and easy-going.",
              "stability": 0.65, "style": 0.1},
}

ADVICE = {
    "stressed": "Be calm, brief and reassuring; take load off them, offer to handle things.",
    "excited": "Match their energy a little and share the moment.",
    "down": "Be gentle and kind; check in briefly without being heavy.",
    "tired": "Keep it short and easy; don't pile on.",
}

_lock = threading.Lock()


def _pitch(frame: np.ndarray) -> float:
    """Autocorrelation pitch for one 40 ms frame, 70-400 Hz. 0 if unvoiced."""
    f = frame - frame.mean()
    if np.sqrt(np.mean(f ** 2)) < 1e-3:
        return 0.0
    ac = np.correlate(f, f, "full")[len(f) - 1:]
    lo, hi = SR // 400, SR // 70
    if hi >= len(ac):
        return 0.0
    i = lo + int(np.argmax(ac[lo:hi]))
    return SR / i if ac[i] > 0.3 * ac[0] else 0.0


def features(audio: np.ndarray, text: str) -> dict | None:
    if len(audio) < SR * 0.6:
        return None
    hop, win = 160, 640
    frames = [audio[i:i + win] for i in range(0, len(audio) - win, hop)]
    rms = np.array([np.sqrt(np.mean(f ** 2)) for f in frames]) + 1e-9
    voiced = rms > max(0.25 * np.percentile(rms, 95), 0.004)
    if voiced.sum() < 10:
        return None
    pitches = np.array([_pitch(f) for f, v in zip(frames, voiced) if v])
    pitches = pitches[pitches > 0]
    words = len(re.findall(r"[A-Za-z']+", text))
    dur = voiced.sum() * hop / SR
    return {
        "loud": float(20 * np.log10(np.mean(rms[voiced]))),
        "pitch": float(np.median(pitches)) if len(pitches) > 5 else 0.0,
        "pitch_var": float(np.std(pitches) / (np.median(pitches) + 1e-9)) if len(pitches) > 5 else 0.0,
        "rate": words / max(dur, 0.3),
        "words": words,
    }


def _load() -> dict:
    try:
        return json.loads(PROFILE.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {"n": 0}


def _save(p: dict):
    try:
        PROFILE.write_text(json.dumps(p), "utf-8")
    except OSError:
        pass


def valence(text: str) -> tuple[int, list[str]]:
    words = re.findall(r"[a-z']+", text.lower())
    pos = [w for w in words if w in POS]
    neg = [w for w in words if w in NEG]
    score = len(pos) - len(neg) + (1 if text.count("!") and pos else 0)
    return score, neg or pos


def analyze(audio: np.ndarray, text: str, profile: dict | None = None, update: bool = True) -> dict:
    """Return {"mood", "arousal", "valence", "cues"} for one utterance."""
    feat = features(audio, text) if audio is not None and len(audio) else None
    val, cue_words = valence(text)
    cues: list[str] = []
    arousal = 0.0
    with _lock:
        prof = profile if profile is not None else _load()
        if feat and prof.get("n", 0) >= 3:
            spreads = {"loud": 4.0, "pitch": 0.15, "pitch_var": 0.35, "rate": 0.25}
            z = {
                "loud": (feat["loud"] - prof["loud"]) / spreads["loud"],
                "pitch": ((feat["pitch"] / prof["pitch"] - 1) / spreads["pitch"]) if feat["pitch"] and prof.get("pitch") else 0.0,
                "pitch_var": ((feat["pitch_var"] / max(prof["pitch_var"], 1e-3) - 1) / spreads["pitch_var"])
                if feat["pitch_var"] and prof.get("pitch_var") else 0.0,
                "rate": ((feat["rate"] / max(prof["rate"], 0.1) - 1) / spreads["rate"]) if feat["words"] >= 3 else 0.0,
            }
            z = {k: float(np.clip(v, -3, 3)) for k, v in z.items()}
            arousal = 0.35 * z["loud"] + 0.25 * z["pitch"] + 0.2 * z["pitch_var"] + 0.2 * z["rate"]
            names = {"loud": ("louder", "quieter"), "pitch": ("higher-pitched", "lower-pitched"),
                     "pitch_var": ("more animated", "flatter"), "rate": ("faster", "slower")}
            for k, v in sorted(z.items(), key=lambda kv: -abs(kv[1]))[:2]:
                if abs(v) >= 1:
                    cues.append(f"{names[k][0 if v > 0 else 1]} than usual")
        if feat and update:
            n = prof.get("n", 0)
            a = 1 / (n + 1) if n < 20 else 0.05          # running mean, then a slow EMA
            for k in ("loud", "pitch", "pitch_var", "rate"):
                if feat[k] or k == "loud":
                    prof[k] = feat[k] if k not in prof else (1 - a) * prof[k] + a * feat[k]
            prof["n"] = n + 1
            if profile is None:
                _save(prof)
    if cue_words:
        cues.append("words like " + ", ".join(f'"{w}"' for w in cue_words[:3]))

    if arousal > 0.8:
        mood = "stressed" if val < 0 else "excited" if val > 0 or arousal > 1.6 else "neutral"
    elif arousal < -0.8:
        mood = "down" if val < 0 else "tired"
    else:
        mood = "stressed" if val <= -2 else "excited" if val >= 2 else "neutral"
    return {"mood": mood, "arousal": round(arousal, 2), "valence": val, "cues": cues}


def tone_note(user: str, m: dict) -> str:
    if m["mood"] == "neutral":
        return ""
    why = f" ({'; '.join(m['cues'])})" if m["cues"] else ""
    return f"<tone>{user} sounds {m['mood']}{why}. {ADVICE.get(m['mood'], '')}</tone>"
