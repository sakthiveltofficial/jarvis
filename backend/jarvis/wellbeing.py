"""Presence, health nudges and the weekly life report, wired to the session.

One thread ticks every 20 seconds: it logs the minute (lifelog.py), runs the health coach (health.py) with what the
camera sees (presence.py), and reads the weekly report when it's due. Presence events (arrive, leave, stranger) come
straight from the camera thread.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import traceback

from . import character, config, health, lifelog, llm, presence
from .ambient import DO_NOT_DISTURB, idle_seconds, in_quiet_hours, phrase_system
from .tools import ToolOutput

STATE_FILE = config.HOME / "life_state.json"


class Wellbeing:
    TICK_S = 20

    def __init__(self, session, hub, start: bool = True, idle_fn=None, title_fn=None):
        self.s = session
        self.idle_fn = idle_fn or idle_seconds
        self.title_fn = title_fn
        self.log = lifelog.LifeLog()
        self.coach = health.HealthCoach()
        self.presence = presence.Presence(session.settings, hub, self.on_presence, idle_fn=self.idle_fn)
        self.state = self._load_state()
        self.last_greet_at = 0.0
        self.last_minute = 0
        self.reporting = False
        self.last_report: dict | None = None
        self._stop = threading.Event()
        if self.state.get("last_week") is None:      # first start: the first report is this week's
            prev = dt.date.today() - dt.timedelta(days=7)
            y, w, _ = prev.isocalendar()
            self.state["last_week"] = f"{y}-W{w:02d}"
            self._save_state()
        if start:
            threading.Thread(target=self.sync, name="presence-sync", daemon=True).start()
            threading.Thread(target=self._loop, name="wellbeing", daemon=True).start()

    def cfg(self, key: str) -> dict:
        return self.s.settings.get(key) or {}

    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self):
        try:
            STATE_FILE.write_text(json.dumps(self.state, indent=2), "utf-8")
        except OSError:
            pass

    def sync(self):
        self.presence.sync()

    def stop(self):
        self._stop.set()
        self.presence.stop()

    # ------------------------------------------------------------------ speaking up
    def _busy(self) -> bool:
        try:
            return self.s.ambient._busy()
        except Exception:  # noqa: BLE001
            return False

    def phrase(self, kind: str, idea: str, fallback: str) -> str:
        st = self.s.settings
        from .lang import LABELS
        lang = LABELS.get(getattr(self.s, "last_lang", ""), "")
        extra = ""
        if kind == "greet":
            try:
                ups = self.s.scheduler.upcoming(6)[:3]
                if ups:
                    extra = "\nComing up: " + "; ".join(f"{j['title']} at {str(j.get('next_run'))[11:16]}" for j in ups)
            except Exception:  # noqa: BLE001
                pass
        try:
            line = llm.complete(st, phrase_system(st),
                                f"Idea: {idea}{extra}\nLanguage hint: {lang or 'English'}", max_tokens=80,
                                model=(st.get("ambient") or {}).get("model") or None).strip().strip('"')
            if line and len(line) < 300:
                return line
        except Exception as e:  # noqa: BLE001
            print(f"[jarvis] couldn't phrase the {kind} line: {e}")
        return fallback.format(user=st.get("user_name") or "")

    # ------------------------------------------------------------------ presence events (camera thread)
    def on_presence(self, kind: str, data: dict):
        cfg = self.cfg("presence")
        if kind == "arrive":
            self.log.event("arrive", str(round(data.get("away_s") or 0)))
            today = dt.date.today().isoformat()
            first = self.state.get("greeted_day") != today
            away = data.get("away_s")
            if cfg.get("greet", True) and (first or (away or 0) >= 60 * float(cfg.get("greet_after_min", 20))):
                self.state["greeted_day"] = today
                self._save_state()
                self.last_greet_at = time.time()
                idea = presence.greet_idea(away, first, dt.datetime.now())
                if first:
                    idea += self._sleep_hint()
                threading.Thread(target=self._say, args=("greet", idea, "Welcome back{user_sep}."),
                                 daemon=True).start()
        elif kind == "leave":
            self.log.event("leave")
            if data.get("lock"):
                ok = presence.lock_screen()
                self.log.event("lock", "ok" if ok else "failed")
                self.s.emit({"type": "info", "text": "🔒 You stepped away, so I locked the PC." if ok else
                             "You stepped away, but I couldn't lock the PC."})
        elif kind == "stranger":
            self.log.event("stranger", "with owner" if data.get("owner_present") else "alone")
            if not cfg.get("stranger_alert", True):
                return
            if data.get("owner_present"):
                self.s.emit({"type": "info", "text": "👀 Heads up: someone I don't recognise is looking at your screen."})
                return
            locked = False
            if cfg.get("stranger_lock", True):
                locked = presence.lock_screen()
            when = dt.datetime.now().strftime("%H:%M")
            text = ("Someone I don't recognise sat down at your computer" + (", so I've locked it." if locked else "."))
            self.s.emit({"type": "assistant", "text": f"🚨 {text} ({when})" + (
                f" Photo saved: {data['photo']}" if data.get("photo") else ""), "proactive": "presence"})
            try:
                self.s.speak(character.stranger_line(self.s.settings, locked))
            except Exception:  # noqa: BLE001
                pass
            self.s.context_notes.append(f"At {when} an unknown person sat at the computer while the user was away"
                                        + (" (PC locked)" if locked else ""))

    def _sleep_hint(self) -> str:
        try:
            today = dt.date.today()
            nights = self.log.nights(today, today + dt.timedelta(days=1))
            if nights:
                n = nights[-1]
                goal = float(self.cfg("health").get("sleep_goal_h", 7.5))
                if n["hours"] < goal - 1:
                    return (f" They were on the computer until {n['bed']} last night, only about {n['hours']:g} hours "
                            "away from it; maybe say go easy today.")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _say(self, kind: str, idea: str, fallback: str):
        user = self.s.settings.get("user_name") or ""
        fallback = character.fallback(self.s.settings, kind, fallback)
        line = self.phrase(kind, idea, fallback.replace("{user_sep}", f", {user}" if user else ""))
        self.s.announce(line, "presence" if kind == "greet" else "health")

    # ------------------------------------------------------------------ the tick
    def _loop(self):
        from .tools import active_window_title
        title_fn = self.title_fn or active_window_title
        while not self._stop.wait(self.TICK_S):
            try:
                self.tick(time.time(), title_fn() or "", float(self.idle_fn()))
            except Exception as e:  # noqa: BLE001
                print(f"[jarvis] wellbeing tick failed: {e}")

    def tick(self, now: float, title: str, idle: float):
        p = self.presence
        camera_on = p.state == "running" and self.cfg("presence").get("enabled")
        at_desk = p.tracker.present if camera_on else None
        post = p.posture if camera_on else None
        if post and now - post.get("at", 0) > 15:
            post = None
        if post:
            self.coach.posture.add(now, post["bad"], post["reason"])
        active = idle < health.AWAY_IDLE_S or bool(at_desk)
        lcfg = self.cfg("life")
        minute = int(now // 60)
        if lcfg.get("track", True) and minute != self.last_minute:
            self.last_minute = minute
            self.log.record_minute(now, title, active, at_desk, post["bad"] if post else None)
            if minute % 1440 == 0:
                self.log.prune(int(lcfg.get("keep_days", 120)))
        hcfg = self.cfg("health")
        local = dt.datetime.fromtimestamp(now)
        quiet = self._busy() or bool(DO_NOT_DISTURB.search(title or ""))
        nudge = self.coach.tick(now, local, idle, at_desk, hcfg, quiet=quiet,
                                quiet_hours=in_quiet_hours(self.cfg("ambient"), local))
        if nudge:
            kind, idea = nudge
            self.log.event("nudge", kind)
            threading.Thread(target=self._say, args=(kind, idea, health.FALLBACK[kind]), daemon=True).start()
        week = lifelog.report_due(lcfg, local, self.state.get("last_week"))
        if week and not self.reporting and not self._busy():
            self.reporting = True
            threading.Thread(target=self._weekly, args=(week,), daemon=True).start()

    def _weekly(self, week: str):
        try:
            rep = self.report(week)
            self.state["last_week"] = week
            self._save_state()
            if rep["stats"]["screen_hours"] < 1:
                return                          # nothing worth reading (first week, or away all week)
            if self.cfg("life").get("speak", True):
                self.s.announce(rep["text"], "report")
            else:
                self.s.emit({"type": "assistant", "text": rep["text"], "proactive": "report"})
            self.s.emit({"type": "info", "text": f"📊 Weekly life report saved to {rep['path']}"})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self.reporting = False

    # ------------------------------------------------------------------ the report
    def week_range(self, week: str | None = None) -> tuple[dt.date, dt.date, str]:
        if week:
            y, w = week.split("-W")
            start = dt.date.fromisocalendar(int(y), int(w), 1)
        else:
            start, _ = lifelog.week_bounds(dt.date.today())
            y, w, _ = start.isocalendar()
            week = f"{y}-W{w:02d}"
        return start, start + dt.timedelta(days=7), week

    def report(self, week: str | None = None, use_llm: bool = True) -> dict:
        start, end, week = self.week_range(week)
        bedtime = self.cfg("health").get("bedtime") or "23:30"
        st = self.log.stats(start, end, bedtime)
        prev = self.log.stats(start - dt.timedelta(days=7), start, bedtime)
        goals = [lifelog.goal_progress(g, st) for g in lifelog.load_goals()]
        user = self.s.settings.get("user_name") or ""
        text = ""
        if use_llm and st["screen_hours"] >= 1:
            diary = []
            try:
                if self.s.memory:
                    diary = [f"{d['date']}: {d['text']}" for d in self.s.memory.digests(10)
                             if start.isoformat() <= str(d["date"]) < end.isoformat()]
            except Exception:  # noqa: BLE001
                pass
            try:
                from .lang import LABELS
                st_ = self.s.settings
                text = llm.complete(st_, lifelog.report_system(st_, user or "the user"),
                                    lifelog.report_prompt(st, prev, goals, diary,
                                                          LABELS.get(getattr(self.s, "last_lang", ""), "")),
                                    max_tokens=700, model=self.cfg("life").get("model") or None).strip()
            except Exception as e:  # noqa: BLE001
                print(f"[jarvis] life report phrasing failed: {e}")
                text = ""
        if not text:
            text = lifelog.plain_report(st, prev if prev["screen_hours"] else None, goals, user)
        lifelog.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = lifelog.REPORTS_DIR / f"{week}.md"
        path.write_text(lifelog.to_markdown(text, st, goals, week), "utf-8")
        self.last_report = {"week": week, "text": text, "path": str(path), "stats": st, "goals": goals,
                            "at": dt.datetime.now().isoformat(timespec="seconds")}
        return self.last_report

    def reports(self) -> list[dict]:
        if not lifelog.REPORTS_DIR.exists():
            return []
        return [{"week": p.stem, "path": str(p)} for p in sorted(lifelog.REPORTS_DIR.glob("*.md"), reverse=True)]

    # ------------------------------------------------------------------ the agent's `life` tool
    def tool(self, a: dict) -> ToolOutput:
        act = (a.get("action") or "").lower()
        if act == "log":
            what = (a.get("what") or "").strip().lower()
            n = max(1, min(20, int(a.get("count") or 1)))
            if not what:
                return ToolOutput("Say what to log: 'water' or a habit name.", is_error=True)
            if what in ("water", "glass of water", "a glass of water", "drink water"):
                for _ in range(n):
                    self.log.event("water")
                self.coach.drank()
                return ToolOutput(f"Logged {n} glass{'es' if n > 1 else ''} of water "
                                  f"({self.log.today_count('water')} today).")
            for _ in range(n):
                self.log.event("habit", what)
            start, end, _ = self.week_range()
            return ToolOutput(f"Logged {what} ({self.log.count('habit', start, end, what)} this week).")
        if act == "goal_set":
            try:
                g = lifelog.parse_goal(a)
            except ValueError as e:
                return ToolOutput(str(e), is_error=True)
            goals = [x for x in lifelog.load_goals() if x["name"].lower() != g["name"].lower()] + [g]
            lifelog.save_goals(goals)
            return ToolOutput(f"Goal saved: {g['name']}, {'at most' if g['direction'] == 'max' else 'at least'} "
                              f"{g['target']:g} {g['unit']} per {g['period']}. It's in the weekly report.")
        if act == "goal_remove":
            name = (a.get("name") or "").strip().lower()
            goals = lifelog.load_goals()
            keep = [x for x in goals if x["name"].lower() != name]
            if len(keep) == len(goals):
                return ToolOutput(f"No goal called {name!r}.", is_error=True)
            lifelog.save_goals(keep)
            return ToolOutput(f"Removed the goal {name!r}.")
        if act == "goals":
            start, end, _ = self.week_range()
            st = self.log.stats(start, end, self.cfg("health").get("bedtime") or "23:30")
            goals = [lifelog.goal_progress(g, st) for g in lifelog.load_goals()]
            if not goals:
                return ToolOutput("No goals yet. The user can set some, e.g. 'exercise 3 times a week'.")
            return ToolOutput("\n".join(
                f"- {g['name']}: {g['actual']:g} of {g['target']:g} {g['unit']} per {g['period']} "
                f"({'on track' if g['met'] else 'not yet'})" for g in goals))
        if act == "stats":
            days = max(1, min(120, int(a.get("days") or 7)))
            end = dt.date.today() + dt.timedelta(days=1)
            st = self.log.stats(end - dt.timedelta(days=days), end, self.cfg("health").get("bedtime") or "23:30")
            return ToolOutput(lifelog.stats_text(st))
        if act == "report":
            rep = self.report()
            return ToolOutput(f"Weekly life report so far (saved to {rep['path']}). Read it to the user as your "
                              f"reply, as written:\n\n{rep['text']}")
        return ToolOutput("action must be log, goal_set, goal_remove, goals, stats or report.", is_error=True)

    def status(self) -> dict:
        return {"presence": self.presence.status(), "camera": self.presence.hub.status(),
                "health": {"screen_min": round(self.coach.screen_s / 60), "water_min": round(self.coach.water_s / 60),
                           "water_today": self.log.today_count("water"),
                           "posture": self.presence.posture},
                "life": {"last_week": self.state.get("last_week"), "reports": self.reports()[:8],
                         "goals": lifelog.load_goals()}}
