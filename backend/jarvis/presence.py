"""Presence: Jarvis knows when you're at the desk.

  * Greets you when you sit down (first time today, or after a while away)
  * Locks the PC when you walk away (no face for `lock_after_s` and no keyboard/mouse)
  * Notices when someone else sits down, or looks over your shoulder, and tells you (and your paired phone)
  * Watches your posture (head dropping, leaning in, tilting) for the health nudges in health.py

It reads the shared webcam stream (camera.py, the same one hand gestures use) about twice a second. Faces are found
with OpenCV's YuNet detector and recognised with SFace: your face becomes a list of 128 numbers ("embeddings") that is
stored only in ~/.jarvis/presence/owner.npz on this PC. No picture of you is ever saved, and nothing about faces is
sent to the AI model or anywhere else. If you turn on "photo of visitors", a photo of an unknown face that sits down
while you're away is kept in ~/.jarvis/presence/visitors for 30 days, so you can see who it was.

`PresenceTracker`, `face_metrics` and `posture_check` are pure logic so they can be tested without a camera.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config

DIR = config.HOME / "presence"
OWNER_FILE = DIR / "owner.npz"
PROFILE_FILE = DIR / "profile.json"
VISITORS = DIR / "visitors"
MODEL_DIR = config.MODELS_DIR / "faces"
MODELS = {
    "detector": ("face_detection_yunet_2023mar.onnx", 200_000,
                 ["https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/"
                  "face_detection_yunet_2023mar.onnx",
                  "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
                  "face_detection_yunet_2023mar.onnx"]),
    "recognizer": ("face_recognition_sface_2021dec.onnx", 30_000_000,
                   ["https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/"
                    "face_recognition_sface_2021dec.onnx",
                    "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
                    "face_recognition_sface_2021dec.onnx"]),
}
MATCH_THRESHOLD = 0.38        # SFace cosine similarity; OpenCV suggests 0.363 for "same person"


def model_path(kind: str) -> Path:
    return MODEL_DIR / MODELS[kind][0]


def models_ready() -> bool:
    return all(model_path(k).exists() and model_path(k).stat().st_size > MODELS[k][1] for k in MODELS)


def ensure_models():
    """Download the face detector (0.2 MB) and recogniser (37 MB) once."""
    import httpx
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for kind, (name, min_size, urls) in MODELS.items():
        dest = model_path(kind)
        if dest.exists() and dest.stat().st_size > min_size:
            continue
        err = None
        for url in urls:
            tmp = dest.with_suffix(".part")
            try:
                with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
                if tmp.stat().st_size < min_size:
                    raise RuntimeError(f"{name} download was too small")
                tmp.replace(dest)
                err = None
                break
            except Exception as e:  # noqa: BLE001
                err = e
                tmp.unlink(missing_ok=True)
        if err:
            raise RuntimeError(f"couldn't download the face model {name}: {err}")


# --------------------------------------------------------------------------- pure logic
def face_metrics(face: dict) -> dict:
    """Posture numbers from one face (landmarks in 0..1 of the frame): where the eyes are, how big the face is
    (eye distance, grows as you lean in), the head's roll and a pitch ratio (eyes-to-nose vs nose-to-mouth)."""
    (rx, ry), (lx, ly), (nx, ny), (mrx, mry), (mlx, mly) = face["landmarks"][:5]
    eye_y = (ry + ly) / 2
    eye_d = max(1e-4, math.hypot(lx - rx, ly - ry))
    roll = math.degrees(math.atan2(ly - ry, lx - rx))
    mouth_y = (mry + mly) / 2
    pitch = (ny - eye_y) / max(1e-4, mouth_y - eye_y)
    return {"y": eye_y, "size": eye_d, "roll": roll, "pitch": pitch, "x": (rx + lx) / 2}


