"""Life log and the weekly "life report".

Once a minute Jarvis notes which app is in front (the app's name and a category such as coding or meetings, never the
window's full title), whether you were active, at the desk (camera) and sitting well. It also logs what you tell it
("I drank water", "I went for a run") and the health nudges and presence events. All of it lives in
~/.jarvis/life.db on this PC and is pruned after `keep_days`.

Every week (Sunday 19:00 by default) the numbers are turned into a short spoken report in Jarvis's voice: time spent
and where it went, goals and habits against your targets, water, posture, sleep, compared with the week before. Only
the totals (not the log) go to the AI model to phrase it; without a model a plain summary is read instead. Reports are
saved as Markdown in ~/.jarvis/reports.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import config
from .ambient import CODING, WATCHING
from .tools import ToolOutput, ToolSpec

DB_FILE = config.HOME / "life.db"
GOALS_FILE = config.HOME / "life_goals.json"
REPORTS_DIR = config.HOME / "reports"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

CATEGORIES = [
    ("meetings", re.compile(r"zoom|microsoft teams|\bteams\b|google meet|meet\.google|webex|skype|huddle", re.I)),
    ("coding", CODING),
    ("communication", re.compile(r"gmail|outlook|\bmail\b|thunderbird|slack|whatsapp|telegram|discord|signal|"
                                 r"messenger|messages", re.I)),
    ("social", re.compile(r"twitter|\bx\.com\b|instagram|facebook|reddit|linkedin|threads|tiktok", re.I)),
    ("entertainment", re.compile(WATCHING.pattern + r"|steam|epic games|minecraft|valorant|fortnite|league of legends"
                                 r"|\bgame\b|playing", re.I)),
    ("documents", re.compile(r"\bword\b|excel|powerpoint|google docs|google sheets|google slides|notion|obsidian|"
                             r"onenote|acrobat|\.pdf\b|libreoffice|pages|numbers|keynote", re.I)),
    ("design", re.compile(r"figma|photoshop|illustrator|blender|canva|premiere|after effects|davinci|lightroom", re.I)),
    ("browsing", re.compile(r"chrome|firefox|edge|brave|safari|opera|vivaldi|arc\b", re.I)),
]
LABEL = {"meetings": "meetings", "coding": "coding", "communication": "email and chat", "social": "social media",
         "entertainment": "videos, music and games", "documents": "documents", "design": "design",
         "browsing": "browsing", "other": "other apps"}
SEPARATORS = re.compile(r"\s+[-–—|]\s+")


def categorize(title: str) -> str:
    for name, rx in CATEGORIES:
        if rx.search(title or ""):
            return name
    return "other"


def app_name(title: str) -> str:
    """The app from a window title: 'notes.txt - Notepad' -> 'Notepad'. Never the document or page name."""
    t = (title or "").strip()
    if not t:
        return ""
    parts = [p.strip() for p in SEPARATORS.split(t) if p.strip()]
    name = parts[-1] if parts else t
    return name[:40]


def week_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    start = day - dt.timedelta(days=day.weekday())
    return start, start + dt.timedelta(days=7)


def _hours(sec_or_min: float, minutes: bool = True) -> float:
    return round(sec_or_min / (60 if minutes else 3600), 1)


def _lateness(night: dict) -> float:
    """How late a bedtime is: 22:00 < 23:30 < 01:00."""
    b = night["bed_dt"]
    return (b.hour + (24 if b.hour < 12 else 0)) * 60 + b.minute


class LifeLog:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or DB_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS minutes (ts INTEGER PRIMARY KEY, day TEXT, app TEXT, category TEXT,
                active INTEGER, at_desk INTEGER, posture_bad INTEGER);
            CREATE TABLE IF NOT EXISTS events (ts REAL, day TEXT, kind TEXT, detail TEXT);
            CREATE INDEX IF NOT EXISTS minutes_day ON minutes(day);
            CREATE INDEX IF NOT EXISTS events_day ON events(day);
        """)
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------------ writing
    def record_minute(self, ts: float, title: str, active: bool, at_desk: bool | None = None,
                      posture_bad: bool | None = None):
        minute = int(ts // 60) * 60
        day = dt.datetime.fromtimestamp(minute).date().isoformat()
        row = (minute, day, app_name(title) if active else "", categorize(title) if active else "",
               int(active), None if at_desk is None else int(at_desk), None if posture_bad is None else int(posture_bad))
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO minutes VALUES (?,?,?,?,?,?,?)", row)
            self.db.commit()

    def event(self, kind: str, detail: str = "", ts: float | None = None):
        ts = ts or time.time()
        with self._lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)",
                            (ts, dt.datetime.fromtimestamp(ts).date().isoformat(), kind, (detail or "")[:200]))
            self.db.commit()

    def prune(self, keep_days: int):
        if keep_days <= 0:
            return
        cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
        with self._lock:
            self.db.execute("DELETE FROM minutes WHERE day < ?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE day < ?", (cutoff,))
            self.db.commit()

    def forget_all(self):
        with self._lock:
            self.db.execute("DELETE FROM minutes")
            self.db.execute("DELETE FROM events")
            self.db.commit()

    # ------------------------------------------------------------------ reading
    def _rows(self, sql: str, args: tuple) -> list:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def count(self, kind: str, start: dt.date, end: dt.date, detail: str | None = None) -> int:
        sql = "SELECT count(*) FROM events WHERE kind=? AND day>=? AND day<?"
        args: tuple = (kind, start.isoformat(), end.isoformat())
        if detail is not None:
            sql += " AND lower(detail)=?"
            args += (detail.lower(),)
        return int(self._rows(sql, args)[0][0])

    def today_count(self, kind: str, detail: str | None = None) -> int:
        d = dt.date.today()
        return self.count(kind, d, d + dt.timedelta(days=1), detail)

    def nights(self, start: dt.date, end: dt.date, min_gap_h: float = 3.0) -> list[dict]:
        """Overnight gaps in computer use that end inside [start, end): an estimate of sleep (time off the PC)."""
        lo = int(dt.datetime.combine(start - dt.timedelta(days=1), dt.time(12)).timestamp())
        hi = int(dt.datetime.combine(end, dt.time(14)).timestamp())
        ts = [r[0] for r in self._rows("SELECT ts FROM minutes WHERE active=1 AND ts>=? AND ts<? ORDER BY ts", (lo, hi))]
        out = []
        for a, b in zip(ts, ts[1:]):
            gap_h = (b - a) / 3600
            if gap_h < min_gap_h or gap_h > 16:
                continue
            bed, wake = dt.datetime.fromtimestamp(a + 60), dt.datetime.fromtimestamp(b)
            if not (bed.hour >= 20 or bed.hour < 6) or not (start <= wake.date() < end):
                continue
            out.append({"bed": bed.strftime("%H:%M"), "wake": wake.strftime("%H:%M"), "hours": round(gap_h, 1),
                        "date": wake.date().isoformat(), "bed_dt": bed})
        return out

    def stats(self, start: dt.date, end: dt.date, bedtime: str = "23:30") -> dict:
        """Totals for [start, end)."""
        s, e = start.isoformat(), end.isoformat()
        q = self._rows
        active_min = q("SELECT count(*) FROM minutes WHERE active=1 AND day>=? AND day<?", (s, e))[0][0]
        per_day = {d: n for d, n in q("SELECT day, count(*) FROM minutes WHERE active=1 AND day>=? AND day<? "
                                      "GROUP BY day", (s, e))}
        cats = q("SELECT category, count(*) c FROM minutes WHERE active=1 AND day>=? AND day<? GROUP BY category "
                 "ORDER BY c DESC", (s, e))
        apps = q("SELECT app, count(*) c FROM minutes WHERE active=1 AND app!='' AND day>=? AND day<? GROUP BY app "
                 "ORDER BY c DESC LIMIT 6", (s, e))
        posture = q("SELECT sum(posture_bad), count(posture_bad) FROM minutes WHERE active=1 AND posture_bad IS NOT NULL "
                    "AND day>=? AND day<?", (s, e))[0]
        events = {k: n for k, n in q("SELECT kind, count(*) FROM events WHERE day>=? AND day<? GROUP BY kind", (s, e))}
        nudges = {d: n for d, n in q("SELECT detail, count(*) FROM events WHERE kind='nudge' AND day>=? AND day<? "
                                     "GROUP BY detail", (s, e))}
        habits = {d: n for d, n in q("SELECT lower(detail), count(*) FROM events WHERE kind='habit' AND day>=? AND day<? "
                                     "GROUP BY lower(detail)", (s, e))}
        n_days = max(1, (end - start).days)
        nights = self.nights(start, end)
        late = 0
        try:
            bh, bm = (int(x) for x in bedtime.split(":")[:2])
        except Exception:  # noqa: BLE001
            bh, bm = 23, 30
        for n in nights:
            bed = n["bed_dt"]
            target = dt.datetime.combine(bed.date() if bed.hour >= 12 else bed.date() - dt.timedelta(days=1),
                                         dt.time(bh, bm))
            if target.hour < 12:
                target += dt.timedelta(days=1)
            if bed > target + dt.timedelta(minutes=30):
                late += 1
        busiest = max(per_day.items(), key=lambda kv: kv[1]) if per_day else None
        return {
            "from": s, "to": (end - dt.timedelta(days=1)).isoformat(), "days": n_days,
            "screen_hours": _hours(active_min), "days_used": len(per_day),
            "per_day_hours": {d: _hours(n) for d, n in sorted(per_day.items())},
            "busiest_day": ({"day": dt.date.fromisoformat(busiest[0]).strftime("%A"), "hours": _hours(busiest[1])}
                            if busiest else None),
            "categories": [{"name": LABEL.get(c or "other", c), "key": c or "other", "hours": _hours(n)}
                           for c, n in cats if n >= 5],
            "top_apps": [{"name": a, "hours": _hours(n)} for a, n in apps if n >= 10],
            "water_glasses": events.get("water", 0), "water_per_day": round(events.get("water", 0) / n_days, 1),
            "posture_bad_pct": round(100 * (posture[0] or 0) / posture[1]) if posture[1] else None,
            "nudges": nudges, "habits": habits,
            "arrivals": events.get("arrive", 0), "locks": events.get("lock", 0),
            "visitors": events.get("stranger", 0),
            "sleep": {"nights": len(nights),
                      "avg_hours": round(sum(n["hours"] for n in nights) / len(nights), 1) if nights else None,
                      "late_nights": late,
                      "latest_bed": max(nights, key=_lateness)["bed"] if nights else None},
        }


