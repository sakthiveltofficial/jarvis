"""v0.11: real-world claps, clap + wake word training on your own samples."""
import os
import tempfile
import threading
import unittest

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

import numpy as np  # noqa: E402

from jarvis import voicetrain  # noqa: E402
from jarvis.audio import Mic  # noqa: E402
from jarvis.clap import ClapDetector  # noqa: E402

SR = 16000
rng = np.random.RandomState(11)


def silence(s, level=0.002):
    return (rng.randn(int(SR * s)) * level).astype(np.float32)


def band_clap(lo=400, hi=2200, amp=0.4, tau=0.015):
    """A clap as a laptop mic hears it: most energy between ~0.4 and 2 kHz, not white noise."""
    n = int(SR * 0.08)
    x = rng.randn(n)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / SR)
    X[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(X, n)
    x /= x.std()
    t = np.arange(n) / SR
    return (x * amp * np.minimum(1, t / 0.001) * np.exp(-t / tau)).astype(np.float32)


def double(gap=0.3, amp=0.4, lead=1.0, **kw):
    return np.concatenate([silence(lead), band_clap(amp=amp, **kw), silence(gap), band_clap(amp=amp * 0.9, **kw),
                           silence(1.0)])


def run(audio, det):
    fired = 0
    for i in range(0, len(audio), 480):
        fired += det.process(audio[i:i + 480])
    return fired


class RealClapTest(unittest.TestCase):
    def test_dull_laptop_claps_now_count(self):
        # Before v0.11 these scored ~0.1-0.2 "high frequency" and were silently ignored.
        for lo, hi in ((300, 1800), (500, 2500)):
            self.assertEqual(run(double(lo=lo, hi=hi), ClapDetector(0.5)), 1, f"{lo}-{hi} Hz")

    def test_clap_starting_late_in_a_frame(self):
        for off in (0, 40, 80, 120, 150):
            a = np.concatenate([silence(off / SR), double()])
            self.assertEqual(run(a, ClapDetector(0.5)), 1, f"offset {off}")


class ClapTrainingTest(unittest.TestCase):
    def test_take_check(self):
        r = voicetrain.check_clap_take(double())
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["default_fired"])
        self.assertFalse(voicetrain.check_clap_take(silence(3))["ok"])
        one = np.concatenate([silence(1), band_clap(), silence(2)])
        r = voicetrain.check_clap_take(one)
        self.assertFalse(r["ok"])
        self.assertIn("one clap", r["message"])

    def test_calibrate_catches_soft_claps_and_still_rejects_speech(self):
        # Soft, dull claps that the built-in sensitivity-0.5 detector misses.
        soft = lambda: double(amp=0.02, lo=250, hi=1400, gap=0.45)  # noqa: E731
        self.assertEqual(run(soft(), ClapDetector(0.5)), 0)
        p = voicetrain.calibrate_claps([soft() for _ in range(4)])
        self.assertEqual(p["takes"], 4)
        for _ in range(3):
            self.assertEqual(run(soft(), ClapDetector(0.5, profile=p)), 1)
        # A voice (harmonics under 1 kHz) still doesn't count.
        t = np.arange(int(SR * 0.3)) / SR
        vowel = sum(np.sin(2 * np.pi * 160 * k * t) / k for k in range(1, 6)).astype(np.float32) * 0.1
        talk = np.concatenate([silence(1), vowel, silence(0.2), vowel, silence(1)])
        self.assertEqual(run(talk, ClapDetector(0.5, profile=p)), 0)
        voicetrain.save_clap_profile(p)
        self.assertEqual(voicetrain.load_clap_profile()["takes"], 4)

    def test_calibrate_needs_two_good_takes(self):
        with self.assertRaises(ValueError):
            voicetrain.calibrate_claps([double(), silence(3)])


