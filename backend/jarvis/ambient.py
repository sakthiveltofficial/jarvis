"""Ambient mode: Jarvis chimes in on its own, like a friend in the room.

  * Long coding session  -> "You've been coding for three hours. Fancy a break?"
  * Long screen session  -> a stretch / eyes break after a long stretch of any work
  * Back after a while   -> a short welcome back with what's coming up
  * Late at night        -> a gentle nudge toward bed (only outside quiet hours)

It watches only the active window title and keyboard/mouse idle time (nothing is stored or sent
anywhere except the one short line it asks the model to phrase). Never during quiet hours, a meeting,
a game or video in full swing, or while Jarvis is busy; at most one chime-in per `min_gap_min`.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from . import llm

CODING = re.compile(r"visual studio|vs ?code|\bcode\b|cursor|pycharm|intellij|webstorm|android studio|rider|clion|"
                    r"goland|sublime|vim|neovim|emacs|zed|xcode|jupyter|terminal|powershell|cmd\.exe|windows terminal|"
                    r"iterm|konsole|git ?bash|wsl|\.py\b|\.js\b|\.ts\b|\.rs\b|\.java\b|\.cpp\b|\.go\b", re.I)
DO_NOT_DISTURB = re.compile(r"zoom meeting|\bzoom\b|microsoft teams|\bteams\b|google meet|meet\.google|webex|"
                            r"skype|discord.*(voice|call)|slack.*huddle|obs studio|recording|presentation|"
                            r"slide ?show|powerpoint slide show", re.I)
WATCHING = re.compile(r"youtube|netflix|prime video|hotstar|vlc|media player|twitch|spotify", re.I)

PHRASE_SYSTEM = """You are {name}, {user}'s warm, witty personal assistant (dry British humour). You are about to
speak up on your own, unprompted. Say the idea below as ONE short, natural spoken line (under 22 words): kind,
light, never preachy, no lists, no emoji. If {user} usually talks in Tamil or Tanglish (see the hint), say it the
same way. Reply with only the line."""


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (0 if unknown)."""
    try:
        if os.name == "nt":
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            li = LASTINPUTINFO()
            li.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
                return max(0.0, (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0)
        elif sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=3).stdout
            m = re.search(r'"HIDIdleTime" = (\d+)', out)
            if m:
                return int(m.group(1)) / 1e9
        elif shutil.which("xprintidle"):
            out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=3).stdout
            return int(out.strip()) / 1000.0
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _hm(s: str, default: str) -> dt.time:
    try:
        h, m = (s or default).split(":")[:2]
        return dt.time(int(h), int(m))
    except Exception:  # noqa: BLE001
        h, m = default.split(":")
        return dt.time(int(h), int(m))


def in_quiet_hours(cfg: dict, now: dt.datetime) -> bool:
    if not cfg.get("quiet_enabled", True):
        return False
    start, end = _hm(cfg.get("quiet_start"), "23:00"), _hm(cfg.get("quiet_end"), "07:00")
    t = now.time()
    if start == end:
        return False
    return (start <= t < end) if start < end else (t >= start or t < end)