# --------------------------------------------------------------------------- goals and habits
def load_goals() -> list[dict]:
    try:
        return json.loads(GOALS_FILE.read_text("utf-8")) if GOALS_FILE.exists() else []
    except Exception:  # noqa: BLE001
        return []


def save_goals(goals: list[dict]):
    GOALS_FILE.write_text(json.dumps(goals, indent=2), "utf-8")


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def goal_source(name: str, unit: str) -> str:
    n = _norm(name)
    if n in ("water", "drink water", "glasses of water"):
        return "water"
    if n in ("sleep", "sleep hours"):
        return "sleep"
    if n in ("screen time", "screen", "computer time"):
        return "screen"
    for key, label in LABEL.items():
        if n in (key, label) and unit.startswith("hour"):
            return f"category:{key}"
    return f"habit:{n}"


def goal_progress(goal: dict, st: dict) -> dict:
    """Where a goal stands in a stats() result."""
    src, period, target = goal["source"], goal.get("period", "week"), float(goal.get("target") or 0)
    days = st.get("days", 7)
    if src == "water":
        actual = st["water_per_day"] if period == "day" else st["water_glasses"]
    elif src == "sleep":
        actual = st["sleep"]["avg_hours"] or 0.0
    elif src == "screen":
        actual = st["screen_hours"] / days if period == "day" else st["screen_hours"]
    elif src.startswith("category:"):
        key = src.split(":", 1)[1]
        hrs = sum(c["hours"] for c in st["categories"] if c["key"] == key)
        actual = hrs / days if period == "day" else hrs
    else:
        n = st["habits"].get(src.split(":", 1)[1], 0)
        actual = n / days if period == "day" else n
    actual = round(float(actual), 1)
    met = actual <= target if goal.get("direction") == "max" else actual >= target
    return {"name": goal["name"], "target": target, "unit": goal.get("unit", "times"), "period": period,
            "direction": goal.get("direction", "min"), "actual": actual, "met": met}


