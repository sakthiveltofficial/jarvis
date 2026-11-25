"""Hand gesture control from the webcam (MediaPipe Hand Landmarker, runs on the CPU, no special hardware).

    open palm (hold)   wake me / stop me talking   (push-to-talk while your palm is up, if the mic is off)
    fist (hold)        stop what I'm doing
    pinch + move       spin the orb; a quick pinch shows or hides the Jarvis window
    swipe left/right   switch windows
    swipe up/down      maximise / minimise the active window

The camera loop runs on its own thread at ~15 fps (slower while no hand is in view). Frames never leave the
computer and nothing is recorded: each frame is turned into 21 hand points and dropped.
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


# --------------------------------------------------------------------------- camera loop
class GestureControl:
    """Owns the webcam and the hand tracker. Calls on_gesture(name) and on_hand(dict | None)."""

    FPS_ACTIVE = 15
    FPS_IDLE = 6          # no hand for a while: look less often
    IDLE_AFTER = 6.0

    def __init__(self, settings: dict, on_gesture, on_hand):
        self.settings = settings
        self.on_gesture = on_gesture
        self.on_hand = on_hand
        self.clf = GestureClassifier(self._cfg().get("sensitivity", 0.5))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.camera = int(self._cfg().get("camera", 0) or 0)
        self.state = "off"           # off | loading | downloading | running | error
        self.error = ""
        self.fps = 0.0
        self.last_gesture = ""
        self.last_gesture_at = 0.0
        self.hand_visible = False

    def _cfg(self) -> dict:
        return self.settings.get("gestures") or {}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.camera = int(self._cfg().get("camera", 0) or 0)
        self._thread = threading.Thread(target=self._run, name="gestures", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None
        self.state, self.fps = "off", 0.0

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "fps": round(self.fps, 1), "camera": self.camera,
                "model_ready": model_ready(), "last_gesture": self.last_gesture,
                "last_gesture_ago": round(time.time() - self.last_gesture_at, 1) if self.last_gesture_at else None,
                "hand_visible": self.hand_visible}

    def _open_camera(self):
        import cv2
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.camera, backend)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self.camera)
        if not cap.isOpened():
            raise RuntimeError(f"couldn't open camera {self.camera} (no webcam found, or another app is using it)")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        return cap

    def _run(self):
        try:
            self.state, self.error = "loading", ""
            if not model_ready():
                self.state = "downloading"
                ensure_model()
            import cv2
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision
            opts = vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(MODEL_PATH)),
                running_mode=vision.RunningMode.VIDEO, num_hands=1,
                min_hand_detection_confidence=0.6, min_hand_presence_confidence=0.5, min_tracking_confidence=0.5)
            tracker = vision.HandLandmarker.create_from_options(opts)
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            print(f"[jarvis] gestures unavailable: {self.error}")
            return
        cap = None
        t_start = time.monotonic()
        last_ts = -1
        last_hand_emit = 0.0
        frames: deque = deque(maxlen=30)
        try:
            while not self._stop.is_set():
                if cap is None:
                    try:
                        cap = self._open_camera()
                        self.state, self.error = "running", ""
                    except Exception as e:  # noqa: BLE001
                        self.state, self.error = "error", str(e)
                        self._stop.wait(5)
                        continue
                tick = time.monotonic()
                ok, frame = cap.read()
                if not ok or frame is None:
                    cap.release()
                    cap = None
                    self.state, self.error = "error", "the camera stopped sending pictures"
                    self._stop.wait(2)
                    continue
                frame = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
                ts = int((tick - t_start) * 1000)
                ts = max(ts, last_ts + 1)
                last_ts = ts
                res = tracker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=frame), ts)
                lm = [(p.x, p.y, p.z) for p in res.hand_landmarks[0]] if res.hand_landmarks else None
                self.clf.set_sensitivity(self._cfg().get("sensitivity", 0.5))
                fired = self.clf.update(lm, tick)
                was_visible, self.hand_visible = self.hand_visible, lm is not None
                if lm is not None and tick - last_hand_emit >= 1 / 15:
                    last_hand_emit = tick
                    self._safe(self.on_hand, dict(self.clf.hand or {}))
                elif was_visible and lm is None:
                    self._safe(self.on_hand, None)
                for g in fired:
                    if g != "lost":
                        self.last_gesture, self.last_gesture_at = g, time.time()
                    self._safe(self.on_gesture, g)
                frames.append(tick)
                if len(frames) > 1:
                    self.fps = (len(frames) - 1) / max(1e-3, frames[-1] - frames[0])
                idle = self.clf.last_seen is None or tick - self.clf.last_seen > self.IDLE_AFTER
                period = 1 / (self.FPS_IDLE if idle else self.FPS_ACTIVE)
                self._stop.wait(max(0.0, period - (time.monotonic() - tick)))
        finally:
            if cap is not None:
                cap.release()
            try:
                tracker.close()
            except Exception:  # noqa: BLE001
                pass
            self.hand_visible = False

    @staticmethod
    def _safe(fn, *a):
        try:
            fn(*a)
        except Exception as e:  # noqa: BLE001
            print(f"[jarvis] gesture handler failed: {e}")


__all__ = ["GestureClassifier", "GestureControl", "describe_pose", "window_keys", "ensure_model", "model_ready",
           "MODEL_PATH", "GESTURES", "Path"]
