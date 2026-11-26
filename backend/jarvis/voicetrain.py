"""Train the wake word and the double clap on your own voice, hands, mic and room (v0.11).

Settings → Listening → "Train on my voice" records a few short takes through the live mic:

  wake word  5 x "Hey Jarvis" + one take of you talking normally (what must NOT wake me).
             Each take is run through the wake word pipeline; the embedding window where you finish
             the phrase becomes a personal template, and the similarity cut-off is placed between
             your wake word takes and your normal speech. Saved to ~/.jarvis/wake_profile.npz.
  clap       4 x double clap. Every clap-like impulse is measured (loudness over the room's noise,
             share of energy above 1 kHz, length, gap between the two claps) and the detector's
             thresholds are set a safe margin below what your claps actually look like.
             Saved to ~/.jarvis/clap_profile.json.

Everything here is plain numpy on recorded arrays, so it is tested without a microphone.
"""
from __future__ import annotations

import json
import threading
import time

import numpy as np

from . import config
from .clap import ClapDetector

SR = 16000
CHUNK = 1280                         # wake word step (80 ms)
WAKE_TAKES = 5
CLAP_TAKES = 4


def clap_path():
    return config.HOME / "clap_profile.json"


def wake_path():
    return config.HOME / "wake_profile.npz"


# --------------------------------------------------------------------------- claps
def find_claps(audio: np.ndarray) -> list[dict]:
    """Every clap-like impulse in a recording, measured with a deliberately permissive detector."""
    d = ClapDetector(1.0)
    d.ratio, d.abs_min, d.hf_min, d.max_len = 4.0, 1e-7, 0.05, 0.25
    d.collect = True
    d.process(np.asarray(audio, np.float32))
    d.process(np.zeros(int(SR * 0.3), np.float32))      # flush a clap still being measured
    out = []
    for c in d.candidates:
        out.append({"t": round(c["t"], 3), "peak": c["peak"], "ratio": c["peak"] / max(c["floor"], 1e-9),
                    "hf": c["hf"], "len": c["len"]})
    return out


def _pick_double(claps: list[dict]) -> tuple[dict, dict] | None:
    """The two loudest impulses, if they look like one double clap (similar, 0.08 .. 1.3 s apart)."""
    if len(claps) < 2:
        return None
    top = sorted(claps, key=lambda c: c["peak"], reverse=True)[:2]
    a, b = sorted(top, key=lambda c: c["t"])
    gap = b["t"] - a["t"]
    if not 0.08 <= gap <= 1.3 or max(a["peak"], b["peak"]) / min(a["peak"], b["peak"]) > 25:
        return None
    return a, b


def check_clap_take(audio: np.ndarray, sensitivity: float = 0.5, profile: dict | None = None) -> dict:
    claps = find_claps(audio)
    pair = _pick_double(claps)
    d = ClapDetector(sensitivity, profile=profile)
    fired = bool(d.process(audio)) | bool(d.process(np.zeros(int(SR * 0.6), np.float32)))
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if pair is None:
        msg = ("I didn't hear any claps. Check the microphone in Settings → Voice." if not claps else
               "I heard one clap. Clap twice, about a third of a second apart." if len(claps) == 1 else
               "That didn't sound like one double clap. Try two crisp claps, then stay quiet.")
        ok = False
    else:
        gap = pair[1]["t"] - pair[0]["t"]
        msg = f"Got it: two claps {gap * 1000:.0f} ms apart."
        ok = True
    return {"ok": ok, "message": msg, "claps": len(claps), "pair": list(pair) if pair else None,
            "default_fired": fired, "default_reason": d.last_event, "peak": round(peak, 3)}


