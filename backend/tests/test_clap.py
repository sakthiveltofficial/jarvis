import unittest

import numpy as np

from jarvis.clap import ClapDetector

SR = 16000
rng = np.random.RandomState(7)


def silence(s, level=0.002):
    return (rng.randn(int(SR * s)) * level).astype(np.float32)


def clap(amp=0.5, tau=0.012):
    n = int(SR * 0.08)
    t = np.arange(n) / SR
    env = np.minimum(1, t / 0.001) * np.exp(-t / tau)
    return (rng.randn(n) * amp * env).astype(np.float32)


def tone(s, f=180, amp=0.3):
    t = np.arange(int(SR * s)) / SR
    sig = sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 6))
    env = np.minimum(1, t / 0.03) * np.minimum(1, (s - t) / 0.03)
    return (amp * sig * env / 2).astype(np.float32)


def run(audio, sens=0.5, block=480):
    d = ClapDetector(sens)
    fired = 0
    for i in range(0, len(audio), block):
        fired += d.process(audio[i:i + block])
    return fired, d


def seq(*parts):
    return np.concatenate([p.astype(np.float32) for p in parts])


class ClapTest(unittest.TestCase):
    def test_double_clap_fires_once(self):
        a = seq(silence(1.0), clap(), silence(0.3), clap(0.45), silence(1.0))
        self.assertEqual(run(a)[0], 1)

    def test_gaps(self):
        for gap, want in ((0.15, 1), (0.6, 1), (0.0, 0), (1.3, 0)):  # onset gap = gap + 80 ms
            a = seq(silence(1.0), clap(), silence(gap), clap(), silence(1.0))
            self.assertEqual(run(a)[0], want, f"gap {gap}")

    def test_single_and_triple_rejected(self):
        self.assertEqual(run(seq(silence(1.0), clap(), silence(1.5)))[0], 0)
        a = seq(silence(1.0), clap(), silence(0.25), clap(), silence(0.25), clap(), silence(1.0))
        self.assertEqual(run(a)[0], 0)

    def test_applause_rejected(self):
        parts = [silence(1.0)]
        for _ in range(20):
            parts += [clap(rng.uniform(0.2, 0.6)), silence(rng.uniform(0.04, 0.2))]
        parts.append(silence(1.0))
        self.assertEqual(run(seq(*parts))[0], 0)

    def test_speech_like_and_music_rejected(self):
        words = [silence(0.8)]
        for _ in range(8):
            words += [tone(rng.uniform(0.15, 0.4), rng.uniform(120, 250)), silence(rng.uniform(0.05, 0.3))]
        words.append(silence(1.0))
        self.assertEqual(run(seq(*words))[0], 0)
        self.assertEqual(run(seq(silence(0.5), tone(3.0, 220, 0.5), silence(1.0)))[0], 0)

    def test_low_thud_rejected(self):
        # Door slam / desk bump: loud but low-frequency.
        n = int(SR * 0.15)
        t = np.arange(n) / SR
        thud = (0.8 * np.sin(2 * np.pi * 60 * t) * np.exp(-t / 0.03)).astype(np.float32)
        a = seq(silence(1.0), thud, silence(0.3), thud, silence(1.0))
        self.assertEqual(run(a)[0], 0)

    def test_noisy_room_and_quiet_claps(self):
        noisy = seq(silence(1.0, 0.02), clap(0.6), silence(0.3, 0.02), clap(0.6), silence(1.0, 0.02))
        self.assertEqual(run(noisy)[0], 1)
        faint = seq(silence(1.0), clap(0.03), silence(0.3), clap(0.03), silence(1.0))
        self.assertEqual(run(faint, sens=0.2)[0], 0)
        self.assertEqual(run(faint, sens=0.9)[0], 1)

    def test_unequal_claps_rejected(self):
        a = seq(silence(1.0), clap(0.8), silence(0.3), clap(0.02), silence(1.0))
        self.assertEqual(run(a, sens=1.0)[0], 0)

    def test_plosive_then_vowel_rejected(self):
        # "t...k" bursts followed by vowels: impulsive onsets, but the sound carries on.
        word = lambda: seq(clap(0.3, 0.004), tone(0.25, 150, 0.4))
        a = seq(silence(1.0), word(), silence(0.2), word(), silence(1.0))
        self.assertEqual(run(a)[0], 0)

    def test_reverberant_room(self):
        def wet(amp=0.5):
            dry = np.concatenate([clap(amp), np.zeros(int(SR * 0.3), np.float32)])
            t = np.arange(len(dry)) / SR
            tail = rng.randn(len(dry)) * amp * 0.08 * np.exp(-t / 0.07) * (t > 0.005)
            return (dry + tail).astype(np.float32)
        a = seq(silence(1.0), wet(), silence(0.05), wet(), silence(1.0))
        self.assertEqual(run(a)[0], 1)

    def test_many_takes(self):
        hits = 0
        for i in range(20):
            gap = 0.15 + 0.03 * i
            a = seq(silence(0.8), clap(rng.uniform(0.3, 0.7), rng.uniform(0.008, 0.02)), silence(gap),
                    clap(rng.uniform(0.3, 0.7), rng.uniform(0.008, 0.02)), silence(1.0))
            hits += run(a)[0]
        self.assertGreaterEqual(hits, 19)


if __name__ == "__main__":
    unittest.main()