# --------------------------------------------------------------------------- the report
REPORT_SYSTEM = """You are {name}, {user}'s personal assistant, with a warm voice and dry British wit. Read out
{user}'s weekly life report. It will be SPOKEN aloud, so: plain sentences, no lists, no markdown, no emoji, numbers
rounded the way a person would say them. Under 220 words. Open with one line on the week overall, then where the time
went, goals and habits (celebrate what was met, be kind and specific about what wasn't), health (water, posture, eye
breaks, sleep), anything notable from the week's diary, and end with one small, concrete suggestion for next week.
If {user} usually talks in Tamil or Tanglish (see the hint), speak the same way. Never invent numbers."""


def report_prompt(st: dict, prev: dict | None, goals: list[dict], diary: list[str], lang: str = "") -> str:
    data = {k: v for k, v in st.items() if k not in ("per_day_hours",)}
    data["sleep"] = {k: v for k, v in st["sleep"].items()}
    if prev:
        data["previous_week"] = {"screen_hours": prev["screen_hours"], "water_glasses": prev["water_glasses"],
                                 "sleep_avg_hours": prev["sleep"]["avg_hours"],
                                 "posture_bad_pct": prev["posture_bad_pct"]}
    data["goals"] = goals
    out = f"This week's numbers (JSON):\n{json.dumps(data, default=str, indent=1)}\n"
    if diary:
        out += "\nDiary for the week (from my memory):\n" + "\n".join(f"- {d}" for d in diary[:7]) + "\n"
    out += f"\nLanguage hint: {lang or 'English'}"
    return out