def calibrate_claps(takes: list[np.ndarray]) -> dict:
    """Detector thresholds from your double-clap takes (at least two good ones)."""
    pairs = [p for p in (_pick_double(find_claps(t)) for t in takes) if p]
    if len(pairs) < 2:
        raise ValueError("I need at least two good double-clap takes. Record a few more.")
    cl = [c for p in pairs for c in p]
    gaps = [b["t"] - a["t"] for a, b in pairs]
    # Margins: half the weakest clap's loudness over the noise, 3/4 of its brightness, 1.6x the longest.
    return {
        "ratio": float(np.clip(min(c["ratio"] for c in cl) * 0.35, 4.0, 60.0)),
        "abs_min": float(np.clip(min(c["peak"] for c in cl) * 0.2, 1e-7, 1e-2)),
        "hf_min": float(np.clip(min(c["hf"] for c in cl) * 0.75, 0.08, 0.5)),
        "max_len": float(np.clip(max(c["len"] for c in cl) * 1.6 + 0.02, 0.08, 0.25)),
        "min_gap": float(np.clip(min(gaps) * 0.6, 0.06, 0.3)),
        "max_gap": float(np.clip(max(gaps) * 1.5 + 0.1, 0.5, 1.4)),
        "takes": len(pairs),
        "trained_at": time.strftime("%Y-%m-%d %H:%M"),
    }


def load_clap_profile() -> dict | None:
    try:
        return json.loads(clap_path().read_text("utf-8"))
    except (OSError, ValueError):
        return None


def save_clap_profile(p: dict):
    clap_path().write_text(json.dumps(p, indent=1), "utf-8")


# --------------------------------------------------------------------------- wake word
def _chunk_rms(audio: np.ndarray) -> np.ndarray:
    n = len(audio) // CHUNK
    if n == 0:
        return np.zeros(0, np.float32)
    a = np.asarray(audio[:n * CHUNK], np.float32).reshape(n, CHUNK)
    return np.sqrt(np.mean(a ** 2, axis=1))


def speech_end(audio: np.ndarray) -> int | None:
    """Index of the last 80 ms chunk with speech in it, or None when the take is silent."""
    r = _chunk_rms(audio)
    if len(r) == 0:
        return None
    floor = float(np.percentile(r, 20))
    loud = np.where(r > max(floor * 3.0, 0.008))[0]
    return int(loud[-1]) if len(loud) else None


def check_wake_take(ww, audio: np.ndarray, custom: bool = False) -> dict:
    """One wake word take: the stock model's best score and the windows around where you finished.
    custom: your own wake phrase, so the stock "Hey Jarvis" score means nothing; the window where you finish counts."""
    scores, wins = ww.scan(audio)
    end = speech_end(audio)
    if end is None or len(scores) < 8:
        return {"ok": False, "message": "I didn't hear anything. Speak up, or check the microphone.",
                "score": 0.0}
    if custom:
        if end < 6:
            return {"ok": False, "message": "Start speaking a moment after the beep, and finish before it ends.",
                    "score": 0.0}
        i = min(end + 1, len(scores) - 1)
        idx = sorted({max(6, i - 1), i, min(i + 1, len(scores) - 1)})
        return {"ok": True, "message": "Got it.", "score": 0.0, "windows": wins[idx], "_end": end}
    lo, hi = max(6, end - 2), min(len(scores) - 1, end + 4)
    if lo > hi:
        return {"ok": False, "message": "Start speaking a moment after the beep, and finish before it ends.",
                "score": 0.0}
    seg = scores[lo:hi + 1]
    best = float(seg.max()) if len(seg) else 0.0
    i = lo + int(np.argmax(seg)) if best >= 0.1 else min(end + 1, len(scores) - 1)
    idx = sorted({i, min(i + 1, len(scores) - 1)})
    heard = best >= ww.threshold
    msg = ("Got it. The stock model already hears you clearly." if heard else
           f"Got it. The stock model only scored you {best:.2f} (needs {ww.threshold:.2f}), so your samples will help.")
    return {"ok": True, "message": msg, "score": round(best, 3), "windows": wins[idx], "_end": end}


def check_negative_take(ww, audio: np.ndarray) -> dict:
    scores, wins = ww.scan(audio)
    if speech_end(audio) is None:
        return {"ok": False, "message": "I didn't hear you talking. Say a sentence or two in your normal voice."}
    return {"ok": True, "message": "Got it.", "windows": wins[6:], "score": round(float(scores[6:].max(initial=0)), 3)}


