"""One shared webcam stream for everything that looks through the camera.

Hand gestures (gestures.py) and Presence (presence.py: who is at the desk, posture) both need the webcam. Opening it
twice fails on most Windows drivers, so a single `CameraHub` owns the device and hands each frame to the consumers
that want one. Each consumer asks for its own frame rate (gestures ~15 fps while a hand is up, Presence ~2 fps) and
the hub reads only as fast as the hungriest consumer needs. Frames stay in memory: nothing is saved or sent.

A consumer is `fn(frame_rgb, t) -> float | None`: it gets a mirrored RGB numpy frame and the monotonic time, and may
return a new frame rate for itself (None keeps the current one).
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Callable

Consumer = Callable[[object, float], "float | None"]


def camera_index(settings: dict) -> int:
    """The webcam everything shares (set under Settings > Desktop > Hand gestures > Camera)."""
    try:
        return int((settings.get("gestures") or {}).get("camera", 0) or 0)
    except (TypeError, ValueError):
        return 0


class CameraHub:
    def __init__(self, settings: dict, opener: Callable[[int], object] | None = None):
        self.settings = settings
        self.opener = opener or self._open_camera
        self.consumers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.camera = camera_index(settings)
        self.state = "off"            # off | running | error
        self.error = ""
        self.fps = 0.0
        self.size = (0, 0)

    # ------------------------------------------------------------------ consumers
    def add(self, name: str, fn: Consumer, fps: float = 5.0):
        with self._lock:
            self.consumers[name] = {"fn": fn, "fps": max(0.2, float(fps)), "next": 0.0}
        self._ensure()

    def remove(self, name: str):
        with self._lock:
            self.consumers.pop(name, None)
            empty = not self.consumers
        if empty:
            self.stop()

    def has(self, name: str) -> bool:
        return name in self.consumers

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _ensure(self):
        want = camera_index(self.settings)
        if self.running and want != self.camera:
            self.stop()
        if not self.running and self.consumers:
            self._stop.clear()
            self.camera = want
            self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
            self._thread.start()

    def sync(self):
        """Settings changed: reopen on another camera if needed."""
        if self.consumers:
            self._ensure()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None
        self.state, self.fps = "off", 0.0

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "fps": round(self.fps, 1), "camera": self.camera,
                "consumers": sorted(self.consumers)}

    # ------------------------------------------------------------------ loop
    def _open_camera(self, index: int):
        import cv2
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            raise RuntimeError(f"couldn't open camera {index} (no webcam found, or another app is using it)")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        return cap

    @staticmethod
    def _prepare(frame):
        import cv2
        return cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)

    def step(self, frame, tick: float) -> float:
        """Hand one frame to every consumer that is due. Returns seconds until the next one is due."""
        with self._lock:
            due = [(n, c) for n, c in self.consumers.items() if tick >= c["next"]]
        for name, c in due:
            try:
                new_fps = c["fn"](frame, tick)
            except Exception as e:  # noqa: BLE001
                print(f"[jarvis] camera consumer {name} failed: {e}")
                new_fps = None
            if new_fps:
                c["fps"] = max(0.2, float(new_fps))
            c["next"] = tick + 1.0 / c["fps"]
        with self._lock:
            nxt = min((c["next"] for c in self.consumers.values()), default=tick + 1.0)
        return max(0.0, nxt - tick)

    def _run(self):
        cap = None
        frames: deque = deque(maxlen=30)
        try:
            while not self._stop.is_set() and self.consumers:
                if cap is None:
                    try:
                        cap = self.opener(self.camera)
                        self.state, self.error = "running", ""
                    except Exception as e:  # noqa: BLE001
                        self.state, self.error = "error", str(e)
                        self._stop.wait(5)
                        continue
                tick = time.monotonic()
                ok, frame = cap.read()
                if not ok or frame is None:
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001
                        pass
                    cap = None
                    self.state, self.error = "error", "the camera stopped sending pictures"
                    self._stop.wait(2)
                    continue
                frame = self._prepare(frame)
                self.size = (frame.shape[1], frame.shape[0])
                frames.append(tick)
                if len(frames) > 1:
                    self.fps = (len(frames) - 1) / max(1e-3, frames[-1] - frames[0])
                wait = self.step(frame, tick)
                self._stop.wait(max(0.0, wait - (time.monotonic() - tick)))
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
            if self.state == "running":
                self.state = "off"