def plain_report(st: dict, prev: dict | None, goals: list[dict], user: str = "") -> str:
    """The report without a model: still in a speakable voice."""
    who = f", {user}" if user else ""
    parts = [f"Here's your week{who}."]
    if not st["screen_hours"]:
        parts.append("I didn't see you at the computer this week, so there's not much to report.")
    else:
        line = f"You spent about {st['screen_hours']:g} hours at the computer across {st['days_used']} days"
        if prev and prev["screen_hours"]:
            diff = st["screen_hours"] - prev["screen_hours"]
            if abs(diff) >= 1:
                line += f", {abs(diff):.0f} hours {'more' if diff > 0 else 'less'} than last week"
        parts.append(line + ".")
        if st["categories"]:
            top = st["categories"][:3]
            parts.append("Most of it went to " + ", ".join(f"{c['name']} ({c['hours']:g} hours)" for c in top) + ".")
        if st["busiest_day"]:
            parts.append(f"{st['busiest_day']['day']} was the busiest, at {st['busiest_day']['hours']:g} hours.")
    met = [g for g in goals if g["met"]]
    missed = [g for g in goals if not g["met"]]
    if met:
        parts.append("You hit " + ", ".join(g["name"] for g in met) + ". Well done.")
    if missed:
        parts.append("Still to work on: " + ", ".join(f"{g['name']} ({g['actual']:g} of {g['target']:g})"
                                                       for g in missed) + ".")
    if st["water_glasses"]:
        parts.append(f"You logged {st['water_glasses']} glasses of water, about {st['water_per_day']:g} a day.")
    if st["posture_bad_pct"] is not None:
        parts.append(f"You were slouching about {st['posture_bad_pct']}% of the time I could see you.")
    sl = st["sleep"]
    if sl["avg_hours"]:
        parts.append(f"Away from the screen overnight about {sl['avg_hours']:g} hours on average"
                     + (f", with {sl['late_nights']} late nights." if sl["late_nights"] else "."))
    parts.append("Same time next week.")
    return " ".join(parts)


def to_markdown(text: str, st: dict, goals: list[dict], week: str) -> str:
    md = [f"# Life report, {week}", "", f"_{st['from']} to {st['to']}_", "", text, "", "## Numbers", "",
          f"- Screen time: {st['screen_hours']:g} h over {st['days_used']} days"]
    for c in st["categories"]:
        md.append(f"  - {c['name']}: {c['hours']:g} h")
    md.append(f"- Water: {st['water_glasses']} glasses ({st['water_per_day']:g}/day)")
    if st["posture_bad_pct"] is not None:
        md.append(f"- Slouching: {st['posture_bad_pct']}% of observed time")
    if st["sleep"]["avg_hours"]:
        md.append(f"- Overnight off the PC: {st['sleep']['avg_hours']:g} h avg, {st['sleep']['late_nights']} late nights")
    if st["nudges"]:
        md.append("- Nudges: " + ", ".join(f"{k} {v}" for k, v in st["nudges"].items()))
    if goals:
        md += ["", "## Goals", ""]
        for g in goals:
            md.append(f"- {'✅' if g['met'] else '⬜'} {g['name']}: {g['actual']:g} / {g['target']:g} {g['unit']} "
                      f"per {g['period']}{' (at most)' if g['direction'] == 'max' else ''}")
    return "\n".join(md) + "\n"