class Ambient:
    """Pure-ish state machine (tick() takes the time, window title and idle seconds), plus a thread."""

    AWAY_S = 5 * 60          # this much idle counts as a break
    TICK_S = 20

    def __init__(self, session=None, start: bool = True, title_fn=None, idle_fn=None):
        self.s = session
        self.title_fn = title_fn
        self.idle_fn = idle_fn or idle_seconds
        self.coding_since: float | None = None
        self.active_since: float | None = None
        self.last_seen_active: float | None = None
        self.away_since: float | None = None
        self.last_chime = 0.0
        self.said: dict[str, float] = {}         # kind -> when
        self.late_night_date = None
        self._stop = threading.Event()
        if start:
            threading.Thread(target=self._loop, daemon=True, name="ambient").start()

    def cfg(self) -> dict:
        return (self.s.settings.get("ambient") if self.s else {}) or {}

    # ------------------------------------------------------------------ core logic
    def tick(self, now: float, title: str, idle: float, cfg: dict, local: dt.datetime | None = None,
             busy: bool = False) -> tuple[str, str] | None:
        """Update the activity model. Returns (kind, idea) when it's time to chime in."""
        local = local or dt.datetime.fromtimestamp(now)
        active = idle < self.AWAY_S
        if not active:
            if self.away_since is None:
                self.away_since = now - idle
            self.coding_since = None
            self.active_since = None
            return None
        came_back = self.away_since is not None and now - self.away_since >= 60 * float(cfg.get("welcome_after_min", 45))
        self.away_since = None
        if self.active_since is None:
            self.active_since = now
        if CODING.search(title or ""):
            if self.coding_since is None:
                self.coding_since = now
            self.last_seen_active = now
        elif self.coding_since is not None and self.last_seen_active and now - self.last_seen_active > 10 * 60:
            self.coding_since = None              # switched to other things for a while

        if not cfg.get("enabled", True) or busy or in_quiet_hours(cfg, local):
            return None
        if DO_NOT_DISTURB.search(title or ""):
            return None
        if now - self.last_chime < 60 * float(cfg.get("min_gap_min", 45)):
            return None

        out = None
        code_h = float(cfg.get("coding_break_h", 2.0))
        screen_h = float(cfg.get("screen_break_h", 3.0))
        if came_back and cfg.get("welcome_back", True):
            out = ("welcome", "Welcome them back after time away; if anything is scheduled soon, mention it briefly.")
        elif cfg.get("coding_break", True) and self.coding_since and now - self.coding_since >= code_h * 3600:
            hrs = (now - self.coding_since) / 3600
            out = ("coding", f"They've been coding for about {_hours(hrs)} without a real break. Suggest a short "
                             "break (stretch, water, rest the eyes), maybe offer to keep an eye on things.")
        elif cfg.get("screen_break", True) and self.active_since and now - self.active_since >= screen_h * 3600 \
                and not WATCHING.search(title or ""):
            hrs = (now - self.active_since) / 3600
            out = ("screen", f"They've been at the computer for about {_hours(hrs)} straight. Suggest a quick "
                             "stretch or eye break.")
        elif cfg.get("late_night", False) and local.hour >= int(cfg.get("late_hour", 1)) and local.hour < 5 \
                and self.late_night_date != local.date():
            self.late_night_date = local.date()
            out = ("late", f"It's {local:%H:%M} and they're still up. Gently suggest wrapping up for the night.")
        if out:
            self.last_chime = now
            self.said[out[0]] = now
            if out[0] in ("coding", "screen"):    # don't nag again for the same stretch
                self.coding_since = now if out[0] == "coding" else self.coding_since
                self.active_since = now
        return out

    # ------------------------------------------------------------------ thread
    def _loop(self):
        from .tools import active_window_title
        title_fn = self.title_fn or active_window_title
        while not self._stop.wait(self.TICK_S):
            c = self.cfg()
            if not c.get("enabled", True):
                continue
            try:
                title = title_fn() or ""
                res = self.tick(time.time(), title, self.idle_fn(), c, busy=self._busy())
                if res:
                    self.say(*res)
            except Exception as e:  # noqa: BLE001
                print(f"[jarvis] ambient tick failed: {e}")

    def _busy(self) -> bool:
        s = self.s
        if s is None:
            return False
        return (s.state not in ("idle", "listening") or bool(s.task and not s.task.done())
                or s.mic.speaking or bool(s.pending) or s.teacher is not None)

    def phrase(self, kind: str, idea: str) -> str:
        st = self.s.settings
        user = st.get("user_name") or "the user"
        from .lang import LABELS
        lang = LABELS.get(getattr(self.s, "last_lang", ""), "")
        extra = ""
        if kind == "welcome":
            try:
                ups = self.s.scheduler.upcoming(3)[:3]
                if ups:
                    extra = "\nComing up: " + "; ".join(f"{j['title']} at {str(j.get('next_run'))[11:16]}" for j in ups)
            except Exception:  # noqa: BLE001
                pass
        try:
            line = llm.complete(st, PHRASE_SYSTEM.format(name=st.get("assistant_name") or "Jarvis", user=user),
                                f"Idea: {idea}{extra}\nLanguage hint: {lang or 'English'}", max_tokens=80,
                                model=(st.get("ambient") or {}).get("model") or None).strip().strip('"')
            if line and len(line) < 300:
                return line
        except Exception as e:  # noqa: BLE001
            print(f"[jarvis] ambient phrasing failed: {e}")
        return FALLBACK.get(kind, "Just checking in.").format(user=st.get("user_name") or "")

    def say(self, kind: str, idea: str):
        line = self.phrase(kind, idea)
        self.s.offer(line, f"ambient mode ({kind}): {idea}")

    def stop(self):
        self._stop.set()


FALLBACK = {
    "coding": "You've been coding for a good while now. Fancy a short break? I'll keep an eye on things.",
    "screen": "You've been at it for hours. A quick stretch and some water might do wonders.",
    "welcome": "Welcome back. I'm here whenever you need me.",
    "late": "It's getting rather late. Perhaps time to wrap up for the night?",
}


def _hours(h: float) -> str:
    whole = round(h * 2) / 2
    return f"{whole:g} hour{'s' if whole != 1 else ''}"
