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
                 on_barge: Callable[[np.ndarray], bool] | None = None,
                 on_wake: Callable[[], None] | None = None,
                 on_clap: Callable[[], None] | None = None):
        self.settings = settings
        self.on_level = on_level
        self.on_utterance = on_utterance
        self.on_barge = on_barge
        self.on_wake = on_wake
        self.on_clap = on_clap
        self.wake = None           # WakeWord detector, used in "wake" mode
        self.clap = None           # ClapDetector (double clap = wake), used in "wake" and "handsfree" modes
        self.idle_mode = "off"     # mode to return to after push-to-talk: off | wake | handsfree
        self.in_speech = False     # hands-free: currently capturing an utterance
        self._ignore_blocks = 0    # drop this many blocks (our own chime right after the wake word)
        self.mode = "off"          # off | ptt | handsfree | wake
        self.speaking = False      # True while the assistant's voice is playing
        self.out_level = 0.0       # RMS of what the speaker is playing right now (echo estimate)
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._ptt_buf: list[np.ndarray] = []
        self._thread = None
        self._lock = threading.Lock()
        self._handsfree_wanted = False
        self._rec = None           # {"buf": [...], "n": samples wanted, "done": Event} while training records

    def capture(self, seconds: float) -> np.ndarray:
        """Record `seconds` of raw mic audio from the running stream (wake word / clap training).
        Wake word, clap and voice detection pause while it records so the samples can't trigger anything."""
        started = self._stream is None
        self._ensure_stream()
        rec = {"buf": [], "n": int(seconds * SR), "got": 0, "done": threading.Event()}
        self._rec = rec
        try:
            if not rec["done"].wait(seconds + 5.0):
                raise RuntimeError("the microphone stopped sending audio")
        finally:
            self._rec = None
            if started and self.idle_mode == "off" and self.mode == "off":
                self.close()
        audio = np.concatenate(rec["buf"]) if rec["buf"] else np.zeros(0, np.float32)
        return audio[:rec["n"]].astype(np.float32)

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
            self.mode = self.idle_mode
            buf, self._ptt_buf = self._ptt_buf, []
        return np.concatenate(buf) if buf else np.zeros(0, np.float32)

    def set_handsfree(self, on: bool):
        self.set_idle_mode("handsfree" if on else "off")

    def set_idle_mode(self, mode: str):
        """off | wake (listen only for the wake word) | handsfree (always listening)."""
        if mode == "wake" and self.wake is None:
            mode = "handsfree"
        self.idle_mode = mode
        self._handsfree_wanted = mode == "handsfree"
        if mode != "off":
            self._ensure_stream()
        if self.mode != "ptt":
            self.mode = mode

    def engage(self):
        """Wake word heard: listen for a request (hands-free) until the session puts us back to sleep."""
        self._ignore_blocks = 8     # ~240 ms
        self.mode = "handsfree"

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

            rec = self._rec
            if rec is not None:
                rec["buf"].append(block)
                rec["got"] += len(block)
                if rec["got"] >= rec["n"]:
                    rec["done"].set()
                continue
            if self.mode == "ptt":
                with self._lock:
                    self._ptt_buf.append(block)
                continue
            if self.clap is not None and self.mode in ("wake", "handsfree") and not self.speaking:
                try:
                    clapped = self.clap.process(block)
                except Exception:  # noqa: BLE001
                    clapped = False
                if clapped:
                    in_speech, speech, loud, silent_ms, cand, checking = False, [], 0, 0, [], None
                    preroll.clear()
                    self.engage()
                    cb = self.on_clap or self.on_wake
                    if cb:
                        threading.Thread(target=cb, daemon=True).start()
                    continue
            if self.wake is not None and (self.mode == "wake" or (self.mode == "handsfree" and self.speaking
                                                                  and self.idle_mode == "wake")):
                try:
                    fired = self.wake.process(block)
                except Exception:  # noqa: BLE001
                    fired = False
                if fired:
                    in_speech, speech, loud, silent_ms, cand, checking = False, [], 0, 0, [], None
                    preroll.clear()
                    self.engage()
                    if self.on_wake:
                        threading.Thread(target=self.on_wake, daemon=True).start()
                    continue
                if self.mode == "wake":
                    self.in_speech = False
                    continue
            if self._ignore_blocks > 0:
                self._ignore_blocks -= 1
                continue
            if self.mode != "handsfree":
                in_speech, speech, loud, silent_ms, cand, checking = False, [], 0, 0, [], None
                self.in_speech = False
                preroll.clear()
                continue
            self.in_speech = in_speech or checking is not None

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
