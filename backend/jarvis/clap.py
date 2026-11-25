"""Double-clap wake ("clap twice and I'm listening"), alongside the "Hey Jarvis" wake word.

A clap is an impulse: near-instant attack, broadband (lots of high-frequency energy), gone within
~100 ms. Speech, music, a door slam or a cough fail at least one of those. A *double* clap is two
such impulses 0.12 to 0.8 s apart, of similar loudness, after a moment of relative quiet and NOT
followed by a third one (applause, drumming, typing bursts). All of it is plain numpy on 10 ms frames,
so it costs next to nothing next to the wake word model.

    det = ClapDetector(sensitivity=0.5)
    if det.process(block):   # float32 -1..1 at 16 kHz, any length
        ...                  # double clap heard
"""
from __future__ import annotations

import collections
import time

import numpy as np

SR = 16000
FRAME = 160                    # 10 ms


class ClapDetector:
    MIN_GAP = 0.12             # seconds between the two claps
    MAX_GAP = 0.80
    CONFIRM = 0.40             # silence needed after the second clap (rules out a third one)
    PRE_QUIET = 0.25           # relative quiet needed before the first clap
    MAX_LEN = 0.12             # a clap is louder than half its peak for at most this long

    def __init__(self, sensitivity: float = 0.5, sr: int = SR, clock=None):
        self.sr = sr
        self.clock = clock          # optional fake clock (tests); default = sample time
        self.set_sensitivity(sensitivity)
        self.reset()

    def set_sensitivity(self, s: float):
        s = min(1.0, max(0.0, float(s)))
        self.sensitivity = s
        # Higher sensitivity = quieter claps count. ratio is "how far above the room's noise floor".
        self.ratio = 40.0 - 32.0 * s            # energy ratio: 40x (~16 dB) .. 8x (~9 dB)
        self.abs_min = 10 ** (-2.6 - 1.8 * s)   # minimum frame energy (~-26 .. -44 dBFS)
        self.hf_min = 0.30 - 0.10 * s           # share of energy that is high-frequency

    def reset(self):
        self.pending = np.zeros(0, np.float32)
        self.t = 0.0                              # seconds of audio processed
        self.floor = 1e-5                         # running noise-floor energy
        self.hist = collections.deque(maxlen=int(self.PRE_QUIET * 100) + 1)   # recent frame energies
        self.clap = None                          # clap being measured: {t, peak, loud_frames, pre}
        self.claps: list[dict] = []               # accepted claps (most recent last)
        self.last_fire = -10.0
        self.loud_run = 0                         # consecutive loud frames that aren't a clap
        self.last_event = ""                      # why the last candidate was rejected/accepted (debugging)

    # ------------------------------------------------------------------ frame analysis
    @staticmethod
    def _hf_share(frame: np.ndarray) -> float:
        """Share of the frame's energy in its first difference (a cheap high-pass, rises above ~2.5 kHz)."""
        e = float(np.dot(frame, frame)) + 1e-12
        d = np.diff(frame)
        return float(np.dot(d, d)) / (2.0 * e)

    def _frame(self, f: np.ndarray) -> bool:
        e = float(np.dot(f, f)) / len(f)
        now = self.t
        self.t += len(f) / self.sr
        fired = False

        if self.clap is not None:                 # measuring a clap's tail
            c = self.clap
            c["frames"] += 1
            if e > c["peak"]:
                c["peak"] = e
            if e > c["peak"] * 0.25:              # louder than half the peak amplitude
                c["loud"] += 1
            if c["frames"] * 0.01 >= self.MAX_LEN + 0.03 or e < c["peak"] * 0.02:
                self._finish_clap()
        else:
            # Sustained sound (a vowel after a "t", music, a voice) between or after claps breaks the sequence.
            ref = self.claps[-1]["peak"] * 0.05 if self.claps else 0.0     # room echo of a clap is quieter
            if e > max(self.floor * self.ratio / 4, self.abs_min / 4, ref):
                self.loud_run += 1
                if self.loud_run >= 5 and self.claps:
                    self.last_event = "sustained sound"
                    self.claps = []
            else:
                self.loud_run = 0
            prev = self.hist[-1] if self.hist else self.floor
            onset = (e > self.floor * self.ratio and e > self.abs_min and e > prev * 4.0
                     and self._hf_share(f) >= self.hf_min)
            if onset:
                pre = max(list(self.hist)[:-1] or [self.floor])
                self.clap = {"t": now, "peak": e, "loud": 1, "frames": 1, "pre": pre}
                self.loud_run = 0

        # Room noise tracking: follow quiet frames quickly-ish, loud ones only very slowly.
        if self.clap is None:
            a = 0.05 if e < self.floor * 3 else 0.002
            self.floor = max(1e-7, (1 - a) * self.floor + a * e)
        self.hist.append(e)

        # A complete double clap is confirmed once nothing followed the second clap for CONFIRM seconds.
        if len(self.claps) >= 2 and self.clap is None and now - self.claps[-1]["t"] >= self.CONFIRM:
            a, b = self.claps[-2], self.claps[-1]
            gap = b["t"] - a["t"]
            similar = max(a["peak"], b["peak"]) / max(1e-12, min(a["peak"], b["peak"])) <= 16.0   # within 12 dB
            if len(self.claps) == 2 and self.MIN_GAP <= gap <= self.MAX_GAP and similar and now - self.last_fire > 1.5:
                fired = True
                self.last_fire = now
                self.last_event = f"double clap (gap {gap * 1000:.0f} ms)"
            else:
                self.last_event = f"rejected: {len(self.claps)} claps, gap {gap * 1000:.0f} ms, similar={similar}"
            self.claps = []
        elif len(self.claps) == 1 and self.clap is None and now - self.claps[-1]["t"] > self.MAX_GAP:
            self.last_event = "single clap"
            self.claps = []
        return fired

    def _finish_clap(self):
        c, self.clap = self.clap, None
        length = c["loud"] * 0.01
        if length > self.MAX_LEN:
            self.last_event = f"too long for a clap ({length * 1000:.0f} ms)"
            self.claps = []                               # a sustained sound breaks any sequence
            return
        if not self.claps and c["pre"] > c["peak"] * 0.06:
            self.last_event = "not quiet before the first clap"
            return
        if self.claps and c["t"] - self.claps[-1]["t"] > self.MAX_GAP:
            self.claps = []
        self.claps.append(c)

    # ------------------------------------------------------------------ public
    def process(self, block: np.ndarray) -> bool:
        """Feed float32 audio (-1..1) at 16 kHz. True once a double clap has been confirmed."""
        self.pending = np.concatenate([self.pending, np.asarray(block, np.float32)])
        fired = False
        while len(self.pending) >= FRAME:
            f, self.pending = self.pending[:FRAME], self.pending[FRAME:]
            fired = self._frame(f) or fired
        return fired


ACKS = ("Yes?", "At your service.", "I'm here.", "Listening.", "Go ahead.")


def ack_line(seed: float | None = None) -> str:
    """A short spoken acknowledgement after a clap (Iron Man style)."""
    i = int((seed if seed is not None else time.time()) * 7) % len(ACKS)
    return ACKS[i]
