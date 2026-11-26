"""Double-clap wake ("clap twice and I'm listening"), alongside the "Hey Jarvis" wake word.

A clap is an impulse: near-instant attack, broadband (lots of high-frequency energy), gone within
~100 ms. Speech, music, a door slam or a cough fail at least one of those. A *double* clap is two
such impulses 0.12 to 0.8 s apart, of similar loudness, after a moment of relative quiet and NOT
followed by a third one (applause, drumming, typing bursts). All of it is plain numpy on 10 ms frames,
so it costs next to nothing next to the wake word model.

    det = ClapDetector(sensitivity=0.5)
    if det.process(block):   # float32 -1..1 at 16 kHz, any length
        ...                  # double clap heard

v0.11: "high-frequency" now means energy above 1 kHz (a real clap through a laptop mic peaks around
1-2.5 kHz, which the old first-difference measure scored as too dull, so many real claps never
counted), the onset may build up over two 10 ms frames, and `set_profile()` takes thresholds learned
from your own recorded claps (see voicetrain.py).
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

    def __init__(self, sensitivity: float = 0.5, sr: int = SR, clock=None, profile: dict | None = None):
        self.sr = sr
        self.clock = clock          # optional fake clock (tests); default = sample time
        self.profile = profile or None
        self.collect = False        # training: keep every clap-like candidate in self.candidates
        self.candidates: list[dict] = []
        self.set_sensitivity(sensitivity)
        self.reset()

    def set_profile(self, profile: dict | None):
        """Thresholds learned from your own claps (voicetrain.calibrate_claps); None = built-in defaults."""
        self.profile = profile or None
        self.set_sensitivity(self.sensitivity)

    def set_sensitivity(self, s: float):
        s = min(1.0, max(0.0, float(s)))
        self.sensitivity = s
        # Higher sensitivity = quieter claps count. ratio is "how far above the room's noise floor".
        self.ratio = 40.0 - 32.0 * s            # energy ratio: 40x (~16 dB) .. 8x (~9 dB)
        self.abs_min = 10 ** (-2.6 - 1.8 * s)   # minimum frame energy (~-26 .. -44 dBFS)
        self.hf_min = 0.40 - 0.16 * s           # share of energy above 1 kHz
        self.max_len, self.min_gap, self.max_gap = self.MAX_LEN, self.MIN_GAP, self.MAX_GAP
        p = self.profile
        if p:
            # Your trained values sit at sensitivity 0.5; the slider still nudges them (x2 .. x0.5).
            k = 2.0 ** ((0.5 - s) * 2.0)
            self.ratio = min(60.0, max(4.0, float(p.get("ratio", self.ratio)) * k))
            self.abs_min = min(1e-2, max(1e-7, float(p.get("abs_min", self.abs_min)) * k))
            self.hf_min = min(0.6, max(0.05, float(p.get("hf_min", self.hf_min)) - (s - 0.5) * 0.1))
            self.max_len = min(0.25, max(0.06, float(p.get("max_len", self.MAX_LEN))))
            self.min_gap = min(0.3, max(0.06, float(p.get("min_gap", self.MIN_GAP))))
            self.max_gap = min(1.4, max(self.min_gap + 0.2, float(p.get("max_gap", self.MAX_GAP))))

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
    def _hf_share(frame: np.ndarray, sr: int = SR) -> float:
        """Share of the frame's energy above 1 kHz (claps are broadband; voices, hums and thuds are not)."""
        p = np.abs(np.fft.rfft(frame)) ** 2
        k = max(1, int(round(1000 * len(frame) / sr)))
        tot = float(p[1:].sum()) + 1e-12            # ignore DC
        return float(p[k:].sum()) / tot

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
            if c["frames"] * 0.01 >= self.max_len + 0.03 or e < c["peak"] * 0.02:
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
            # The clap may start late in a frame, so compare against the quieter of the last two frames.
            h = list(self.hist)
            prev = min(h[-2:]) if h else self.floor
            if e > self.floor * self.ratio and e > self.abs_min and e > prev * 4.0:
                hf = self._hf_share(f, self.sr)
                if hf >= self.hf_min:
                    pre = max(h[:-2] or [self.floor])
                    self.clap = {"t": now, "peak": e, "loud": 1, "frames": 1, "pre": pre, "hf": hf,
                                 "floor": self.floor}
                    self.loud_run = 0
                else:
                    self.last_event = f"too dull for a clap (hf {hf:.2f} < {self.hf_min:.2f})"

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
            if len(self.claps) == 2 and self.min_gap <= gap <= self.max_gap and similar and now - self.last_fire > 1.5:
                fired = True
                self.last_fire = now
                self.last_event = f"double clap (gap {gap * 1000:.0f} ms)"
            else:
                self.last_event = f"rejected: {len(self.claps)} claps, gap {gap * 1000:.0f} ms, similar={similar}"
            self.claps = []
        elif len(self.claps) == 1 and self.clap is None and now - self.claps[-1]["t"] > self.max_gap:
            self.last_event = "single clap"
            self.claps = []
        return fired

    def _finish_clap(self):
        c, self.clap = self.clap, None
        length = c["loud"] * 0.01
        c["len"] = length
        if self.collect:
            self.candidates.append(dict(c))
        if length > self.max_len:
            self.last_event = f"too long for a clap ({length * 1000:.0f} ms)"
            self.claps = []                               # a sustained sound breaks any sequence
            return
        if not self.claps and c["pre"] > c["peak"] * 0.06:
            self.last_event = "not quiet before the first clap"
            return
        if self.claps and c["t"] - self.claps[-1]["t"] > self.max_gap:
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
