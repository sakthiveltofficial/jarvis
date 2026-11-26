"""Nightly memory consolidation: keeps the knowledge graph sharp over months and years.

Once a night (default 03:00, or at the next start if the PC was off), and whenever you press
"Clean up now", Jarvis:

  1. Summarises each day that had conversations into a short digest (a :Digest node linked to the
     people and things it covers), so "what did we do last Tuesday?" has an answer even after the
     raw conversation snippets are pruned.
  2. Merges duplicates: entities that are the same thing under slightly different names
     ("Mom" / "mom", "Acme" / "Acme Corp") and facts that say the same thing twice.
  3. Lets stale facts fade: every fact has a strength that halves every `half_life_days` since it
     was last recalled or re-stated, boosted by how often it was used. Facts that drop below a
     threshold are marked outdated (reason "faded"), never silently lost. Identity facts (names,
     family, birthdays, where you live and work, allergies) never fade.
  4. Prunes: outdated history older than `keep_history_days`, raw conversation snippets older than
     30 days whose day has a digest, and orphan entities nothing points to any more.

Steps 2-4 work without an LLM. Step 1 and the smarter half of step 2 use the configured model
(or the cheaper `memory.model` if set) and are skipped, not fatal, when it isn't reachable.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
import time
import traceback
from typing import Callable

from . import character, config
from . import memory as gm

STATE_FILE = config.HOME / "consolidation.json"
FADE_BELOW = 0.2                  # strength under which a fact fades out
EPISODE_KEEP_DAYS = 30            # raw conversation snippets kept this long once their day is summarised
ORPHAN_GRACE_DAYS = 14
MAX_DAYS_PER_RUN = 14
# Facts about who someone is don't go stale just because nobody mentioned them for a while.
PROTECTED = re.compile(r"(name|born|birth|birthday|lives|live_in|home|works|employ|married|spouse|wife|husband|"
                       r"partner|sibling|brother|sister|parent|mother|father|mom|dad|son|daughter|child|"
                       r"allerg|blood|anniversary|nationality|native|speaks)", re.I)
_ART = re.compile(r"^(the|a|an|my|our)\s+", re.I)


def _parse(ts: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def strength(fact: dict, half_life_days: float, now: dt.datetime | None = None) -> float:
    """1.0 for a fresh fact; halves every half-life since it was last used; frequent use slows the fade."""
    now = now or dt.datetime.now()
    last = _parse(fact.get("reinforced") or "") or _parse(fact.get("created") or "") or now
    age = max(0.0, (now - last).total_seconds() / 86400)
    return (0.5 ** (age / max(1.0, half_life_days))) * (1 + math.log1p(int(fact.get("uses") or 0)))


def canonical(name: str) -> str:
    """Name key used to spot obvious duplicates: case, articles, punctuation and plurals ignored."""
    n = _ART.sub("", (name or "").strip().lower())
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    words = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in n.split()]
    return " ".join(words)


def _tokens(text: str) -> set[str]:
    return set(gm.keywords(text))


def similar(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 1.0 if a.strip().lower() == b.strip().lower() else 0.0
    return len(ta & tb) / len(ta | tb)


DIGEST_SYSTEM = """You write the daily diary of an AI companion called {assistant} about the person it assists ({user}).
Below are the snippets of {date}'s conversations. Write a short, factual summary of that day (2-5 sentences,
third person, past tense): what {user} worked on, decided, planned, felt, and anything worth remembering
later. No filler, no greetings, do not mention the assistant's tools.
Reply with JSON only: {{"summary": "...", "entities": ["names of the main people/projects/things involved"]}}"""

CLEANUP_SYSTEM = """You tidy a personal knowledge graph. You get (A) pairs of entities that might be the same
real-world thing and (B) groups of current facts about the same entities that might repeat each other.

For (A): list only the pairs that are definitely the same thing (same person, organisation, place, project...).
For (B): when several facts say the same thing, keep the best one (optionally reword it into one clear sentence
that keeps every detail) and drop the rest. When facts contradict, keep the newest and drop the older.
Facts that add different information must all be kept. When in doubt, keep.

