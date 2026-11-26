"""Hand gesture control from the webcam (MediaPipe Hand Landmarker, runs on the CPU, no special hardware).

    open palm (hold)   wake me / stop me talking   (push-to-talk while your palm is up, if the mic is off)
    fist (hold)        stop what I'm doing
    pinch + move       spin the orb; a quick pinch shows or hides the Jarvis window
    swipe left/right   switch windows
    swipe up/down      maximise / minimise the active window
    mirror (v0.12)     hold up a heart (two hands), thumbs up/down, peace, rock, love, call me, OK or point, and the
                       orb's particles take the same shape, like a friend copying your gesture back

Frames come from the shared webcam stream (camera.py) at ~15 fps (slower while no hand is in view), the same stream
Presence uses. Frames never leave the computer and nothing is recorded: each frame becomes 21 hand points and is dropped.
`GestureClassifier` is pure logic over those points so it can be unit-tested without a camera.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import config

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/"
             "float16/1/hand_landmarker.task")
MODEL_DIR = config.MODELS_DIR / "gestures"
MODEL_PATH = MODEL_DIR / "hand_landmarker.task"

# landmark indices (MediaPipe hand model)
WRIST, THUMB_IP, THUMB_TIP = 0, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_TIP = 5, 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP = 9, 10, 12
RING_MCP, RING_PIP, RING_TIP = 13, 14, 16
PINKY_MCP, PINKY_PIP, PINKY_TIP = 17, 18, 20
FINGERS = ((INDEX_PIP, INDEX_TIP), (MIDDLE_PIP, MIDDLE_TIP), (RING_PIP, RING_TIP), (PINKY_PIP, PINKY_TIP))

GESTURES = ("palm", "fist", "pinch_tap", "swipe_left", "swipe_right", "swipe_up", "swipe_down")


def _d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def describe_pose(lm) -> dict:
    """21 (x, y[, z]) points in 0..1 image space (mirrored, so x grows to the user's right) -> pose summary.

    Distances are measured in "hand sizes" (wrist to the middle knuckle) so they work near and far from the camera.
    """
    scale = max(_d(lm[WRIST], lm[MIDDLE_MCP]), 1e-4)
    tip = [_d(lm[WRIST], lm[t]) / scale for _, t in FINGERS]
    pip = [_d(lm[WRIST], lm[p]) / scale for p, _ in FINGERS]
    extended = [tip[i] > pip[i] * 1.12 for i in range(4)]
    curled = [tip[i] < pip[i] * 1.02 and tip[i] < 1.15 for i in range(4)]   # tip folded back onto the palm
    thumb_out = _d(lm[THUMB_TIP], lm[PINKY_MCP]) > _d(lm[THUMB_IP], lm[PINKY_MCP]) * 1.05 \
        and _d(lm[THUMB_TIP], lm[INDEX_MCP]) > scale * 0.45
    pinch = _d(lm[THUMB_TIP], lm[INDEX_TIP]) / scale
    cx = sum(lm[i][0] for i in (WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)) / 5
    cy = sum(lm[i][1] for i in (WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)) / 5
    n_ext = sum(extended)
    fist = all(curled)
    if fist:
        pose = "fist"
    elif n_ext == 4 and thumb_out:
        pose = "palm"
    elif extended[0] and n_ext == 1:
        pose = "point"
    else:
        pose = "other"
    # a pinch needs the index tip out in front of the palm (in a fist the thumb rests near it too)
    pinchable = not fist and tip[0] > 1.1
    return {"pose": pose, "pinch": pinch, "pinchable": pinchable, "open": n_ext, "x": cx, "y": cy, "size": scale}


class GestureClassifier:
    """Turns a stream of hand poses into discrete gestures, with holds, hysteresis and cool-downs."""

    PALM_HOLD = 0.55       # seconds an open palm must stay up (and fairly still)
    FIST_HOLD = 0.45
    STILL = 0.07           # how far the hand may drift during a hold (fraction of the frame)
    SWIPE_WINDOW = 0.5     # a swipe must happen within this long
    TAP_MAX = 0.4          # a pinch shorter than this (and without moving) is a tap
    LOST_AFTER = 0.35      # no hand for this long = hand gone
    COOLDOWN = 0.9         # after any gesture, ignore new ones this long

    def __init__(self, sensitivity: float = 0.5):
        self.set_sensitivity(sensitivity)
        self.reset()

    def set_sensitivity(self, s: float):
        s = min(1.0, max(0.0, float(s)))
        self.sens = s
        self.swipe_dist = 0.30 - 0.14 * s          # 0.30 .. 0.16 of the frame
        self.pinch_on = 0.22 + 0.14 * s            # thumb-index gap / hand size
        self.pinch_off = self.pinch_on + 0.16

    def reset(self):
        self.pose = None
        self.pose_since = 0.0
        self.pose_origin = (0.0, 0.0)
        self.fired_pose = False
        self.track: deque = deque()
        self.pinching = False
        self.pinch_t = 0.0
        self.pinch_origin = (0.0, 0.0)
        self.pinch_moved = 0.0
        self.last_seen = None
        self.cool_until = 0.0
        self.hand: dict | None = None

    def update(self, lm, t: float) -> list[str]:
        """Feed one frame's landmarks (or None when no hand is seen). Returns the gestures that just fired."""
        out: list[str] = []
        if lm is None:
            if self.last_seen is not None and t - self.last_seen > self.LOST_AFTER:
                self.reset()
                out.append("lost")
            return out
        self.last_seen = t
        p = describe_pose(lm)
        x, y = p["x"], p["y"]

        # pinch (with hysteresis): drag while held, a quick one is a tap
        if not self.pinching and p["pinchable"] and p["pinch"] < self.pinch_on:
            self.pinching, self.pinch_t, self.pinch_origin, self.pinch_moved = True, t, (x, y), 0.0
        elif self.pinching:
            self.pinch_moved = max(self.pinch_moved, math.hypot(x - self.pinch_origin[0], y - self.pinch_origin[1]))
            if p["pinch"] > self.pinch_off or not p["pinchable"]:
                self.pinching = False
                if p["pinchable"] and t - self.pinch_t < self.TAP_MAX and self.pinch_moved < 0.05 and t >= self.cool_until:
                    out.append("pinch_tap")
                    self.cool_until = t + self.COOLDOWN * 0.6
        pose = "pinch" if self.pinching else p["pose"]

        # swipes: an open-ish hand travelling fast in one direction
        self.track.append((t, x, y))
        while self.track and t - self.track[0][0] > self.SWIPE_WINDOW:
            self.track.popleft()
        if p["open"] >= 3 and not self.pinching and t >= self.cool_until and len(self.track) >= 3:
            t0, x0, y0 = self.track[0]
            dx, dy = x - x0, y - y0
            if abs(dx) > self.swipe_dist and abs(dx) > 1.6 * abs(dy):
                out.append("swipe_right" if dx > 0 else "swipe_left")
            elif abs(dy) > self.swipe_dist * 0.85 and abs(dy) > 1.6 * abs(dx):
                out.append("swipe_down" if dy > 0 else "swipe_up")
            if out and out[-1].startswith("swipe"):
                self.track.clear()
                self.cool_until = t + self.COOLDOWN
                self.fired_pose = True               # a swipe isn't also a palm
        # held poses: palm and fist
        if pose != self.pose:
            self.pose, self.pose_since, self.pose_origin, self.fired_pose = pose, t, (x, y), False
        elif not self.fired_pose and t >= self.cool_until and pose in ("palm", "fist"):
            drift = math.hypot(x - self.pose_origin[0], y - self.pose_origin[1])
            if drift > self.STILL:                   # moving: restart the hold from here
                self.pose_since, self.pose_origin = t, (x, y)
            elif t - self.pose_since >= (self.PALM_HOLD if pose == "palm" else self.FIST_HOLD):
                out.append(pose)
                self.fired_pose = True
                self.cool_until = t + self.COOLDOWN
        self.hand = {"x": round(x, 4), "y": round(y, 4), "pose": pose, "pinch": self.pinching,
                     "size": round(p["size"], 4)}
        return out


# --------------------------------------------------------------------------- window actions
def window_keys(gesture: str, platform: str | None = None) -> list[str] | None:
    """Keyboard shortcut for a window gesture on this OS (None = no window action)."""
    plat = platform or ("win" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux")
    table = {
        "win": {"swipe_left": ["alt", "shift", "esc"], "swipe_right": ["alt", "esc"],
                "swipe_up": ["winleft", "up"], "swipe_down": ["winleft", "down"]},
        "mac": {"swipe_left": ["command", "shift", "tab"], "swipe_right": ["command", "tab"],
                "swipe_up": ["ctrl", "command", "f"], "swipe_down": ["command", "m"]},
        "linux": {"swipe_left": ["alt", "shift", "tab"], "swipe_right": ["alt", "tab"],
                  "swipe_up": ["winleft", "up"], "swipe_down": ["winleft", "down"]},
    }
    return table.get(plat, {}).get(gesture)


def model_ready() -> bool:
    return MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 1_000_000


def ensure_model() -> Path:
    """Download the hand landmark model (~7.5 MB) once."""
    if model_ready():
        return MODEL_PATH
    import httpx
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_PATH.with_suffix(".part")
    with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    tmp.replace(MODEL_PATH)
    return MODEL_PATH


# --------------------------------------------------------------------------- mirror: the orb copies your gesture (v0.12)
MIRROR_POSES = ("heart", "thumbs_up", "thumbs_down", "peace", "rock", "love", "call", "ok", "point")


def _fingers(lm) -> tuple[list[bool], list[bool], float]:
    scale = max(_d(lm[WRIST], lm[MIDDLE_MCP]), 1e-4)
    tip = [_d(lm[WRIST], lm[t]) / scale for _, t in FINGERS]
    pip = [_d(lm[WRIST], lm[p]) / scale for p, _ in FINGERS]
    ext = [tip[i] > pip[i] * 1.12 for i in range(4)]
    curled = [tip[i] < pip[i] * 1.05 for i in range(4)]
    return ext, curled, scale


def _thumb_out(lm, scale: float) -> bool:
    return _d(lm[THUMB_TIP], lm[INDEX_MCP]) > scale * 0.55 and \
        _d(lm[THUMB_TIP], lm[PINKY_MCP]) > _d(lm[THUMB_IP], lm[PINKY_MCP]) * 1.05


def mirror_pose(lm) -> str | None:
    """One hand's landmarks (image space, mirrored, y down) -> a gesture the orb can mirror, or None."""
    ext, curled, scale = _fingers(lm)
    thumb = _thumb_out(lm, scale)
    i, m, rg, pk = ext
    if all(curled) and thumb:
        dy = (lm[THUMB_TIP][1] - lm[WRIST][1]) / scale
        if dy < -0.6 and lm[THUMB_TIP][1] < lm[INDEX_MCP][1]:
            return "thumbs_up"
        if dy > 0.3 and lm[THUMB_TIP][1] > lm[INDEX_MCP][1]:
            return "thumbs_down"
        return None
    if m and rg and pk and _d(lm[THUMB_TIP], lm[INDEX_TIP]) / scale < 0.35:
        return "ok"
    if i and m and not rg and not pk and curled[2] and curled[3]:
        return "peace"
    if i and pk and not m and not rg:
        return "love" if thumb else "rock"
    if pk and thumb and not i and not m and not rg and curled[0] and curled[1]:
        return "call"
    if i and not m and not rg and not pk and curled[1] and curled[2] and curled[3] and not thumb:
        return "point"
    return None


def heart_pose(a, b) -> bool:
    """Two hands making a heart: index tips touching at the top, thumb tips touching at the bottom."""
    sa = max(_d(a[WRIST], a[MIDDLE_MCP]), 1e-4)
    sb = max(_d(b[WRIST], b[MIDDLE_MCP]), 1e-4)
    s = (sa + sb) / 2
    idx = _d(a[INDEX_TIP], b[INDEX_TIP]) / s
    thb = _d(a[THUMB_TIP], b[THUMB_TIP]) / s
    top = (a[INDEX_TIP][1] + b[INDEX_TIP][1]) / 2
    bottom = (a[THUMB_TIP][1] + b[THUMB_TIP][1]) / 2
    apart = abs(a[WRIST][0] - b[WRIST][0]) / s
    return idx < 0.7 and thb < 0.7 and (bottom - top) / s > 0.45 and apart > 0.6


class MirrorTracker:
    """Decides when the orb should copy a gesture: held steady for HOLD seconds, released after GRACE without it."""
    HOLD = 0.45
    GRACE = 0.8

    def __init__(self):
        self.cand, self.cand_since = None, 0.0
        self.shown, self.last_seen = None, 0.0

    def update(self, hands: list, t: float) -> dict | None:
        """hands: landmark lists. Returns an event dict when what the orb shows changes, else None."""
        pose = None
        if len(hands) >= 2 and heart_pose(hands[0], hands[1]):
            pose = "heart"
        else:
            poses = [mirror_pose(h) for h in hands]
            pose = next((p for p in poses if p), None)
            if pose:
                hands = [h for h, p in zip(hands, poses) if p == pose]
        if pose != self.cand:
            self.cand, self.cand_since = pose, t
        if pose:
            self.last_seen = t
        if pose and t - self.cand_since >= self.HOLD and pose != self.shown:
            self.shown = pose
            return {"shape": pose, "hands": [[[round(p[0], 3), round(p[1], 3), round(p[2] if len(p) > 2 else 0.0, 3)]
                                              for p in h] for h in hands]}
        if self.shown and not pose and t - self.last_seen >= self.GRACE:
            self.shown = None
            return {"shape": None, "hands": []}
        return None


# --------------------------------------------------------------------------- camera consumer
class GestureControl:
    """The hand tracker. Reads frames from the shared CameraHub (camera.py) and calls on_gesture(name) and
    on_hand(dict | None). Presence (face at the desk, posture) uses the same camera stream at the same time."""

    FPS_ACTIVE = 15
    FPS_IDLE = 6          # no hand for a while: look less often
    IDLE_AFTER = 6.0

    def __init__(self, settings: dict, on_gesture, on_hand, hub=None, on_mirror=None):
        from .camera import CameraHub
        self.settings = settings
        self.on_gesture = on_gesture
        self.on_hand = on_hand
        self.on_mirror = on_mirror       # callable({shape, hands}): the orb copies a held gesture
        self.mirror = MirrorTracker()
        self.hub = hub or CameraHub(settings)
        self.clf = GestureClassifier(self._cfg().get("sensitivity", 0.5))
        self._tracker = None
        self._loader: threading.Thread | None = None
        self._wanted = False
        self.state = "off"           # off | loading | downloading | running | error
        self.error = ""
        self.last_gesture = ""
        self.last_gesture_at = 0.0
        self.hand_visible = False
        self._t0 = time.monotonic()
        self._last_ts = -1
        self._last_hand_emit = 0.0
        self._frames: deque = deque(maxlen=30)
        self.fps = 0.0

    def _cfg(self) -> dict:
        return self.settings.get("gestures") or {}

    @property
    def camera(self) -> int:
        return self.hub.camera

    @property
    def running(self) -> bool:
        return self._wanted

    def start(self):
        if self._wanted:
            self.hub.sync()
            return
        self._wanted = True
        if self._tracker is not None:
            self.hub.add("gestures", self._frame, self.FPS_ACTIVE)
            self.state = "running"
            return
        self._loader = threading.Thread(target=self._load, name="gestures-load", daemon=True)
        self._loader.start()

    def _load(self):
        try:
            self.state, self.error = "loading", ""
            if not model_ready():
                self.state = "downloading"
                ensure_model()
            from mediapipe.tasks.python import BaseOptions, vision
            opts = vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(MODEL_PATH)),
                running_mode=vision.RunningMode.VIDEO, num_hands=2,   # two hands: a heart
                min_hand_detection_confidence=0.6, min_hand_presence_confidence=0.5, min_tracking_confidence=0.5)
            self._tracker = vision.HandLandmarker.create_from_options(opts)
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            self._wanted = False
            print(f"[jarvis] gestures unavailable: {self.error}")
            return
        if self._wanted:
            self.hub.add("gestures", self._frame, self.FPS_ACTIVE)
            self.state = "running"

    def stop(self):
        self._wanted = False
        self.hub.remove("gestures")
        tr, self._tracker = self._tracker, None
        if tr is not None:
            try:
                tr.close()
            except Exception:  # noqa: BLE001
                pass
        self.state, self.fps, self.hand_visible = "off", 0.0, False
        self.clf.reset()

    def status(self) -> dict:
        state, error = self.state, self.error
        if state == "running" and self.hub.state == "error":
            state, error = "error", self.hub.error
        return {"state": state, "error": error, "fps": round(self.fps, 1), "camera": self.camera,
                "model_ready": model_ready(), "last_gesture": self.last_gesture,
                "last_gesture_ago": round(time.time() - self.last_gesture_at, 1) if self.last_gesture_at else None,
                "hand_visible": self.hand_visible}

    def _frame(self, frame, tick: float) -> float | None:
        tracker = self._tracker
        if tracker is None:
            return None
        import mediapipe as mp
        ts = max(int((tick - self._t0) * 1000), self._last_ts + 1)
        self._last_ts = ts
        res = tracker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=frame), ts)
        hands = [[(p.x, p.y, p.z) for p in h] for h in (res.hand_landmarks or [])]
        lm = hands[0] if hands else None
        if self.on_mirror is not None and self._cfg().get("mirror", True):
            ev = self.mirror.update(hands, tick)
            if ev is not None:
                self._safe(self.on_mirror, ev)
        self.clf.set_sensitivity(self._cfg().get("sensitivity", 0.5))
        fired = self.clf.update(lm, tick)
        was_visible, self.hand_visible = self.hand_visible, lm is not None
        if lm is not None and tick - self._last_hand_emit >= 1 / 15:
            self._last_hand_emit = tick
            self._safe(self.on_hand, dict(self.clf.hand or {}))
        elif was_visible and lm is None:
            self._safe(self.on_hand, None)
        for g in fired:
            if g != "lost":
                self.last_gesture, self.last_gesture_at = g, time.time()
            self._safe(self.on_gesture, g)
        self._frames.append(tick)
        if len(self._frames) > 1:
            self.fps = (len(self._frames) - 1) / max(1e-3, self._frames[-1] - self._frames[0])
        idle = self.clf.last_seen is None or tick - self.clf.last_seen > self.IDLE_AFTER
        return self.FPS_IDLE if idle else self.FPS_ACTIVE

    @staticmethod
    def _safe(fn, *a):
        try:
            fn(*a)
        except Exception as e:  # noqa: BLE001
            print(f"[jarvis] gesture handler failed: {e}")


__all__ = ["GestureClassifier", "GestureControl", "MirrorTracker", "mirror_pose", "heart_pose", "describe_pose", "window_keys", "ensure_model", "model_ready",
           "MODEL_PATH", "GESTURES", "Path"]
