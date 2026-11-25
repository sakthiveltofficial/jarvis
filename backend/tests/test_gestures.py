"""v0.8: hand gesture classifier (synthetic hand landmarks, no camera) and the screen HUD hooks."""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

from jarvis import config, gestures, hud, tools  # noqa: E402
from jarvis.gestures import GestureClassifier, describe_pose, window_keys  # noqa: E402


def hand(pose="palm", cx=0.5, cy=0.5, s=1.0):
    """21 landmarks for a right hand facing the camera, fingers up (image y grows downward)."""
    def P(dx, dy):
        return (cx + dx * s, cy + dy * s, 0.0)
    lm = [None] * 21
    lm[0] = P(0, 0.15)                                    # wrist
    mcps = {5: -0.06, 9: -0.02, 13: 0.02, 17: 0.06}
    for base, x in mcps.items():
        lm[base] = P(x, 0)
        if pose in ("fist",) or (pose == "point" and base != 5):
            lm[base + 1], lm[base + 2], lm[base + 3] = P(x, -0.04), P(x, -0.01), P(x, 0.02)   # curled
        else:
            lm[base + 1], lm[base + 2], lm[base + 3] = P(x, -0.06), P(x, -0.10), P(x, -0.14)  # extended
    if pose in ("fist", "point"):                         # thumb tucked across the fingers
        lm[1], lm[2], lm[3], lm[4] = P(-0.07, 0.12), P(-0.08, 0.08), P(-0.07, 0.06), P(-0.03, 0.05)
    else:                                                 # thumb out to the side
        lm[1], lm[2], lm[3], lm[4] = P(-0.07, 0.12), P(-0.11, 0.08), P(-0.14, 0.05), P(-0.17, 0.02)
    if pose == "pinch":                                   # thumb tip meets the index tip ("OK" sign)
        lm[3], lm[4] = P(-0.09, -0.08), P(-0.07, -0.13)
    return lm


def feed(clf, frames, t0=0.0, fps=15):
    out = []
    for i, f in enumerate(frames):
        out += clf.update(f, t0 + i / fps)
    return out


class PoseTests(unittest.TestCase):
    def test_poses(self):
        self.assertEqual(describe_pose(hand("palm"))["pose"], "palm")
        self.assertEqual(describe_pose(hand("fist"))["pose"], "fist")
        self.assertEqual(describe_pose(hand("point"))["pose"], "point")
        p = describe_pose(hand("pinch"))
        self.assertTrue(p["pinchable"])
        self.assertLess(p["pinch"], 0.2)

    def test_fist_is_not_a_pinch(self):
        p = describe_pose(hand("fist"))
        self.assertFalse(p["pinchable"])

    def test_scale_invariant(self):
        for s in (0.5, 1.0, 1.8):
            self.assertEqual(describe_pose(hand("palm", s=s))["pose"], "palm")
            self.assertEqual(describe_pose(hand("fist", s=s))["pose"], "fist")