def posture_check(m: dict, base: dict | None, strictness: float = 0.5) -> tuple[bool, str]:
    """(bad, reason) compared with your "sitting up straight" baseline. Strictness 0..1 (higher = stricter)."""
    if not base:
        return False, ""
    s = min(1.0, max(0.0, float(strictness)))
    size_ratio = m["size"] / max(1e-4, base["size"])
    drop = (m["y"] - base["y"]) / max(1e-4, base["size"])          # in "eye distances", so it works at any range
    if size_ratio > 1.32 - 0.14 * s:
        return True, "leaning in close to the screen"
    if drop > 1.1 - 0.5 * s and size_ratio > 0.8:
        return True, "slouching (your head has dropped)"
    if abs(m["roll"] - base.get("roll", 0.0)) > 16 - 6 * s:
        return True, "tilting your head to one side"
    if m["pitch"] - base.get("pitch", m["pitch"]) > 0.45 - 0.2 * s:
        return True, "craning your neck down"
    return False, ""


def baseline_from(samples: list[dict]) -> dict | None:
    if len(samples) < 3:
        return None
    out = {}
    for k in ("y", "size", "roll", "pitch", "x"):
        vals = sorted(s[k] for s in samples)
        out[k] = vals[len(vals) // 2]
    return out


class PresenceTracker:
    """Turns a stream of "who's in front of the camera" observations into events:

        arrive   {"away_s": seconds away or None on the first sighting}
        leave    {"lock": True when the PC should lock}
        stranger {"owner_present": bool}  someone who isn't you, seen steadily for a couple of seconds
    """

    STRANGER_SEEN = 4          # observations in a row (about 2 s) before a stranger counts
    STRANGER_REPEAT = 300      # don't alert again for the same visit within 5 minutes
    GONE_GRACE = 8.0           # face missing this long before we start the leave countdown

    def __init__(self):
        self.present = False
        self.last_owner = None        # time the owner (or any face, when not enrolled) was last seen
        self.left_at = None
        self.stranger_run = 0
        self.stranger_alerted = float("-inf")
        self.locked_for = None        # left_at of the absence we already locked for

    def update(self, t: float, labels: list[str], idle_s: float, cfg: dict, enrolled: bool) -> list[tuple]:
        out: list[tuple] = []
        owner_seen = "owner" in labels or (not enrolled and bool(labels))
        stranger_seen = enrolled and "stranger" in labels
        if owner_seen:
            if not self.present:
                away = (t - self.left_at) if self.left_at is not None else None
                self.present = True
                out.append(("arrive", {"away_s": away}))
            self.last_owner = t
        elif self.present and self.last_owner is not None:
            gone = t - self.last_owner
            lock_after = max(10.0, float(cfg.get("lock_after_s", 45)))
            if gone >= max(lock_after, self.GONE_GRACE) and idle_s >= min(lock_after, gone) * 0.8:
                self.present = False
                self.left_at = self.last_owner
                lock = bool(cfg.get("lock_on_leave", True)) and self.locked_for != self.left_at
                if lock:
                    self.locked_for = self.left_at
                out.append(("leave", {"lock": lock}))
        if stranger_seen:
            self.stranger_run += 1
            if self.stranger_run >= self.STRANGER_SEEN and t - self.stranger_alerted > self.STRANGER_REPEAT:
                self.stranger_alerted = t
                out.append(("stranger", {"owner_present": owner_seen}))
        else:
            self.stranger_run = 0
        return out


# --------------------------------------------------------------------------- OS actions
def lock_screen() -> bool:
    """Lock this computer (Windows, macOS, Linux)."""
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.user32.LockWorkStation())  # type: ignore[attr-defined]
        if sys.platform == "darwin":
            r = subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "q" using '
                                '{control down, command down}'], capture_output=True, timeout=5)
            return r.returncode == 0
        for argv in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"], ["gnome-screensaver-command", "-l"]):
            if shutil.which(argv[0]) and subprocess.run(argv, capture_output=True, timeout=5).returncode == 0:
                return True
    except Exception as e:  # noqa: BLE001
        print(f"[jarvis] couldn't lock the screen: {e}")
    return False


