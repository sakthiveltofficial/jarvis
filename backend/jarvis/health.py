"""Health nudges: posture, water, eye breaks and sleep.

  * Posture  (needs Presence on): you've been slouching, leaning in or craning your neck for a few minutes
  * Eyes     the 20-20-20 rule: after `eyes_min` of screen time, look at something 20 feet away for 20 seconds
  * Water    after `water_min` at the desk without a drink ("I drank some water" resets it, and counts for the report)
  * Sleep    you're still at the computer after your bedtime (once, then one gentle follow-up an hour later)

Screen time comes from keyboard/mouse activity, plus the camera when Presence is on (so reading or watching without
touching anything still counts, and a short walk away counts as a break). Nudges are short spoken lines, never during
a meeting or while Jarvis is busy, and at most one every `min_gap_min` (sleep nudges excepted).
`HealthCoach.tick` is pure logic so it can be tested without a clock or a camera.
"""
from __future__ import annotations

import datetime as dt
from collections import deque

BREAK_S = 90            # this long away from the screen counts as an eye break
AWAY_IDLE_S = 120       # keyboard/mouse idle this long (and no face, if the camera is on) = away


def _hm(s: str | None, default: str) -> dt.time:
    try:
        h, m = (s or default).split(":")[:2]
        return dt.time(int(h), int(m))
    except Exception:  # noqa: BLE001
        h, m = default.split(":")
        return dt.time(int(h), int(m))


def past_bedtime(local: dt.datetime, bedtime: str) -> bool:
    """True from bedtime until 5 am (bedtime may be before or after midnight)."""
    b = _hm(bedtime, "23:30")
    t = local.time()
    if b >= dt.time(12, 0):
        return t >= b or t < dt.time(5, 0)
    return b <= t < dt.time(5, 0)


class PostureWindow:
    """Recent posture readings; 'slouching' when most of the last few minutes looked bad."""

    def __init__(self):
        self.samples: deque = deque()

    def add(self, t: float, bad: bool, reason: str = ""):
        self.samples.append((t, bad, reason))
        while self.samples and t - self.samples[0][0] > 30 * 60:
            self.samples.popleft()

    def clear(self):
        self.samples.clear()

    def slouching(self, t: float, minutes: float) -> str | None:
        win = [s for s in self.samples if t - s[0] <= minutes * 60]
        if not win or t - win[0][0] < minutes * 60 * 0.8 or len(win) < 6:
            return None
        bad = [s for s in win if s[1]]
        if len(bad) / len(win) < 0.75:
            return None
        reasons: dict[str, int] = {}
        for s in bad:
            reasons[s[2]] = reasons.get(s[2], 0) + 1
        return max(reasons, key=reasons.get) or "slouching"


class HealthCoach:
    def __init__(self):
        self.last_t: float | None = None
        self.screen_s = 0.0           # continuous screen time since the last eye break
        self.water_s = 0.0            # time at the desk since the last drink
        self.away_s = 0.0             # how long away right now
        self.last_nudge = float("-inf")
        self.posture_quiet_until = 0.0
        self.sleep_nudged: dict[str, float] = {}   # night (date it started) -> time of the last sleep nudge
        self.posture = PostureWindow()

    def drank(self):
        self.water_s = 0.0

    def tick(self, now: float, local: dt.datetime, idle_s: float, at_desk: bool | None, cfg: dict,
             quiet: bool = False, quiet_hours: bool = False) -> tuple[str, str] | None:
        """Advance the clocks. Returns (kind, idea) when it's time to nudge. at_desk: camera's view (None = no camera).
        quiet: in a meeting / Jarvis busy (clocks still run, nothing is said). quiet_hours: only sleep nudges."""
        dt_s = 0.0 if self.last_t is None else max(0.0, min(now - self.last_t, 300.0))
        self.last_t = now
        active = idle_s < AWAY_IDLE_S or bool(at_desk)
        if at_desk is False and idle_s >= 45:
            active = False
        if active:
            if self.away_s >= BREAK_S:
                self.screen_s = 0.0
            self.away_s = 0.0
            self.screen_s += dt_s
            self.water_s += dt_s
        else:
            self.away_s += dt_s
            if self.away_s >= BREAK_S:
                self.posture.clear()
            return None
        if not cfg.get("enabled", True) or quiet:
            return None

        # sleep: ignores the global gap, once per night plus one follow-up an hour later
        if cfg.get("sleep", True) and past_bedtime(local, cfg.get("bedtime") or "23:30"):
            night = (local.date() - dt.timedelta(days=1 if local.hour < 12 else 0)).isoformat()
            last = self.sleep_nudged.get(night)
            if last is None or (last >= 0 and now - last >= 3600):
                self.sleep_nudged[night] = now if last is None else -1.0     # -1: follow-up done
                self.last_nudge = now
                goal = float(cfg.get("sleep_goal_h", 7.5))
                return ("sleep", f"It's {local:%H:%M}, past their bedtime, and they're still at the computer. "
                                 + ("Gently suggest wrapping up for tonight" if last is None else
                                    "A second, softer nudge toward bed")
                                 + f" (their goal is {goal:g} hours of sleep).")
        if quiet_hours or now - self.last_nudge < 60 * float(cfg.get("min_gap_min", 15)):
            return None
        if cfg.get("posture", True) and now >= self.posture_quiet_until:
            why = self.posture.slouching(now, float(cfg.get("posture_min", 3)))
            if why:
                self.posture_quiet_until = now + 60 * 3 * float(cfg.get("posture_min", 3))
                self.posture.clear()
                self.last_nudge = now
                return ("posture", f"For the last few minutes they've been {why}. A quick, light posture check: "
                                   "sit back, shoulders down, screen at eye level.")
        if cfg.get("eyes", True) and self.screen_s >= 60 * float(cfg.get("eyes_min", 20)):
            mins = round(self.screen_s / 60)
            self.screen_s = 0.0
            self.last_nudge = now
            return ("eyes", f"{mins} minutes of screen time without a break. Suggest the 20-20-20 rule: look at "
                            "something about 20 feet away for 20 seconds.")
        if cfg.get("water", True) and self.water_s >= 60 * float(cfg.get("water_min", 60)):
            mins = round(self.water_s / 60)
            self.water_s = 0.0
            self.last_nudge = now
            return ("water", f"About {mins} minutes at the desk since their last drink of water. Suggest a glass; "
                             "they can tell you when they've had one.")
        return None


FALLBACK = {
    "posture": "Quick posture check: sit back, shoulders down, screen at eye level.",
    "eyes": "Time to rest your eyes: look at something far away for twenty seconds.",
    "water": "It's been a while since your last glass of water. Fancy one?",
    "sleep": "It's past your bedtime. Perhaps time to wrap up for the night?",
}