class ClassifierTests(unittest.TestCase):
    def test_palm_hold_fires_once(self):
        clf = GestureClassifier()
        out = feed(clf, [hand("palm")] * 30)              # 2 s of a still palm
        self.assertEqual(out.count("palm"), 1)

    def test_short_palm_does_not_fire(self):
        clf = GestureClassifier()
        self.assertNotIn("palm", feed(clf, [hand("palm")] * 5))

    def test_fist_hold(self):
        clf = GestureClassifier()
        self.assertEqual(feed(clf, [hand("fist")] * 15).count("fist"), 1)

    def test_swipes(self):
        for name, dx, dy in (("swipe_right", 1, 0), ("swipe_left", -1, 0), ("swipe_down", 0, 1), ("swipe_up", 0, -1)):
            clf = GestureClassifier()
            frames = [hand("palm", cx=0.5 + dx * 0.05 * i, cy=0.5 + dy * 0.05 * i) for i in range(-3, 4)]
            out = feed(clf, frames)
            self.assertEqual(out, [name], name)
            self.assertNotIn("palm", feed(clf, [hand("palm", cx=0.5 + dx * 0.15, cy=0.5 + dy * 0.15)] * 3, t0=1))

    def test_slow_drift_is_not_a_swipe(self):
        clf = GestureClassifier()
        frames = [hand("palm", cx=0.3 + 0.004 * i) for i in range(60)]   # 4 s to move a quarter of the frame
        self.assertFalse([g for g in feed(clf, frames) if g.startswith("swipe")])

    def test_pinch_tap_and_drag(self):
        clf = GestureClassifier()
        out = feed(clf, [hand("palm")] * 2 + [hand("pinch")] * 3 + [hand("palm")] * 2)
        self.assertIn("pinch_tap", out)
        clf = GestureClassifier()
        frames = [hand("palm")] * 2 + [hand("pinch", cx=0.5 + 0.02 * i) for i in range(12)] + [hand("palm", cx=0.74)] * 2
        out, seen_drag = [], False
        for i, f in enumerate(frames):
            out += clf.update(f, i / 15)
            seen_drag = seen_drag or bool(clf.hand and clf.hand["pinch"])
        self.assertNotIn("pinch_tap", out)
        self.assertTrue(seen_drag)

    def test_lost(self):
        clf = GestureClassifier()
        feed(clf, [hand("palm")] * 3)
        out = feed(clf, [None] * 10, t0=0.2)
        self.assertEqual(out, ["lost"])
        self.assertIsNone(clf.hand)

    def test_window_keys(self):
        self.assertEqual(window_keys("swipe_right", "win"), ["alt", "esc"])
        self.assertEqual(window_keys("swipe_down", "mac"), ["command", "m"])
        self.assertIsNone(window_keys("palm", "win"))


class HudTests(unittest.TestCase):
    def test_defaults(self):
        self.assertTrue(config.DEFAULTS["hud"]["enabled"])
        self.assertFalse(config.DEFAULTS["gestures"]["enabled"])   # the camera stays off until you opt in

    def test_point_tool_listed_for_vision_models_only(self):
        self.assertIn("point", [s.name for s in tools.specs_for(True)])
        self.assertNotIn("point", [s.name for s in tools.specs_for(False)])
        self.assertIn("point", tools.GUI_TOOLS)

    def test_point_emits_hud_event_in_screen_pixels(self):
        rt = tools.ToolRuntime(config.load())
        events = []
        rt.on_hud = events.append
        rt._scale, rt._offset = 2.0, (1920, 0)
        out = rt.t_point(100, 50, label="Click Export", seconds=5)
        self.assertFalse(out.is_error)
        self.assertEqual(events[0]["kind"], "point")
        self.assertEqual((events[0]["x"], events[0]["y"]), (2120, 100))
        self.assertEqual(events[0]["label"], "Click Export")

    def test_typing_never_shows_the_text(self):
        rt = tools.ToolRuntime(config.load())
        events = []
        rt.on_hud = events.append
        fake = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"pyautogui": fake}), mock.patch("time.sleep"):
            rt.t_keyboard("type", text="hunter2")
        self.assertEqual(events, [{"type": "hud", "kind": "type", "chars": 7}])

    def test_hud_off_means_no_events(self):
        s = config.load()
        s["hud"]["enabled"] = False
        rt = tools.ToolRuntime(s)
        events = []
        rt.on_hud = events.append
        fake = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"pyautogui": fake}):
            out = rt.t_point(10, 10)
        self.assertEqual(events, [])
        self.assertIn("HUD is off", out.text)
        fake.moveTo.assert_called_once()

    def test_stats(self):
        st = hud.Stats()
        st.read()
        r = st.read()
        for k in ("cpu", "ram", "ram_total_gb", "time"):
            self.assertIn(k, r)

    def test_model_path_in_models_dir(self):
        self.assertTrue(str(gestures.MODEL_PATH).startswith(str(config.MODELS_DIR)))


if __name__ == "__main__":
    unittest.main()