Reply with JSON only:
{{"same": [["entity_id_to_keep", "entity_id_to_merge_into_it"]],
 "facts": [{{"keep": "fact_id", "drop": ["fact_id"], "text": "optional reworded fact"}}]}}"""


class Consolidator:
    def __init__(self, settings: dict, mem: gm.GraphMemory, emit: Callable[[dict], None] | None = None,
                 busy: Callable[[], bool] | None = None):
        self.settings = settings
        self.mem = mem
        self.emit = emit or (lambda ev: None)
        self.busy = busy or (lambda: False)
        self.state = self._load()
        self.running = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ settings + state
    @property
    def cfg(self) -> dict:
        return self.settings.get("memory") or {}

    def _load(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save(self):
        try:
            STATE_FILE.write_text(json.dumps(self.state, indent=1), "utf-8")
        except OSError:
            pass

    def next_run(self, now: dt.datetime | None = None) -> dt.datetime | None:
        if not self.cfg.get("consolidate", True):
            return None
        now = now or dt.datetime.now()
        try:
            hh, mm = (int(x) for x in str(self.cfg.get("consolidate_time") or "03:00").split(":")[:2])
        except ValueError:
            hh, mm = 3, 0
        last = _parse(self.state.get("last_run") or "")
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot > now:
            slot -= dt.timedelta(days=1)          # the latest slot that has already passed
        if last is None or last < slot:
            # Missed (PC was off or asleep at that time): run soon, once Jarvis is idle.
            return now if last is not None or self.mem.episodes() else slot + dt.timedelta(days=1)
        return slot + dt.timedelta(days=1)

    def status(self) -> dict:
        nr = self.next_run()
        return {"enabled": bool(self.cfg.get("consolidate", True)), "running": self.running,
                "last_run": self.state.get("last_run"), "last_report": self.state.get("last_report"),
                "next_run": nr.isoformat(timespec="minutes") if nr else None}

    # ------------------------------------------------------------------ background loop
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="consolidation")
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        if self._stop.wait(90):      # let startup (models, mic) settle first
            return
        while not self._stop.is_set():
            try:
                nr = self.next_run()
                if nr is not None and nr <= dt.datetime.now() and not self.busy():
                    self.run(manual=False)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            self._stop.wait(60)

    def run_async(self) -> bool:
        if self.running:
            return False
        threading.Thread(target=self.run, kwargs={"manual": True}, daemon=True, name="consolidation-now").start()
        return True

    # ------------------------------------------------------------------ the run
    def run(self, manual: bool = True, use_llm: bool = True, now: dt.datetime | None = None) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"skipped": "already running"}
        self.running = True
        now = now or dt.datetime.now()
        started = time.time()
        report = {"days_summarised": [], "entities_merged": 0, "facts_merged": 0, "facts_faded": 0,
                  "history_pruned": 0, "episodes_pruned": 0, "orphans_removed": 0, "notes": []}
        self.emit({"type": "memory_consolidating", "manual": manual})
        try:
            llm_ok = use_llm and self._llm_configured()
            if use_llm and not llm_ok:
                report["notes"].append("no model configured: skipped daily summaries and smart merging")
            for step in (self._summarise_days, self._merge_entities, self._merge_facts, self._fade, self._prune):
                try:
                    step(report, now, llm_ok)
                except Exception as e:  # noqa: BLE001  (one failing step must not stop the others)
                    traceback.print_exc()
                    report["notes"].append(f"{step.__name__.strip('_')}: {type(e).__name__}: {e}"[:200])
        finally:
            report["seconds"] = round(time.time() - started, 1)
            report["manual"] = manual
            report["finished"] = dt.datetime.now().isoformat(timespec="seconds")
            self.state["last_run"] = now.isoformat(timespec="seconds")
            self.state["last_report"] = report
            hist = self.state.setdefault("history", [])
            hist.append(report)
            del hist[:-30]
            self._save()
            self.running = False
            self._lock.release()
            self.mem._entities_cache = None
            self.emit({"type": "memory_consolidated", "report": report, "summary": describe(report)})
        return report

    def _llm_configured(self) -> bool:
        return llm_configured(self.settings)

    def _ask(self, system: str, prompt: str, max_tokens: int = 1500) -> dict:
        from . import llm
        raw = llm.complete(self.settings, system, prompt, max_tokens=max_tokens,
                           model=(self.cfg.get("model") or None))
        return gm.parse_json(raw)

    # 1. daily digests --------------------------------------------------------
    def _summarise_days(self, report: dict, now: dt.datetime, llm_ok: bool):
        if not llm_ok:
            return
        done = {d["date"]: d["created"] for d in self.mem.digests()}
        by_day: dict[str, list[dict]] = {}
        for ep in self.mem.episodes():
            by_day.setdefault(ep["created"][:10], []).append(ep)
        # A day needs a (new) digest if it has none, or got more conversation after its digest was written.
        todo = [d for d, eps in sorted(by_day.items())
                if d not in done or max(e["created"] for e in eps) > done[d]][-MAX_DAYS_PER_RUN:]
        ents = {e["id"]: e for e in self.mem.entities()}
        user = self.settings.get("user_name") or "the user"
        for day in todo:
            eps = by_day[day]
            text, total = [], 0
            for e in eps:
                chunk = f"[{e['created'][11:16]}] {e['text'][:1200]}"
                if total + len(chunk) > 14000:
                    break
                text.append(chunk)
                total += len(chunk)
            data = self._ask(DIGEST_SYSTEM.format(assistant=character.name(self.settings),
                                                  user=user, date=day), "\n\n".join(text), max_tokens=700)
            summary = str(data.get("summary") or "").strip()
            if not summary:
                continue
            linked = {i for e in eps for i in e["entities"] if i in ents}
            by_name = {e["name"].lower(): i for i, e in ents.items()}
            for n in data.get("entities") or []:
                i = by_name.get(str(n).lower()) or gm.slug(str(n))
                if i in ents:
                    linked.add(i)
            self.mem.save_digest(day, summary, sorted(linked))
            report["days_summarised"].append(day)

    # 2a. duplicate entities --------------------------------------------------
    def _merge_entities(self, report: dict, now: dt.datetime, llm_ok: bool):
        ents = self.mem.entities()
        groups: dict[str, list[dict]] = {}
        for e in ents:
            key = canonical(e["name"])
            if key:
                groups.setdefault(key, []).append(e)
        pairs: list[tuple[str, str]] = []
        for g in groups.values():
            if len(g) > 1:
                keep = self._pick_keeper(g)
                pairs += [(keep["id"], o["id"]) for o in g if o["id"] != keep["id"]]
        if llm_ok:
            pairs += self._llm_entity_pairs(ents, {o for _, o in pairs})
        merged = set()
        for keep, other in pairs:
            if keep in merged or other in merged or other == gm.USER_ID:
                continue
            if self.mem.merge_entities(keep, other) >= 0:
                merged.add(other)
                report["entities_merged"] += 1
        self.mem._entities_cache = None

    @staticmethod
    def _pick_keeper(group: list[dict]) -> dict:
        for e in group:
            if e["id"] == gm.USER_ID:
                return e
        return max(group, key=lambda e: (e["mentions"] or 0, len(e["summary"] or ""), -len(e["name"])))

    def _llm_entity_pairs(self, ents: list[dict], skip: set) -> list[tuple[str, str]]:
        """Near-miss names ("Acme" vs "Acme Corp", "Mum" vs "Mom") go to the model to decide."""
        ents = [e for e in ents if e["id"] not in skip and e["id"] != gm.USER_ID]
        cands = []
        for i, a in enumerate(ents):
            ca = canonical(a["name"])
            for b in ents[i + 1:]:
                cb = canonical(b["name"])
                if not ca or not cb or ca == cb:
                    continue
                contained = re.search(rf"\b{re.escape(ca)}\b", cb) or re.search(rf"\b{re.escape(cb)}\b", ca)
                if contained or similar(a["name"], b["name"]) >= 0.5 or \
                        (a["type"] == b["type"] and _edit_close(ca, cb)):
                    cands.append((a, b))
        if not cands:
            return []
        cands = cands[:40]
        lines = [f"- {a['id']}: {a['name']} ({a['type']}: {a['summary'][:80]})  <->  "
                 f"{b['id']}: {b['name']} ({b['type']}: {b['summary'][:80]})" for a, b in cands]
        data = self._ask(CLEANUP_SYSTEM, "(A) Possible duplicate entities:\n" + "\n".join(lines) + "\n\n(B) none",
                         max_tokens=800)
        ok = {(a["id"], b["id"]) for a, b in cands} | {(b["id"], a["id"]) for a, b in cands}
        byid = {e["id"]: e for e in ents}
        out = []
        for pair in data.get("same") or []:
            if isinstance(pair, list) and len(pair) == 2 and tuple(pair) in ok:
                keep = self._pick_keeper([byid[pair[0]], byid[pair[1]]])
                out.append((keep["id"], pair[1] if keep["id"] == pair[0] else pair[0]))
        return out

    # 2b. duplicate facts -----------------------------------------------------
    def _merge_facts(self, report: dict, now: dt.datetime, llm_ok: bool):
        facts = self.mem.facts()
        by_pair: dict[tuple, list[dict]] = {}
        for f in facts:
            by_pair.setdefault(tuple(sorted((f["source"], f["target"]))), []).append(f)
        dropped: set[str] = set()
        llm_groups = []
        for group in by_pair.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda f: (f["reinforced"] or f["created"]), reverse=True)
            for i, a in enumerate(group):
                if a["id"] in dropped:
                    continue
                for b in group[i + 1:]:
                    if b["id"] in dropped:
                        continue
                    if a["fact"].strip().lower() == b["fact"].strip().lower() or similar(a["fact"], b["fact"]) >= 0.8:
                        self._fold(a, b)
                        dropped.add(b["id"])
                        report["facts_merged"] += 1
            rest = [f for f in group if f["id"] not in dropped]
            if len(rest) >= 2:
                llm_groups.append(rest[:8])
        if llm_ok and llm_groups:
            self._llm_merge_facts(llm_groups[:25], report)

    def _fold(self, keep: dict, drop: dict):
        self.mem.invalidate(drop["id"], "merged")
        self.mem.reinforce([keep["id"]], by=int(drop.get("uses") or 0) + 1)

    def _llm_merge_facts(self, groups: list[list[dict]], report: dict):
        ents = {e["id"]: e["name"] for e in self.mem.entities()}
        blocks = []
        for i, g in enumerate(groups, 1):
            rows = "\n".join(f"  - {f['id']} ({f['created'][:10]}): {ents.get(f['source'], f['source'])} "
                             f"—{f['relation']}→ {ents.get(f['target'], f['target'])}: {f['fact']}" for f in g)
            blocks.append(f"Group {i}:\n{rows}")
        data = self._ask(CLEANUP_SYSTEM, "(A) none\n\n(B) Fact groups:\n" + "\n".join(blocks), max_tokens=1500)
        valid = {f["id"]: f for g in groups for f in g}
        for item in data.get("facts") or []:
            if not isinstance(item, dict):
                continue
            keep = valid.get(str(item.get("keep")))
            if not keep:
                continue
            drops = [valid[d] for d in item.get("drop") or [] if d in valid and d != keep["id"]]
            if not drops:
                continue
            for d in drops:
                self._fold(keep, d)
                report["facts_merged"] += 1
            text = str(item.get("text") or "").strip()
            if text and 10 <= len(text) <= 400:
                self.mem.set_fact_text(keep["id"], text)

    # 3. fade -----------------------------------------------------------------
    def _fade(self, report: dict, now: dt.datetime, llm_ok: bool):
        half = float(self.cfg.get("half_life_days") or 120)
        if half <= 0:      # 0 = never fade
            return
        for f in self.mem.facts():
            if PROTECTED.search(f["relation"] or ""):
                continue
            if strength(f, half, now) < FADE_BELOW:
                if self.mem.invalidate(f["id"], "faded"):
                    report["facts_faded"] += 1

    # 4. prune ----------------------------------------------------------------
    def _prune(self, report: dict, now: dt.datetime, llm_ok: bool):
        keep_days = int(self.cfg.get("keep_history_days") or 0)
        if keep_days > 0:
            cutoff = (now - dt.timedelta(days=keep_days)).isoformat()
            old = [f["id"] for f in self.mem.facts(valid_only=False)
                   if not f["valid"] and f["invalidated"] and f["invalidated"] < cutoff]
            self.mem.delete_facts(old)
            report["history_pruned"] += len(old)
        digested = {d["date"] for d in self.mem.digests()}
        ep_cut = (now - dt.timedelta(days=EPISODE_KEEP_DAYS)).isoformat()
        old_eps = [e["id"] for e in self.mem.episodes() if e["created"] < ep_cut and e["created"][:10] in digested]
        self.mem.delete_episodes(old_eps)
        report["episodes_pruned"] += len(old_eps)
        # Orphans: entities no fact, digest or file points to, rarely mentioned, and not touched lately.
        linked = {n for f in self.mem.facts(valid_only=False) for n in (f["source"], f["target"])}
        linked |= {r[0] for r in self.mem.q("MATCH (:Digest)-[:Covers]->(e:Entity) RETURN DISTINCT e.id")}
        try:
            linked |= {r[0] for r in self.mem.q("MATCH (:Document)-[:About]->(e:Entity) RETURN DISTINCT e.id")}
        except Exception:  # noqa: BLE001  (file index tables not created yet)
            pass
        grace = (now - dt.timedelta(days=ORPHAN_GRACE_DAYS)).isoformat()
        for e in self.mem.entities():
            if e["id"] == gm.USER_ID or e["id"] in linked:
                continue
            if (e["mentions"] or 0) <= 2 and (e["updated"] or "") < grace:
                self.mem.forget(e["id"])
                report["orphans_removed"] += 1


def llm_configured(settings: dict) -> bool:
    """Is there a model we can call in the background (API key set, or a local server)?"""
    llm = settings.get("llm") or {}
    kind = llm.get("kind") or config.LLM_PRESETS.get(llm.get("provider"), {}).get("kind", "openai")
    if kind == "anthropic":
        return bool(config.api_key(settings, "anthropic"))
    base = llm.get("base_url") or ""
    return bool(llm.get("model")) and (bool(config.api_key(settings, llm.get("provider", "custom")))
                                       or "localhost" in base or "127.0.0.1" in base)


def _edit_close(a: str, b: str) -> bool:
    """One small typo apart ("mom"/"mum", "jon"/"john") on short names."""
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 3 or max(len(a), len(b)) > 14:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    s, t = (a, b) if len(a) < len(b) else (b, a)
    return any(t[:i] + t[i + 1:] == s for i in range(len(t)))


def describe(r: dict) -> str:
    bits = []
    if r.get("days_summarised"):
        n = len(r["days_summarised"])
        bits.append(f"summarised {n} day{'s' if n != 1 else ''}")
    for key, label in (("entities_merged", "merged {} duplicate entit{}"), ("facts_merged", "merged {} repeated fact{}"),
                       ("facts_faded", "faded {} stale fact{}"), ("history_pruned", "pruned {} old history entr{}"),
                       ("orphans_removed", "removed {} orphan entit{}")):
        n = r.get(key) or 0
        if n:
            plural = ("ies" if n != 1 else "y") if label.endswith("entit{}") or label.endswith("entr{}") else \
                ("s" if n != 1 else "")
            bits.append(label.format(n, plural))
    text = ", ".join(bits) if bits else "everything was already tidy"
    return text[0].upper() + text[1:] + "."