def report_due(cfg: dict, now: dt.datetime, last_week: str | None) -> str | None:
    """The ISO week ('2026-W48') whose report is due now, or None."""
    if not cfg.get("report", True):
        return None
    day = (cfg.get("report_day") or "sun")[:3].lower()
    wd = DAYS.index(day) if day in DAYS else 6
    try:
        h, m = (int(x) for x in (cfg.get("report_time") or "19:00").split(":")[:2])
    except Exception:  # noqa: BLE001
        h, m = 19, 0
    start, _ = week_bounds(now.date())
    due_at = dt.datetime.combine(start + dt.timedelta(days=wd), dt.time(h, m))
    if now >= due_at:
        y, w, _ = now.isocalendar()
        week = f"{y}-W{w:02d}"
        return None if week == last_week else week
    # PC was off at report time last week: catch up early this week (until Tuesday)
    prev = now - dt.timedelta(days=7)
    y, w, _ = prev.isocalendar()
    week = f"{y}-W{w:02d}"
    if now.weekday() <= 1 and last_week is not None and last_week < week:
        return week
    return None


SPEC = ToolSpec(
    "life",
    "The user's life log, habits, goals and weekly life report (all kept on this PC). "
    "action=log records something they did: what='water' (a glass of water) or a habit like 'exercise', 'reading', "
    "'meditation' (count = how many times, default 1). action=goal_set sets a target: name (a habit, 'water', 'sleep', "
    "'screen time' or a category: coding, meetings, social media, entertainment, documents, browsing), target, unit "
    "(times | hours | glasses), period (day | week), direction (min = at least, max = at most). action=goal_remove "
    "(name), action=goals lists them with this week's progress, action=stats gives totals for the last `days` days, "
    "action=report reads the weekly life report out loud now. Use it when they say 'I drank water', 'I went for a run', "
    "'I want to exercise 3 times a week', 'how was my week?', 'how much did I code this week?'.",
    {"action": {"type": "string", "description": "log | goal_set | goal_remove | goals | stats | report"},
     "what": {"type": "string", "description": "action=log: 'water' or a habit name"},
     "count": {"type": "integer", "description": "action=log: how many (default 1)"},
     "name": {"type": "string", "description": "goal name"},
     "target": {"type": "number"}, "unit": {"type": "string"}, "period": {"type": "string"},
     "direction": {"type": "string"},
     "days": {"type": "integer", "description": "action=stats: how many days back (default 7)"}},
    ["action"], group="main")


def stats_text(st: dict) -> str:
    lines = [f"{st['from']} to {st['to']}: {st['screen_hours']:g} h at the computer over {st['days_used']} days."]
    if st["categories"]:
        lines.append("By category: " + ", ".join(f"{c['name']} {c['hours']:g} h" for c in st["categories"]))
    if st["top_apps"]:
        lines.append("Top apps: " + ", ".join(f"{a['name']} {a['hours']:g} h" for a in st["top_apps"]))
    lines.append(f"Water: {st['water_glasses']} glasses ({st['water_per_day']:g}/day).")
    if st["habits"]:
        lines.append("Habits logged: " + ", ".join(f"{k} ×{v}" for k, v in st["habits"].items()))
    if st["posture_bad_pct"] is not None:
        lines.append(f"Slouching {st['posture_bad_pct']}% of observed time.")
    if st["sleep"]["avg_hours"]:
        lines.append(f"Overnight off the PC: {st['sleep']['avg_hours']:g} h on average, "
                     f"{st['sleep']['late_nights']} late nights.")
    if st["nudges"]:
        lines.append("Health nudges: " + ", ".join(f"{k} {v}" for k, v in st["nudges"].items()))
    return "\n".join(lines)


def parse_goal(a: dict) -> dict:
    name = (a.get("name") or a.get("what") or "").strip()
    if not name:
        raise ValueError("a goal needs a name")
    unit = (a.get("unit") or ("glasses" if _norm(name) == "water" else "hours" if _norm(name) in
                              ("sleep", "screen time") else "times")).lower()
    period = (a.get("period") or "week").lower()
    period = "day" if period.startswith("d") else "week"
    direction = "max" if str(a.get("direction") or "").lower() in ("max", "at most", "less", "under") else "min"
    return {"name": name, "target": float(a.get("target") or 1), "unit": unit, "period": period,
            "direction": direction, "source": goal_source(name, unit),
            "created": dt.date.today().isoformat()}


__all__ = ["LifeLog", "categorize", "app_name", "week_bounds", "goal_progress", "plain_report", "report_due", "SPEC",
           "load_goals", "save_goals", "parse_goal", "stats_text", "to_markdown", "report_prompt", "REPORT_SYSTEM",
           "ToolOutput"]
