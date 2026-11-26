"""The orb mirrors held hand gestures (gestures.py: mirror_pose, heart_pose, MirrorTracker)."""
import os
import tempfile
import unittest

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

from jarvis import config, gestures  # noqa: E402


def hand(fingers=(0, 0, 0, 0), thumb="in", dx=0.0):
    """Synthetic right-ish hand in image space (y down). fingers: 1 = extended, 0 = curled (index..pinky)."""
    wx, wy = 0.5 + dx, 0.8
    lm = [(0, 0, 0)] * 21
    lm[0] = (wx, wy, 0)
    xs = (-0.06, -0.02, 0.02, 0.06)
    for k, (mcp, ext) in enumerate(zip((5, 9, 13, 17), fingers)):
        x = wx + xs[k]
        lm[mcp] = (x, 0.62, 0)
        if ext:
            lm[mcp + 1], lm[mcp + 2], lm[mcp + 3] = (x, 0.54, 0), (x, 0.48, 0), (x, 0.43, 0)
        else:
            lm[mcp + 1], lm[mcp + 2], lm[mcp + 3] = (x + 0.01, 0.57, 0), (x + 0.01, 0.62, 0), (x, 0.66, 0)
    tx = wx - 0.09
    if thumb == "up":
        pts = [(tx + 0.02, 0.74), (tx + 0.01, 0.66), (tx, 0.56), (tx, 0.48)]
    elif thumb == "down":
        pts = [(tx + 0.02, 0.78), (tx, 0.84), (tx, 0.9), (tx, 0.96)]
    elif thumb == "out":
        pts = [(tx + 0.03, 0.76), (tx, 0.72), (tx - 0.05, 0.68), (tx - 0.1, 0.65)]
    else:
        pts = [(tx + 0.03, 0.76), (tx + 0.05, 0.7), (wx - 0.04, 0.66), (wx - 0.02, 0.64)]
    for i, (x, y) in zip((1, 2, 3, 4), pts):
        lm[i] = (x, y, 0)
    return lm


class MirrorPoseTest(unittest.TestCase):
    def test_single_hand_poses(self):
        self.assertEqual(gestures.mirror_pose(hand((1, 1, 0, 0))), "peace")
        self.assertEqual(gestures.mirror_pose(hand((1, 0, 0, 1))), "rock")
        self.assertEqual(gestures.mirror_pose(hand((1, 0, 0, 1), thumb="out")), "love")
        self.assertEqual(gestures.mirror_pose(hand((0, 0, 0, 1), thumb="out")), "call")
        self.assertEqual(gestures.mirror_pose(hand((1, 0, 0, 0))), "point")
        self.assertEqual(gestures.mirror_pose(hand((0, 0, 0, 0), thumb="up")), "thumbs_up")
        self.assertEqual(gestures.mirror_pose(hand((0, 0, 0, 0), thumb="down")), "thumbs_down")

    def test_ordinary_hands_are_not_mirrored(self):
        self.assertIsNone(gestures.mirror_pose(hand((1, 1, 1, 1), thumb="out")))    # open palm (a command)
        self.assertIsNone(gestures.mirror_pose(hand((0, 0, 0, 0))))                  # fist (a command)

    def test_heart(self):
        s = 0.18
        left = [(0.4, 0.8, 0)] * 21
        right = [(0.6, 0.8, 0)] * 21
        left, right = list(left), list(right)
        for h, side in ((left, -1), (right, 1)):
            h[gestures.MIDDLE_MCP] = (0.5 + side * 0.1, 0.8 - s, 0)
            h[gestures.INDEX_TIP] = (0.5 + side * 0.01, 0.45, 0)
            h[gestures.THUMB_TIP] = (0.5 + side * 0.01, 0.7, 0)
        self.assertTrue(gestures.heart_pose(left, right))
        far = list(right)
        far[gestures.INDEX_TIP] = (0.8, 0.45, 0)
        self.assertFalse(gestures.heart_pose(left, far))

    def test_tracker_holds_then_releases(self):
        tr = gestures.MirrorTracker()
        peace = hand((1, 1, 0, 0))
        self.assertIsNone(tr.update([peace], 0.0))
        ev = tr.update([peace], 0.5)
        self.assertEqual(ev["shape"], "peace")
        self.assertEqual(len(ev["hands"]), 1)
        self.assertEqual(len(ev["hands"][0]), 21)
        self.assertIsNone(tr.update([peace], 0.6))           # no repeats while held
        self.assertIsNone(tr.update([], 0.9))                # grace period
        self.assertEqual(tr.update([], 1.5), {"shape": None, "hands": []})

    def test_setting_default(self):
        self.assertTrue(config.DEFAULTS["gestures"]["mirror"])


if __name__ == "__main__":
    unittest.main()