# --------------------------------------------------------------------------- the camera consumer
class FaceEngine:
    """YuNet face detection + SFace recognition on CPU."""

    DETECT_W = 320

    def __init__(self):
        import cv2
        self.cv2 = cv2
        self.det = cv2.FaceDetectorYN.create(str(model_path("detector")), "", (self.DETECT_W, 240), 0.75, 0.3, 50)
        self.rec = cv2.FaceRecognizerSF.create(str(model_path("recognizer")), "")
        self._size = (self.DETECT_W, 240)

    def detect(self, rgb) -> list[dict]:
        """Faces in an RGB frame, largest first: box + 5 landmarks in 0..1, plus the raw row for alignment."""
        import numpy as np
        cv2 = self.cv2
        h, w = rgb.shape[:2]
        scale = self.DETECT_W / w
        small = cv2.resize(rgb, (self.DETECT_W, max(1, round(h * scale))))
        bgr = cv2.cvtColor(small, cv2.COLOR_RGB2BGR)
        size = (bgr.shape[1], bgr.shape[0])
        if size != self._size:
            self.det.setInputSize(size)
            self._size = size
        _, faces = self.det.detect(bgr)
        out = []
        for row in (faces if faces is not None else []):
            full = np.array(row, dtype=np.float32)
            full[:14] /= scale                      # back to full-frame pixels for alignment
            x, y, bw, bh = full[:4]
            lms = [(float(full[4 + 2 * i]) / w, float(full[5 + 2 * i]) / h) for i in range(5)]
            out.append({"box": (float(x) / w, float(y) / h, float(bw) / w, float(bh) / h), "landmarks": lms,
                        "score": float(full[14]), "row": full, "area": float(bw * bh) / (w * h)})
        out.sort(key=lambda f: -f["area"])
        return out

    def embed(self, rgb, face: dict):
        import numpy as np
        bgr = self.cv2.cvtColor(rgb, self.cv2.COLOR_RGB2BGR)
        crop = self.rec.alignCrop(bgr, face["row"])
        feat = np.asarray(self.rec.feature(crop), dtype=np.float32).reshape(-1)
        return feat / max(1e-6, float(np.linalg.norm(feat)))


