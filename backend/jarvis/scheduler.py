"""Scheduled tasks, reminders and daily briefings.

Jobs live in ~/.jarvis/schedule.json and survive restarts. A job is either
  * a reminder: Jarvis just says the message, or
  * a task: Jarvis runs the message as an instruction through the agent ("clean my Downloads").
Briefings are built-in recurring tasks configured in settings["briefings"].

When: one-off (`at` local time) or repeating (daily / weekdays / weekly on given days / hourly /
every N minutes). Missed one-off jobs (PC was off) still fire once at startup if they are less
than 12 hours late; missed repeats just move on to their next slot.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from typing import Callable

from . import config

FILE = config.HOME / "schedule.json"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
REPEATS = ("none", "daily", "weekdays", "weekly", "hourly", "minutes")


def _parse_time(s: str | None, default: str = "09:00") -> dt.time:
    s = (s or default).strip().lower().replace(".", ":")
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H"):
        try:
            return dt.datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"can't read time {s!r} (use HH:MM)")


def _parse_at(s: str) -> dt.datetime:
    s = s.strip().replace("Z", "")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        d = dt.datetime.combine(dt.date.today(), _parse_time(s))
        if d <= dt.datetime.now():
            d += dt.timedelta(days=1)
    if d.tzinfo is not None:      # convert to local naive time
        d = d.astimezone().replace(tzinfo=None)
    return d


def next_run(job: dict, after: dt.datetime | None = None) -> dt.datetime | None:
    now = after or dt.datetime.now()
    rep = job.get("repeat", "none")
    if rep == "none":
        at = dt.datetime.fromisoformat(job["at"])
        return at if at > now or not job.get("last_run") else None
    if rep == "minutes":
        n = max(1, int(job.get("every_minutes") or 60))
        base = dt.datetime.fromisoformat(job["last_run"]) if job.get("last_run") else now
        nxt = base + dt.timedelta(minutes=n)
        return nxt if nxt > now else now + dt.timedelta(minutes=n)
    if rep == "hourly":
        minute = _parse_time(job.get("time") or "00:00").minute
        nxt = now.replace(minute=minute, second=0, microsecond=0)
        return nxt if nxt > now else nxt + dt.timedelta(hours=1)
    t = _parse_time(job.get("time"))
    days = {"daily": set(range(7)), "weekdays": {0, 1, 2, 3, 4}}.get(rep)
    if days is None:   # weekly
        days = {DAYS.index(d[:3].lower()) for d in (job.get("days") or ["mon"]) if d[:3].lower() in DAYS} or {0}
    for i in range(0, 8):
        d = now.date() + dt.timedelta(days=i)
        cand = dt.datetime.combine(d, t)
        if d.weekday() in days and cand > now:
            return cand
    return None


def describe(job: dict) -> str:
    rep = job.get("repeat", "none")
    if rep == "none":
        when = dt.datetime.fromisoformat(job["at"]).strftime("%a %d %b %H:%M")
    elif rep == "minutes":
        when = f"every {job.get('every_minutes')} min"
    elif rep == "hourly":
        when = "hourly"
    elif rep == "weekly":
        when = "every " + ", ".join(d.capitalize() for d in job.get("days") or ["mon"]) + f" at {job.get('time')}"
    else:
        when = f"{rep} at {job.get('time')}"
    return when


class Scheduler:
    def __init__(self, settings: dict, fire: Callable[[dict], None]):
        self.settings = settings
        self.fire = fire
        self.lock = threading.RLock()
        self.jobs: list[dict] = self._load()
        self._stop = threading.Event()
        self._catch_up()
        self.sync_briefings()
        threading.Thread(target=self._loop, daemon=True, name="scheduler").start()

    # storage -----------------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            return json.loads(FILE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return []

    def _save(self):
        with self.lock:
            FILE.write_text(json.dumps(self.jobs, indent=1), "utf-8")

    # api ---------------------------------------------------------------------
    def add(self, title: str, message: str, kind: str = "reminder", at: str | None = None,
            in_minutes: float | None = None, repeat: str = "none", time_: str | None = None,
            days: list | None = None, every_minutes: int | None = None, builtin: str | None = None) -> dict:
        repeat = (repeat or "none").lower()
        if repeat not in REPEATS:
            raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")
        if kind not in ("reminder", "task", "briefing"):
            raise ValueError("kind must be reminder or task")
        job = {"id": uuid.uuid4().hex[:8], "title": title.strip()[:80] or message[:60], "message": message,
               "kind": kind, "repeat": repeat, "enabled": True, "created": dt.datetime.now().isoformat(timespec="seconds"),
               "last_run": None}
        if builtin:
            job["builtin"] = builtin
        if repeat == "none":
            if in_minutes:
                when = dt.datetime.now() + dt.timedelta(minutes=float(in_minutes))
            elif at:
                when = _parse_at(at)
            else:
                raise ValueError("give `at` (local date-time) or `in_minutes` for a one-off job")
            if when < dt.datetime.now() - dt.timedelta(minutes=1):
                raise ValueError(f"{when:%Y-%m-%d %H:%M} is already in the past")
            job["at"] = when.replace(microsecond=0).isoformat()
        else:
            if repeat in ("daily", "weekdays", "weekly", "hourly"):
                job["time"] = _parse_time(time_ or (at if at and len(at) <= 8 else None)).strftime("%H:%M")
            if repeat == "weekly":
                job["days"] = [d[:3].lower() for d in (days or ["mon"])]
            if repeat == "minutes":
                job["every_minutes"] = int(every_minutes or 60)
        nr = next_run(job)
        if nr is None:
            raise ValueError("that time is already in the past")
        job["next_run"] = nr.isoformat(timespec="seconds")
        with self.lock:
            self.jobs.append(job)
            self._save()
        return job

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            n = len(self.jobs)
            self.jobs = [j for j in self.jobs if j["id"] != job_id and j.get("title", "").lower() != job_id.lower()]
            self._save()
            return len(self.jobs) != n

    def list(self, include_builtin: bool = True) -> list[dict]:
        with self.lock:
            out = [dict(j, when=describe(j)) for j in self.jobs if include_builtin or not j.get("builtin")]
        return sorted(out, key=lambda j: j.get("next_run") or "9999")

    def upcoming(self, hours: float = 36) -> list[dict]:
        limit = (dt.datetime.now() + dt.timedelta(hours=hours)).isoformat()
        return [j for j in self.list() if j.get("enabled") and j.get("next_run") and j["next_run"] <= limit]

    def sync_briefings(self):
        """Create/update/remove the built-in morning and evening briefing jobs from settings."""
        b = self.settings.get("briefings", {})
        with self.lock:
            for key, title in (("morning", "Morning briefing"), ("evening", "Evening briefing")):
                cfg = b.get(key, {})
                existing = next((j for j in self.jobs if j.get("builtin") == key), None)
                if not cfg.get("enabled"):
                    if existing:
                        self.jobs.remove(existing)
                    continue
                t = _parse_time(cfg.get("time") or ("08:00" if key == "morning" else "18:30")).strftime("%H:%M")
                rep = "weekdays" if cfg.get("weekdays_only") else "daily"
                if existing and existing.get("time") == t and existing.get("repeat") == rep:
                    continue
                if existing:
                    self.jobs.remove(existing)
                job = {"id": f"brief-{key}", "title": title, "message": key, "kind": "briefing", "repeat": rep,
                       "time": t, "enabled": True, "builtin": key, "last_run": None,
                       "created": dt.datetime.now().isoformat(timespec="seconds")}
                job["next_run"] = next_run(job).isoformat(timespec="seconds")
                self.jobs.append(job)
            self._save()

    # loop --------------------------------------------------------------------
    def _catch_up(self):
        now = dt.datetime.now()
        with self.lock:
            for j in self.jobs:
                if not j.get("next_run"):
                    continue
                due = dt.datetime.fromisoformat(j["next_run"])
                if due <= now and j.get("repeat") != "none":
                    nr = next_run(j, now)
                    j["next_run"] = nr.isoformat(timespec="seconds") if nr else None
                elif due <= now and now - due > dt.timedelta(hours=12):
                    j["enabled"] = False
            self._save()

    def tick(self, now: dt.datetime | None = None) -> list[dict]:
        """Fire due jobs. Returns the jobs fired (used by tests)."""
        now = now or dt.datetime.now()
        fired = []
        with self.lock:
            for j in list(self.jobs):
                if not j.get("enabled") or not j.get("next_run"):
                    continue
                if dt.datetime.fromisoformat(j["next_run"]) <= now:
                    j["last_run"] = now.isoformat(timespec="seconds")
                    nr = next_run(j, now)
                    if nr is None:
                        self.jobs.remove(j)
                    else:
                        j["next_run"] = nr.isoformat(timespec="seconds")
                    fired.append(dict(j))
            if fired:
                self._save()
        for j in fired:
            try:
                self.fire(j)
            except Exception as e:  # noqa: BLE001
                print(f"[jarvis] scheduled job failed: {e}")
        return fired

    def _loop(self):
        while not self._stop.wait(10):
            self.tick()

    def stop(self):
        self._stop.set()