def calibrate_wake(pos: list[np.ndarray], neg: list[np.ndarray], pos_scores: list[float], name: str,
                   custom: bool = False) -> dict:
    """pos: per take, a few (16, 96) windows; neg: (M, 16, 96) windows of normal speech / room noise."""
    if len(pos) < 3:
        raise ValueError("I need at least three good wake word takes. Record a few more.")
    flat = [np.asarray(p, np.float32).reshape(len(p), -1) for p in pos]
    negf = np.concatenate([np.asarray(n, np.float32).reshape(len(n), -1) for n in neg]) if neg else \
        np.zeros((0, flat[0].shape[1]), np.float32)
    allw = np.concatenate(flat + ([negf] if len(negf) else []))
    mean = allw.mean(axis=0)

    def norm(m):
        m = m - mean
        return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)

    tp = [norm(f) for f in flat]
    # How close is each take to the OTHER takes (leave one out)? That's what a live hit will look like.
    loo = []
    for i, t in enumerate(tp):
        others = np.concatenate([o for j, o in enumerate(tp) if j != i])
        loo.append(float((t @ others.T).max()))
    loo.sort()
    pos_lo = loo[1] if len(loo) >= 5 else loo[0]          # tolerate one odd take out of five
    templates = np.concatenate(tp)
    neg_hi = float((norm(negf) @ templates.T).max()) if len(negf) else 0.0
    if pos_lo > neg_hi + 0.04:
        sim_thr = pos_lo - (pos_lo - neg_hi) * 0.4          # a bit nearer your takes than your chat
        quality = "good"
    else:
        sim_thr = neg_hi + 0.03                             # stay above normal speech, even if it misses some
        quality = "weak"
    # a custom phrase has only your templates to go on (no stock model to agree), so it never goes as low
    sim_thr = float(np.clip(sim_thr, 0.62 if custom else 0.55, 0.97))
    gate = 0.0 if custom else (float(min(0.02, 0.25 * min(pos_scores))) if pos_scores else 0.0)
    return {"templates": templates.astype(np.float32), "mean": mean.astype(np.float32), "sim_thr": sim_thr,
            "gate": gate, "name": name, "custom": custom, "pos_lo": round(pos_lo, 3), "neg_hi": round(neg_hi, 3),
            "quality": quality, "takes": len(pos), "scores": [round(s, 3) for s in pos_scores],
            "trained_at": time.strftime("%Y-%m-%d %H:%M")}


def save_wake_profile(p: dict):
    meta = {k: v for k, v in p.items() if k not in ("templates", "mean")}
    with open(wake_path(), "wb") as f:
        np.savez(f, templates=p["templates"], mean=p["mean"], meta=json.dumps(meta))


def load_wake_profile(name: str | None = None) -> dict | None:
    try:
        with np.load(wake_path()) as z:
            meta = json.loads(str(z["meta"]))
            p = {**meta, "templates": z["templates"], "mean": z["mean"]}
    except (OSError, ValueError, KeyError):
        return None
    if name and p.get("name") != name:
        return None
    return p


def _summary_wake(p: dict | None) -> dict | None:
    if not p:
        return None
    return {k: p.get(k) for k in ("takes", "quality", "sim_thr", "scores", "trained_at", "name", "custom")}


