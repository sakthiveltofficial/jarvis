"""Microphone capture (push-to-talk and hands-free with a simple energy VAD) and playback."""
from __future__ import annotations

import collections
import queue
import threading
import time
from typing import Callable

import numpy as np

SR = 16000
BLOCK = 480  # 30 ms


class Mic:
    def __init__(self, settings: dict, on_level: Callable[[float], None], on_utterance: Callable[[np.ndarray], None]):
        self.settings = settings
        self.on_level = on_level
        self.on_utterance = on_utterance
        self.mode = "off"          # off | ptt | handsfree
        self.muted = False         # True while the assistant is speaking (avoids hearing itself)
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._ptt_buf: list[np.ndarray] = []
        self._thread = None
        self._lock = threading.Lock()

    # stream lifecycle ---------------------------------------------------------
    def _ensure_stream(self):
        if self._stream is not None:
            return
        import sounddevice as sd
        dev = self.settings["audio"].get("input_device")

        def cb(indata, frames, t, status):
            block = indata[:, 0].copy()
            self._q.put(block)

        self._stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=BLOCK,
                                      device=dev, callback=cb)
        self._stream.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self):
        self.mode = "off"
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    # modes --------------------------------------------------------------------
    def ptt_start(self):
        self._ensure_stream()
        with self._lock:
            self._ptt_buf = []
            self.mode = "ptt"

    def ptt_stop(self) -> np.ndarray:
        with self._lock:
            self.mode = "handsfree" if self._handsfree_wanted else "off"
            buf, self._ptt_buf = self._ptt_buf, []
        return np.concatenate(buf) if buf else np.zeros(0, np.float32)

    _handsfree_wanted = False

    def set_handsfree(self, on: bool):
        self._handsfree_wanted = on
        if on:
            self._ensure_stream()
            self.mode = "handsfree"
        elif self.mode == "handsfree":
            self.mode = "off"

    # worker -------------------------------------------------------------------
    def _loop(self):
        a = self.settings["audio"]
        floor = 0.005
        preroll = collections.deque(maxlen=10)      # 300 ms before speech starts
        speech: list[np.ndarray] = []
        in_speech = False
        loud = 0
        silent_ms = 0
        last_level = 0.0
        while self._stream is not None:
            try:
                block = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
            now = time.time()
            if now - last_level > 0.05:
                self.on_level(min(1.0, rms * 12))
                last_level = now

            if self.mode == "ptt":
                with self._lock:
                    self._ptt_buf.append(block)
                continue
            if self.mode != "handsfree" or self.muted:
                in_speech, speech, loud, silent_ms = False, [], 0, 0
                preroll.clear()
                continue

            thr = max(floor * float(a.get("vad_threshold", 2.5)), 0.012)
            if not in_speech:
                floor = 0.95 * floor + 0.05 * rms if rms < floor * 3 else floor   # track ambient noise
                preroll.append(block)
                loud = loud + 1 if rms > thr else 0
                if loud >= 3:
                    in_speech, speech, silent_ms = True, list(preroll), 0
            else:
                speech.append(block)
                silent_ms = silent_ms + 30 if rms < thr else 0
                too_long = len(speech) * 30 > 30000
                if silent_ms >= int(a.get("silence_ms", 900)) or too_long:
                    audio = np.concatenate(speech)
                    in_speech, speech, loud = False, [], 0
                    preroll.clear()
                    if len(audio) > SR * 0.4:
                        threading.Thread(target=self.on_utterance, args=(audio,), daemon=True).start()


class Player:
    def __init__(self, settings: dict):
        self.settings = settings
        self._stop = threading.Event()

    def play(self, samples: np.ndarray, sr: int, on_level: Callable[[float], None] | None = None):
        import sounddevice as sd
        self._stop.clear()
        dev = self.settings["audio"].get("output_device")
        sd.play(samples, sr, device=dev)
        start = time.time()
        dur = len(samples) / sr
        while time.time() - start < dur + 0.1:
            if self._stop.is_set():
                sd.stop()
                return False
            if on_level:
                i = int((time.time() - start) * sr)
                seg = samples[i:i + sr // 20]
                on_level(min(1.0, float(np.sqrt(np.mean(seg ** 2))) * 6) if len(seg) else 0.0)
            time.sleep(0.05)
        sd.wait()
        if on_level:
            on_level(0.0)
        return True

    def stop(self):
        self._stop.set()


def list_devices() -> dict:
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        return {
            "inputs": [{"id": i, "name": d["name"]} for i, d in enumerate(devs) if d["max_input_channels"] > 0],
            "outputs": [{"id": i, "name": d["name"]} for i, d in enumerate(devs) if d["max_output_channels"] > 0],
        }
    except Exception as e:  # noqa: BLE001
        return {"inputs": [], "outputs": [], "error": str(e)}
