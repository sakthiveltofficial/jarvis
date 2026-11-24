"""Wake word ("Hey Jarvis") detection.

Runs the openWakeWord ONNX pipeline (Apache-2.0, https://github.com/dscripka/openWakeWord) directly on
onnxruntime, which Jarvis already ships for Kokoro, so no extra Python packages are needed:

    16 kHz audio -> melspectrogram.onnx -> embedding_model.onnx (76 mel frames -> 96-d)
                 -> hey_jarvis.onnx (last 16 embeddings -> score 0..1)

It costs a few percent of one CPU core, so it is fine on low-end machines. If the models can't be
loaded, the session falls back to spotting "Jarvis" in transcripts.
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
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        self.raw = np.zeros(CHUNK + 480, np.float32)
        self.pending = np.zeros(0, np.float32)
        self.mels = np.ones((76, 32), np.float32)
        noise = (np.random.RandomState(0).randint(-1000, 1000, 16000 * 2)).astype(np.float32)
        self.feats = self._embed_all(noise)
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
                now = time.time()
                if s >= self.threshold and now - self.last_fire > 2.0:
                    self.last_fire = now
                    fired = True
            return fired


def chime(sr: int = 24000) -> np.ndarray:
    """A soft two-note 'I'm listening' chime."""
    out = []
    for f, d in ((880, 0.07), (1320, 0.11)):
        t = np.arange(int(sr * d)) / sr
        env = np.minimum(1, t / 0.01) * np.exp(-t * 18)
        out.append(0.18 * np.sin(2 * np.pi * f * t) * env)
    return np.concatenate(out).astype(np.float32)