# --------------------------------------------------------------------------- the guided flow
class Trainer:
    """Holds the takes of one training run. `session` is agent.Session (mic + settings)."""

    def __init__(self, session):
        self.s = session
        self.lock = threading.Lock()
        self.kind = None
        self.takes: list[dict] = []
        self._ww = None

    def _wake_model(self):
        if self.s.mic.wake is not None:
            return self.s.mic.wake
        if self._ww is None:
            from .wakeword import WakeWord
            v = self.s.settings.get("voice", {})
            self._ww = WakeWord(v.get("wake_word", "hey_jarvis"), float(v.get("wake_threshold", 0.5)))
        return self._ww

    def _profile_name(self) -> str:
        from .selfctl import wake_profile_name
        return wake_profile_name(self.s.settings)

    def _custom(self) -> bool:
        from .selfctl import custom_wake
        return custom_wake(self.s.settings)

    def status(self) -> dict:
        from .selfctl import wake_phrase
        return {"wake": _summary_wake(load_wake_profile(self._profile_name())), "phrase": wake_phrase(self.s.settings),
                "custom": self._custom(),
                "clap": load_clap_profile(), "wake_takes": WAKE_TAKES, "clap_takes": CLAP_TAKES,
                "kind": self.kind, "recorded": len(self.takes)}

    def reset(self, kind: str | None = None):
        self.kind, self.takes = kind, []

    def record(self, kind: str, step: str = "pos", seconds: float | None = None) -> dict:
        """Record one take: kind wake|clap, step pos|neg (neg = normal talking, wake word only)."""
        if kind not in ("wake", "clap"):
            raise ValueError("kind must be wake or clap")
        with self.lock:
            if self.kind != kind:
                self.reset(kind)
            secs = seconds or (3.0 if kind == "clap" else 2.5 if step == "pos" else 6.0)
            audio = self.s.mic.capture(float(min(10.0, max(1.5, secs))))
            if kind == "clap":
                v = self.s.settings.get("voice", {})
                live = self.s.mic.clap.profile if self.s.mic.clap is not None else load_clap_profile()
                r = check_clap_take(audio, float(v.get("clap_sensitivity", 0.5)), live)
                if r["ok"]:
                    self.takes.append({"step": "pos", "audio": audio})
            else:
                ww = self._wake_model()
                r = check_wake_take(ww, audio, self._custom()) if step == "pos" else check_negative_take(ww, audio)
                if r["ok"]:
                    self.takes.append({"step": step, "windows": r.pop("windows"), "score": r.get("score", 0.0)})
                r.pop("windows", None)
                r.pop("_end", None)
            r["recorded"] = len([t for t in self.takes if t["step"] == "pos"])
            return _jsonable(r)

    def finish(self, kind: str) -> dict:
        with self.lock:
            if self.kind != kind:
                raise ValueError("record some takes first")
            if kind == "clap":
                p = calibrate_claps([t["audio"] for t in self.takes])
                save_clap_profile(p)
                if self.s.mic.clap is not None:
                    self.s.mic.clap.set_profile(p)
                tested = sum(check_clap_take(t["audio"], 0.5, p)["default_fired"] for t in self.takes)
                msg = (f"Clap trained on {p['takes']} takes. With your profile I catch {tested} of "
                       f"{len(self.takes)} of them.")
                out = {"ok": True, "message": msg, "profile": p}
            else:
                pos = [t["windows"] for t in self.takes if t["step"] == "pos"]
                neg = [t["windows"] for t in self.takes if t["step"] == "neg"]
                scores = [t["score"] for t in self.takes if t["step"] == "pos"]
                p = calibrate_wake(pos, neg, scores, self._profile_name(), self._custom())
                save_wake_profile(p)
                for w in {id(x): x for x in (self.s.mic.wake, self._ww) if x is not None}.values():
                    w.custom = self._custom()
                    w.set_profile(p)
                if self.s.mic.wake is None and getattr(self.s, "voice_mode", "") == "wake":
                    try:                         # a custom phrase was waiting for training: switch the detector on
                        self.s.set_voice_mode("wake", False)
                    except Exception:  # noqa: BLE001
                        pass
                msg = f"Wake word trained on {p['takes']} takes of your voice."
                if p["quality"] == "weak":
                    msg += (" Your takes sounded a lot like your normal talking, so I set it on the safe side."
                            " Retrain in a quieter moment if it still misses you.")
                out = {"ok": True, "message": msg, "profile": _summary_wake(p)}
            self.reset()
            return _jsonable(out)

    def forget(self, kind: str):
        """Delete a trained profile and go back to the built-in detector."""
        path = clap_path() if kind == "clap" else wake_path()
        try:
            path.unlink()
        except OSError:
            pass
        if kind == "clap" and self.s.mic.clap is not None:
            self.s.mic.clap.set_profile(None)
        if kind == "wake":
            for w in (self.s.mic.wake, self._ww):
                if w is not None:
                    w.set_profile(None)
        return self.status()


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items() if not k.startswith("_")}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, (np.floating, np.integer, np.bool_)):
        return x.item()
    return x