class Presence:
    """Owns the face engine, the owner profile and the tracker. Calls on_event(kind, data) for arrive / leave /
    stranger, and keeps the latest posture reading for health.py."""

    FPS = 2.0
    MIN_FACE = 0.006           # faces smaller than this share of the frame are too far away to be at the desk

    def __init__(self, settings: dict, hub, on_event, idle_fn=None):
        self.settings = settings
        self.hub = hub
        self.on_event = on_event
        self.idle_fn = idle_fn or (lambda: 0.0)
        self.tracker = PresenceTracker()
        self.engine: FaceEngine | None = None
        self.state = "off"            # off | downloading | loading | running | error
        self.error = ""
        self.owner = None             # np.ndarray (N, 128)
        self.profile: dict = {}
        self.enroll_job: dict | None = None
        self.last_seen: dict = {"labels": [], "at": 0.0, "similarity": None}
        self.posture: dict | None = None   # {"bad": bool, "reason": str, "at": t}
        self._lock = threading.Lock()
        self._loading = False
        self._load_profile()

    def cfg(self) -> dict:
        return self.settings.get("presence") or {}

    # ------------------------------------------------------------------ profile (stays on this PC)
    def _load_profile(self):
        try:
            if OWNER_FILE.exists():
                import numpy as np
                with np.load(OWNER_FILE) as z:
                    self.owner = z["embeddings"].astype("float32")
        except Exception as e:  # noqa: BLE001
            print(f"[jarvis] couldn't read the saved face profile: {e}")
            self.owner = None
        try:
            self.profile = json.loads(PROFILE_FILE.read_text("utf-8")) if PROFILE_FILE.exists() else {}
        except Exception:  # noqa: BLE001
            self.profile = {}

    def _save_profile(self):
        DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_FILE.write_text(json.dumps(self.profile, indent=2), "utf-8")
        for f in (PROFILE_FILE, OWNER_FILE):
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass

    @property
    def enrolled(self) -> bool:
        return self.owner is not None and len(self.owner) > 0

    def forget(self):
        """Delete your face (embeddings) and posture baseline from this PC."""
        self.owner = None
        self.profile = {}
        OWNER_FILE.unlink(missing_ok=True)
        PROFILE_FILE.unlink(missing_ok=True)

    def match(self, emb) -> float:
        if not self.enrolled:
            return 0.0
        import numpy as np
        return float(np.max(self.owner @ emb))

    # ------------------------------------------------------------------ lifecycle
    def wanted(self) -> bool:
        return bool(self.cfg().get("enabled")) or self.enroll_job is not None

    def sync(self):
        """Start or stop looking, to match the settings (call off the event loop)."""
        if self.wanted():
            if self.engine is None:
                if not self._loading:
                    self._loading = True
                    threading.Thread(target=self._load, name="presence-load", daemon=True).start()
                return
            self.hub.add("presence", self._frame, self.FPS)
            self.hub.sync()
            self.state = "running"
        else:
            self.hub.remove("presence")
            self.state = "off"
            self.posture = None

    def _load(self):
        try:
            if not models_ready():
                self.state = "downloading"
                ensure_models()
            self.state = "loading"
            self.engine = FaceEngine()
            self.error = ""
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            print(f"[jarvis] presence unavailable: {self.error}")
            if self.enroll_job:
                self.enroll_job.update(done=True, ok=False, message=self.error)
                self.enroll_job = None
            return
        finally:
            self._loading = False
        self.sync()

    def stop(self):
        self.hub.remove("presence")
        self.state = "off"

    def status(self) -> dict:
        state, error = self.state, self.error
        if state == "running" and self.hub.state == "error":
            state, error = "error", self.hub.error
        seen = self.last_seen
        ago = round(time.monotonic() - seen["at"], 1) if seen["at"] else None
        return {"state": state, "error": error, "enrolled": self.enrolled, "samples": int(len(self.owner)) if
                self.enrolled else 0, "enrolled_at": self.profile.get("enrolled_at"),
                "has_baseline": bool(self.profile.get("baseline")), "models_ready": models_ready(),
                "present": self.tracker.present, "seen": seen["labels"], "seen_ago": ago,
                "similarity": seen.get("similarity"), "posture": self.posture,
                "enroll": {k: v for k, v in (self._last_job or {}).items() if k not in ("embs", "metrics")}}

    _last_job: dict | None = None

    # ------------------------------------------------------------------ enrolment and posture calibration
    def start_enroll(self, seconds: float = 8.0, posture_only: bool = False) -> dict:
        """Look at the camera for a few seconds (turn your head a little) while I learn your face. Sit up straight:
        the same moment becomes your good-posture baseline."""
        with self._lock:
            self.enroll_job = {"until": time.monotonic() + seconds, "seconds": seconds, "embs": [], "metrics": [],
                               "posture_only": posture_only, "done": False, "ok": False, "message": "Look at the camera…",
                               "progress": 0.0}
            self._last_job = self.enroll_job
        self.sync()
        return {k: v for k, v in self.enroll_job.items() if k not in ("embs", "metrics")}

    def _enroll_frame(self, rgb, faces: list[dict], tick: float):
        job = self.enroll_job
        if job is None:
            return
        if faces:
            f = faces[0]
            job["metrics"].append(face_metrics(f))
            if not job["posture_only"] and len(job["embs"]) < 40:
                job["embs"].append(self.engine.embed(rgb, f))
            job["message"] = "Got you. Keep looking, turn your head a little…"
        else:
            job["message"] = "I can't see a face. Sit in front of the camera, in good light."
        job["progress"] = round(1 - max(0.0, job["until"] - tick) / job["seconds"], 2)
        if tick < job["until"]:
            return
        import numpy as np
        n = len(job["metrics"])
        if n < 4 or (not job["posture_only"] and len(job["embs"]) < 4):
            job.update(done=True, ok=False, message="I didn't get a clear look at you. Try again in better light.")
        else:
            if not job["posture_only"]:
                embs = np.stack(job["embs"]).astype("float32")
                DIR.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(OWNER_FILE, embeddings=embs)
                self.owner = embs
                self.profile["enrolled_at"] = dt.datetime.now().isoformat(timespec="seconds")
            self.profile["baseline"] = baseline_from(job["metrics"])
            self._save_profile()
            job.update(done=True, ok=True, progress=1.0, message=(
                "Posture baseline saved." if job["posture_only"] else f"Learned your face from {len(job['embs'])} looks."))
        self.enroll_job = None
        if not self.cfg().get("enabled"):
            threading.Thread(target=self.sync, daemon=True).start()

    # ------------------------------------------------------------------ per frame (~2 fps)
    def _frame(self, rgb, tick: float):
        eng = self.engine
        if eng is None:
            return None
        faces = [f for f in eng.detect(rgb) if f["area"] >= self.MIN_FACE][:3]
        if self.enroll_job is not None:
            self._enroll_frame(rgb, faces, tick)
            return None
        if not self.cfg().get("enabled"):
            return None
        thr = float(self.cfg().get("match_threshold", MATCH_THRESHOLD))
        labels, best_sim, owner_face = [], None, None
        for f in faces:
            if self.enrolled:
                sim = self.match(eng.embed(rgb, f))
                best_sim = sim if best_sim is None else max(best_sim, sim)
                if sim >= thr and owner_face is None:
                    labels.append("owner")
                    owner_face = f
                else:
                    labels.append("stranger")
            else:
                labels.append("face")
                owner_face = owner_face or f
        self.last_seen = {"labels": labels, "at": tick if faces else self.last_seen["at"],
                          "similarity": round(best_sim, 3) if best_sim is not None else None}
        base = self.profile.get("baseline")
        if owner_face is not None and base:
            bad, why = posture_check(face_metrics(owner_face), base,
                                     float((self.settings.get("health") or {}).get("posture_strictness", 0.5)))
            self.posture = {"bad": bad, "reason": why, "at": time.time()}
        elif owner_face is None:
            self.posture = None
        try:
            idle = float(self.idle_fn())
        except Exception:  # noqa: BLE001
            idle = 0.0
        for kind, data in self.tracker.update(tick, labels, idle, self.cfg(), self.enrolled):
            if kind == "stranger" and self.cfg().get("visitor_photo"):
                data["photo"] = self._save_visitor(rgb)
            try:
                self.on_event(kind, data)
            except Exception as e:  # noqa: BLE001
                print(f"[jarvis] presence handler failed: {e}")
        return None

    def _save_visitor(self, rgb) -> str:
        """Opt-in: keep a photo of an unknown face (30 days, this PC only)."""
        try:
            import cv2
            VISITORS.mkdir(parents=True, exist_ok=True)
            cutoff = time.time() - 30 * 86400
            for old in VISITORS.glob("*.jpg"):
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
            path = VISITORS / f"{dt.datetime.now():%Y%m%d-%H%M%S}.jpg"
            cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
            return str(path)
        except Exception as e:  # noqa: BLE001
            print(f"[jarvis] couldn't save the visitor photo: {e}")
            return ""


GREET_IDEAS = {
    "morning": "They just sat down at the computer for the first time today, in the morning. Greet them by name, "
               "warmly; if something is scheduled soon, mention it.",
    "first": "They just sat down at the computer for the first time today. Greet them by name, warmly.",
    "back": "They just came back to the desk after about {away}. A brief welcome back.",
}


def greet_idea(away_s: float | None, first_today: bool, local: dt.datetime) -> str:
    if first_today:
        return GREET_IDEAS["morning" if 4 <= local.hour < 12 else "first"]
    mins = (away_s or 0) / 60
    away = f"{round(mins)} minutes" if mins < 90 else f"{mins / 60:.1f} hours"
    return GREET_IDEAS["back"].format(away=away)
