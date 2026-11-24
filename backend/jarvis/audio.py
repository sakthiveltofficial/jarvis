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
    """Microphone with three modes: off, push-to-talk and hands-free.

    Hands-free uses an adaptive energy VAD. While the assistant is speaking the mic keeps
    listening ("barge-in"): sustained speech above an echo-aware threshold becomes a
    candidate, and `on_barge(audio)` decides (via a quick transcript check) whether it is a
    real interruption. Confirmed -> the clip keeps recording as a normal utterance.
    Rejected (noise, echo, "mm-hmm") -> ignored and the assistant keeps talking.
    """

    def __init__(self, settings: dict, on_level: Callable[[float], None],
                 on_utterance: Callable[[np.ndarray], None],
                 on_barge: Callable[[np.ndarray], bool] | None = None):
        self.settings = settings
        self.on_level = on_level
        self.on_utterance = on_utterance
        self.on_barge = on_barge
        self.mode = "off"          # off | ptt | handsfree
        self.speaking = False      # True while the assistant's voice is playing
        self.out_level = 0.0       # RMS of what the speaker is playing right now (echo estimate)
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._ptt_buf: list[np.ndarray] = []
        self._thread = None
        self._lock = threading.Lock()
        self._handsfree_wanted = False

    # stream lifecycle ---------------------------------------------------------
    def _ensure_stream(self):
        if self._stream is not None:
            return
        import sounddevice as sd
        dev = self.settings["audio"].get("input_device")

        def cb(indata, frames, t, status):
            self._q.put(indata[:, 0].copy())

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

    def set_handsfree(self, on: bool):
        self._handsfree_wanted = on
        if on:
            self._ensure_stream()
            self.mode = "handsfree"
        elif self.mode == "handsfree":
            self.mode = "off"

    # worker -------------------------------------------------------------------
    def feed(self, block: np.ndarray):
        """Push one 30 ms block (used by tests; the real stream uses the callback)."""
        self._q.put(block)

    def _loop(self):
        floor = 0.005
        preroll = collections.deque(maxlen=10)      # 300 ms before speech starts
        speech: list[np.ndarray] = []
        in_speech = False
        loud = 0
        silent_ms = 0
        last_level = 0.0
        # barge-in state
        cand: list[np.ndarray] = []
        cand_silent = 0
        checking = None            # {"done": Event, "ok": bool}
        cooldown_until = 0.0

        while self._stream is not None or not self._q.empty():
            try:
                block = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            a = self.settings["audio"]
            rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
            now = time.time()
            if now - last_level > 0.05:
                self.on_level(min(1.0, rms * 12))
                last_level = now

            if self.mode == "ptt":
                with self._lock:
                    self._ptt_buf.append(block)
                continue
            if self.mode != "handsfree":
                in_speech, speech, loud, silent_ms, cand, checking = False, [], 0, 0, [], None
                preroll.clear()
                continue

            thr = max(floor * float(a.get("vad_threshold", 2.5)), 0.012)

            # A barge-in check finished (the assistant may already have stopped talking).
            if checking is not None and checking["done"].is_set():
                if checking["ok"]:
                    in_speech, speech, silent_ms = True, cand + [block], 0
                else:
                    cooldown_until = now + 1.0
                cand, checking = [], None
                preroll.clear()
                continue

            # ---------------- assistant is talking: barge-in detection ----------------
            if self.speaking and not in_speech:
                if not a.get("barge_in", True) or self.on_barge is None or now < cooldown_until:
                    preroll.append(block)
                    continue
                # Echo-aware threshold: the speaker leaks into the mic, so demand more energy
                # than what we are currently playing.
                bthr = max(thr * 2.0, 0.02, self.out_level * float(a.get("echo_gain", 0.6)))
                if checking is None and not cand:
                    preroll.append(block)
                    loud = loud + 1 if rms > bthr else 0
                    if loud >= 6:                                   # ~180 ms of sustained energy
                        cand, cand_silent, loud = list(preroll), 0, 0
                    continue
                cand.append(block)
                cand_silent = cand_silent + 30 if rms < bthr else 0
                if checking is None:
                    if cand_silent >= 450:                          # a cough / bump, not speech
                        cand, cooldown_until = [], now + 0.3
                    elif len(cand) * 30 >= int(a.get("barge_check_ms", 900)):
                        checking = {"done": threading.Event(), "ok": False}
                        clip = np.concatenate(cand)

                        def check(c=checking, clip=clip):
                            try:
                                c["ok"] = bool(self.on_barge(clip))
                            finally:
                                c["done"].set()
                        threading.Thread(target=check, daemon=True).start()
                    continue
                continue   # still checking: keep buffering (result handled above)

            # ---------------- normal hands-free listening ----------------
            if checking is not None:          # check still running; keep the clip growing
                cand.append(block)
                continue
            if not in_speech:
                cand = []
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
        self.current_rms = 0.0

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
                rms = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
                self.current_rms = rms
                on_level(min(1.0, rms * 6))
            time.sleep(0.05)
        sd.wait()
        self.current_rms = 0.0
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
