"""Screen awareness: glance at what the user is doing and offer help when there's a clear problem.

Off by default. When on, every `interval` seconds (while Jarvis is idle) it reads the active window
title and, if the model can see images, takes a small screenshot. Only when something changed does
it ask the model, in one cheap call, whether there's a specific, obvious problem worth offering help
with (an error dialog, a failing build, a crash). Most glances end in "NONE". Offers are rate-limited
and never repeated for the same thing.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
import threading
import time

from . import llm
from .tools import active_window_title

SYSTEM = """You are glancing at a user's screen on behalf of their personal assistant. Decide if there is a
clear, specific problem right now that the assistant could solve for them: an error message or dialog,
a failed build or test run, a crash, a stuck installer, a broken page. Ordinary work (reading, writing,
browsing, videos, games, chats) is NOT a problem. Never comment on private content.
If there is a clear problem, reply with one short, friendly spoken offer (under 20 words) that names it,
like "I see the build failed with a missing module error. Want me to fix it?". Otherwise reply exactly NONE."""

ERRORISH = re.compile(r"error|exception|failed|failure|crash|not responding|traceback|denied|problem|warning", re.I)


class Awareness:
    def __init__(self, session):
        self.s = session
        self.last_title = None
        self.last_hash = None
        self.last_offer_at = 0.0
        self.offered: set[str] = set()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="awareness").start()

    def cfg(self) -> dict:
        return self.s.settings.get("awareness", {})

    def _loop(self):
        while not self._stop.wait(5):
            c = self.cfg()
            if not c.get("enabled"):
                continue
            interval = max(20, int(c.get("interval_s", 60)))
            if time.time() - getattr(self, "_last_glance", 0) < interval:
                continue
            self._last_glance = time.time()
            try:
                self.glance()
            except Exception as e:  # noqa: BLE001
                print(f"[jarvis] awareness glance failed: {e}")

    def busy(self) -> bool:
        s = self.s
        return (s.state not in ("idle", "listening") or bool(s.task and not s.task.done())
                or s.mic.speaking or bool(s.pending))

    def glance(self) -> str | None:
        if self.busy() or time.time() - self.last_offer_at < 60 * int(self.cfg().get("cooldown_min", 10)):
            return None
        title = active_window_title()
        vision = bool(self.s.settings["llm"].get("vision"))
        if title and title.lower().startswith(self.s.settings.get("assistant_name", "Jarvis").lower()):
            return None                                   # looking at Jarvis itself
        img = None
        if vision:
            rt = self.s.runtime
            saved = (rt._mon, rt._offset, rt._scale)     # don't disturb the agent's mouse mapping
            try:
                out = rt.t_screenshot(rt._mon or 1)
            finally:
                rt._mon, rt._offset, rt._scale = saved
            img = self._small(out.image_b64)
            h = hashlib.md5(base64.b64decode(img)[::97]).hexdigest() if img else None
            if title == self.last_title and h == self.last_hash:
                return None
            self.last_hash = h
        else:
            if title == self.last_title or not ERRORISH.search(title or ""):
                self.last_title = title
                return None
        self.last_title = title
        prompt = f"Active window: {title or '(unknown)'}\nIs there a clear problem worth offering help with?"
        model = self.cfg().get("model") or None
        reply = llm.complete(self.s.settings, SYSTEM, prompt, max_tokens=80, images=[img] if img else None,
                             model=model).strip()
        if not reply or reply.upper().startswith("NONE") or len(reply) > 240:
            return None
        key = re.sub(r"\W+", " ", reply.lower())[:60]
        if key in self.offered:
            return None
        self.offered.add(key)
        self.last_offer_at = time.time()
        self.s.offer(reply, f"window '{title}'")
        return reply

    @staticmethod
    def _small(b64: str | None, width: int = 1024) -> str | None:
        if not b64:
            return None
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)))
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode()

    def stop(self):
        self._stop.set()