class FakeWake:
    """Stands in for WakeWord.scan: the embedding window is a fixed 'phrase' pattern while/after you speak."""
    threshold = 0.5
    name = "hey_jarvis"

    def __init__(self, phrase, score=0.12):
        self.phrase = phrase
        self.score = score
        self.profile = None

    def scan(self, audio):
        n = len(audio) // 1280
        rms = np.sqrt(np.mean(audio[:n * 1280].reshape(n, 1280) ** 2, axis=1))
        scores = np.zeros(n, np.float32)
        wins = np.zeros((n, 16, 96), np.float32) + 0.5
        loud = np.where(rms > 0.01)[0]
        for i in range(n):
            if len(loud) and loud[-1] <= i <= loud[-1] + 3:
                wins[i] = self.phrase + rng.randn(16, 96) * 0.15
                scores[i] = self.score
            else:
                wins[i] += rng.randn(16, 96) * 0.4
        return scores, wins

    def set_profile(self, p):
        self.profile = p


def speechy(s, start=0.4):
    a = silence(s)
    i, j = int(start * SR), int((start + 0.7) * SR)
    t = np.arange(j - i) / SR
    a[i:j] += (0.1 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
    return a


class WakeTrainingTest(unittest.TestCase):
    def test_wake_profile_round_trip_and_live_match(self):
        phrase = rng.randn(16, 96).astype(np.float32)
        ww = FakeWake(phrase)
        pos, scores = [], []
        for _ in range(5):
            r = voicetrain.check_wake_take(ww, speechy(2.5))
            self.assertTrue(r["ok"], r)
            self.assertIn("only scored you", r["message"])
            pos.append(r["windows"])
            scores.append(r["score"])
        self.assertFalse(voicetrain.check_wake_take(ww, silence(2.5))["ok"])
        other = FakeWake(rng.randn(16, 96).astype(np.float32))      # normal talking: a different pattern
        neg = voicetrain.check_negative_take(other, speechy(6.0, 1.0))
        p = voicetrain.calibrate_wake(pos, [neg["windows"]], scores, "hey_jarvis")
        self.assertEqual(p["quality"], "good")
        voicetrain.save_wake_profile(p)
        q = voicetrain.load_wake_profile("hey_jarvis")
        self.assertIsNotNone(q)
        self.assertIsNone(voicetrain.load_wake_profile("alexa"))

        from jarvis.wakeword import WakeWord
        w = WakeWord.__new__(WakeWord)          # no ONNX needed for the similarity maths
        w.set_profile(q)
        self.assertGreater(w.similarity(phrase + rng.randn(16, 96) * 0.15), q["sim_thr"])
        self.assertLess(w.similarity(0.5 + rng.randn(16, 96) * 0.4), q["sim_thr"])
        self.assertLess(w.similarity(other.phrase + rng.randn(16, 96) * 0.15), q["sim_thr"])

    def test_needs_three_takes(self):
        with self.assertRaises(ValueError):
            voicetrain.calibrate_wake([np.zeros((2, 16, 96))], [], [0.1], "hey_jarvis")


class CaptureTest(unittest.TestCase):
    def test_capture_takes_raw_audio_and_pauses_detection(self):
        mic = Mic({"audio": {}}, lambda lvl: None, lambda a: None)
        mic._stream = object()                        # pretend the stream is open; feed() supplies audio
        fired = []
        mic.clap = ClapDetector(0.5)
        mic.on_clap = lambda: fired.append(1)
        mic.mode = "wake"
        th = threading.Thread(target=mic._loop, daemon=True)
        th.start()
        a = double()
        out = {}
        cap = threading.Thread(target=lambda: out.setdefault("a", mic.capture(len(a) / SR)))
        cap.start()
        while mic._rec is None:
            pass
        for i in range(0, len(a), 480):
            mic.feed(a[i:i + 480])
        cap.join(5)
        self.assertEqual(len(out["a"]), len(a) // 480 * 480)
        self.assertEqual(fired, [])                   # the training clap didn't wake me
        mic._stream = None
        th.join(2)


if __name__ == "__main__":
    unittest.main()
