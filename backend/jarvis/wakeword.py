"""Wake word ("Hey Jarvis") detection.

Runs the openWakeWord ONNX pipeline (Apache-2.0, https://github.com/dscripka/openWakeWord) directly on
onnxruntime, which Jarvis already ships for Kokoro, so no extra Python packages are needed:

    16 kHz audio -> melspectrogram.onnx -> embedding_model.onnx (76 mel frames -> 96-d)
                 -> hey_jarvis.onnx (last 16 embeddings -> score 0..1)

It costs a few percent of one CPU core, so it is fine on low-end machines. If the models can't be
loaded, the session falls back to spotting "Jarvis" in transcripts.

v0.11: personal wake word. The stock model was trained on synthetic American voices and can score a
real voice, accent or cheap mic well under its threshold. After you record yourself saying the wake
word a few times (voicetrain.py), the 16x96 embedding windows at the end of each take become personal
templates: the detector also fires when the live window is close enough (cosine) to one of yours.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from . import models

CHUNK = 1280            # 80 ms at 16 kHz: one prediction per chunk


class WakeWord:
    def __init__(self, name: str = "hey_jarvis", threshold: float = 0.5):
        import onnxruntime as ort
        models.ensure("wakeword", name)
        mel, emb, ww = models.wake_paths(name)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        prov = ["CPUExecutionProvider"]
        self.mel = ort.InferenceSession(str(mel), opts, providers=prov)
        self.emb = ort.InferenceSession(str(emb), opts, providers=prov)
        self.ww = ort.InferenceSession(str(ww), opts, providers=prov)
        self._mel_in = self.mel.get_inputs()[0].name
        self._emb_in = self.emb.get_inputs()[0].name
        self._ww_in = self.ww.get_inputs()[0].name
        self.threshold = threshold
        self.name = name
        self.profile = None          # personal templates (see set_profile)
        self.last_sim = 0.0
        self._lock = threading.Lock()
        self._noise_feats = None
        self.reset()

    def set_profile(self, profile: dict | None):
        """{templates: (N, 1536), mean: (1536,), sim_thr, gate} from voicetrain.calibrate_wake, or None."""
        if profile is not None:
            t = np.asarray(profile["templates"], np.float32)
            profile = {**profile, "templates": t, "mean": np.asarray(profile["mean"], np.float32)}
        self.profile = profile

    def similarity(self, window: np.ndarray) -> float:
        """Best cosine similarity between a 16x96 embedding window and your recorded wake word."""
        p = self.profile
        if p is None:
            return 0.0
        return float(np.max(p["templates"] @ unit(window.reshape(-1) - p["mean"])))

    def reset(self):
        self.raw = np.zeros(CHUNK + 480, np.float32)
        self.pending = np.zeros(0, np.float32)
        self.mels = np.ones((76, 32), np.float32)
        if self._noise_feats is None:
            noise = (np.random.RandomState(0).randint(-1000, 1000, 16000 * 2)).astype(np.float32)
            self._noise_feats = self._embed_all(noise)
        self.feats = self._noise_feats.copy()
        self.n = 0
        self.last_fire = 0.0
        self.last_score = 0.0

    # -- feature extraction --------------------------------------------------
    def _melspec(self, pcm16: np.ndarray) -> np.ndarray:
        out = self.mel.run(None, {self._mel_in: pcm16[None, :].astype(np.float32)})[0]
        return np.squeeze(out).reshape(-1, 32) / 10.0 + 2.0

    def _embed_all(self, pcm16: np.ndarray) -> np.ndarray:
        spec = self._melspec(pcm16)
        wins = [spec[i:i + 76] for i in range(0, spec.shape[0] - 76 + 1, 8)]
        x = np.stack(wins)[:, :, :, None].astype(np.float32)
        return self.emb.run(None, {self._emb_in: x})[0].reshape(-1, 96)

    def _step(self, chunk16: np.ndarray) -> float:
        self.raw = np.concatenate([self.raw, chunk16])[-(CHUNK + 480):]
        self.mels = np.vstack([self.mels, self._melspec(self.raw)])[-120:]
        x = self.mels[-76:][None, :, :, None].astype(np.float32)
        e = self.emb.run(None, {self._emb_in: x})[0].reshape(1, 96)
        self.feats = np.vstack([self.feats, e])[-32:]
        score = float(self.ww.run(None, {self._ww_in: self.feats[-16:][None].astype(np.float32)})[0].reshape(-1)[0])
        self.n += 1
        return score if self.n > 5 else 0.0

    # -- public ----------------------------------------------------------------
    def process(self, block: np.ndarray) -> bool:
        """Feed float32 audio (-1..1) at 16 kHz of any length. True when the wake word was heard."""
        with self._lock:
            self.pending = np.concatenate([self.pending, block.astype(np.float32) * 32767.0])
            fired = False
            while len(self.pending) >= CHUNK:
                chunk, self.pending = self.pending[:CHUNK], self.pending[CHUNK:]
                s = self._step(chunk)
                self.last_score = s
                hit = s >= self.threshold
                if not hit and self.profile is not None and self.n > 5:
                    self.last_sim = self.similarity(self.feats[-16:])
                    hit = self.last_sim >= self.profile["sim_thr"] and s >= self.profile.get("gate", 0.0)
                now = time.time()
                if hit and now - self.last_fire > 2.0:
                    self.last_fire = now
                    fired = True
            return fired


    def scan(self, audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Run a recording through a fresh copy of the pipeline (the live state is left untouched).
        Returns (score per 80 ms chunk, the 16x96 embedding window after each chunk)."""
        with self._lock:
            saved = (self.raw, self.pending, self.mels, self.feats, self.n)
            try:
                self.raw = np.zeros(CHUNK + 480, np.float32)
                self.mels = np.ones((76, 32), np.float32)
                self.feats = self._noise_feats.copy()
                self.n = 0
                pcm = np.asarray(audio, np.float32) * 32767.0
                scores, wins = [], []
                for i in range(0, len(pcm) - CHUNK + 1, CHUNK):
                    scores.append(self._step(pcm[i:i + CHUNK]))
                    wins.append(self.feats[-16:].copy())
                return np.array(scores, np.float32), np.array(wins, np.float32).reshape(len(wins), 16, 96)
            finally:
                self.raw, self.pending, self.mels, self.feats, self.n = saved


def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, np.float32).reshape(-1)
    return v / (float(np.linalg.norm(v)) + 1e-9)


def chime(sr: int = 24000) -> np.ndarray:
    """A soft two-note 'I'm listening' chime."""
    out = []
    for f, d in ((880, 0.07), (1320, 0.11)):
        t = np.arange(int(sr * d)) / sr
        env = np.minimum(1, t / 0.01) * np.exp(-t * 18)
        out.append(0.18 * np.sin(2 * np.pi * f * t) * env)
    return np.concatenate(out).astype(np.float32)
