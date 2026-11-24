"""Long-term graph memory.

Stored in an embedded graph database (LadybugDB, the maintained fork of Kùzu; Cypher queries,
single folder on disk, no server). Falls back to the `kuzu` package if that's what is installed.

Model
  (:Entity {id, name, type, summary, created, updated, mentions})
  (:Entity)-[:Fact {id, relation, fact, created, valid, invalidated}]->(:Entity)
  (:Episode {id, text, created})-[:Mentions]->(:Entity)

Facts are never deleted when something changes: the old fact is marked valid=false with an
`invalidated` timestamp and the new fact is added, so the graph keeps a timeline that works
over months and years.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import threading
import uuid

from . import config

try:  # LadybugDB (actively maintained Kùzu fork)
    import real_ladybug as gdb
except ImportError:  # pragma: no cover
    import kuzu as gdb  # type: ignore

USER_ID = "user"
DB_PATH = config.HOME / "graph.db"

STOP = set("""a an the and or but if then so to of in on at for from by with about into over after before
is are was were be been being am do does did done have has had having i me my mine you your yours we our
he she it they them their this that these those what which who whom whose when where why how can could
would should will shall may might must not no yes please just tell know remember jarvis hey okay ok like
some any all more most very really there here than also too get got make made want need let lets thing things""".split())


def now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return s[:80] or uuid.uuid4().hex[:8]


def keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]{1,}", text.lower())
    out = []
    for w in words:
        w = w.strip("'-")
        if len(w) < 3 or w in STOP:
            continue
        for suf in ("'s", "ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: -len(suf)]
                break
        out.append(w)
    return list(dict.fromkeys(out))


class GraphMemory:
    def __init__(self, path=DB_PATH):
        self._lock = threading.RLock()
        self.db = gdb.Database(str(path))
        self.conn = gdb.Connection(self.db)
        self._entities_cache = None
        self._schema()

    # ------------------------------------------------------------------ low level
    def q(self, cypher: str, params: dict | None = None) -> list[list]:
        with self._lock:
            r = self.conn.execute(cypher, params or {})
            rows = []
            while r.has_next():
                rows.append(r.get_next())
            return rows

    def _schema(self):
        self.q("CREATE NODE TABLE IF NOT EXISTS Entity(id STRING, name STRING, type STRING, summary STRING, "
               "created STRING, updated STRING, mentions INT64, PRIMARY KEY(id))")
        self.q("CREATE NODE TABLE IF NOT EXISTS Episode(id STRING, text STRING, created STRING, PRIMARY KEY(id))")
        self.q("CREATE REL TABLE IF NOT EXISTS Fact(FROM Entity TO Entity, id STRING, relation STRING, fact STRING, "
               "created STRING, valid BOOLEAN, invalidated STRING)")
        self.q("CREATE REL TABLE IF NOT EXISTS Mentions(FROM Episode TO Entity)")

    def ensure_user(self, name: str):
        name = name or "User"
        self.q("MERGE (u:Entity {id: $id}) ON CREATE SET u.name=$n, u.type='Person', u.summary='The person I assist.', "
               "u.created=$t, u.updated=$t, u.mentions=0 ON MATCH SET u.name=$n", {"id": USER_ID, "n": name, "t": now()})
        self._entities_cache = None

    # ------------------------------------------------------------------ writes
    def upsert_entity(self, name: str, etype: str = "Thing", summary: str = "") -> str:
        eid = USER_ID if name.strip().upper() in ("USER", "ME", "I") else slug(name)
        t = now()
        self.q("MERGE (e:Entity {id: $id}) "
               "ON CREATE SET e.name=$n, e.type=$ty, e.summary=$s, e.created=$t, e.updated=$t, e.mentions=1 "
               "ON MATCH SET e.updated=$t, e.mentions=e.mentions+1",
               {"id": eid, "n": name.strip(), "ty": etype or "Thing", "s": summary or "", "t": t})
        if summary and eid != USER_ID:
            self.q("MATCH (e:Entity {id:$id}) SET e.summary=$s", {"id": eid, "s": summary})
        self._entities_cache = None
        return eid

    def add_fact(self, src: str, relation: str, dst: str, fact: str) -> str | None:
        sid, did = self.upsert_entity(src), self.upsert_entity(dst)
        rel = slug(relation) or "related_to"
        # skip exact duplicates that are still valid
        dup = self.q("MATCH (a:Entity {id:$a})-[f:Fact]->(b:Entity {id:$b}) WHERE f.valid AND f.relation=$r "
                     "AND lower(f.fact)=lower($fact) RETURN f.id", {"a": sid, "b": did, "r": rel, "fact": fact})
        if dup:
            return None
        fid = uuid.uuid4().hex[:10]
        self.q("MATCH (a:Entity {id:$a}), (b:Entity {id:$b}) CREATE (a)-[:Fact {id:$id, relation:$r, fact:$f, "
               "created:$t, valid:true, invalidated:''}]->(b)",
               {"a": sid, "b": did, "id": fid, "r": rel, "f": fact, "t": now()})
        return fid

    def invalidate(self, fact_id: str) -> bool:
        rows = self.q("MATCH ()-[f:Fact {id:$id}]->() WHERE f.valid SET f.valid=false, f.invalidated=$t RETURN f.id",
                      {"id": fact_id, "t": now()})
        return bool(rows)

    def add_episode(self, text: str, entity_ids: list[str]) -> str:
        eid = uuid.uuid4().hex[:10]
        self.q("CREATE (:Episode {id:$id, text:$x, created:$t})", {"id": eid, "x": text[:2000], "t": now()})
        for n in set(entity_ids):
            self.q("MATCH (p:Episode {id:$p}), (e:Entity {id:$e}) CREATE (p)-[:Mentions]->(e)", {"p": eid, "e": n})
        return eid

    def apply(self, data: dict, episode: str) -> dict:
        """Apply an extraction result. Returns the touched node and edge ids."""
        nodes, edges, removed = [], [], []
        for e in data.get("entities", []) or []:
            if e.get("name"):
                nodes.append(self.upsert_entity(e["name"], e.get("type", "Thing"), e.get("summary", "")))
        for fid in data.get("invalidate", []) or []:
            if self.invalidate(str(fid)):
                removed.append(str(fid))
        for f in data.get("facts", []) or []:
            if f.get("source") and f.get("target") and f.get("fact"):
                fid = self.add_fact(f["source"], f.get("relation", "related_to"), f["target"], f["fact"])
                nodes += [USER_ID if f["source"].upper() in ("USER", "ME") else slug(f["source"]),
                          USER_ID if f["target"].upper() in ("USER", "ME") else slug(f["target"])]
                if fid:
                    edges.append(fid)
        if nodes or edges:
            self.add_episode(episode, nodes)
        return {"nodes": list(dict.fromkeys(nodes)), "edges": edges, "invalidated": removed}

    def forget(self, entity_id: str):
        self.q("MATCH (e:Entity {id:$id}) DETACH DELETE e", {"id": entity_id})
        self._entities_cache = None

    def clear(self):
        self.q("MATCH (e:Episode) DETACH DELETE e")
        self.q("MATCH (e:Entity) WHERE e.id <> $u DETACH DELETE e", {"u": USER_ID})
        self._entities_cache = None

    # ------------------------------------------------------------------ reads
    def entities(self) -> list[dict]:
        if self._entities_cache is None:
            rows = self.q("MATCH (e:Entity) RETURN e.id, e.name, e.type, e.summary, e.updated, e.mentions")
            self._entities_cache = [dict(id=r[0], name=r[1], type=r[2], summary=r[3] or "", updated=r[4] or "",
                                         mentions=r[5] or 0) for r in rows]
        return self._entities_cache

    def facts(self, valid_only=True, around: list[str] | None = None) -> list[dict]:
        where = ["f.valid"] if valid_only else []
        params = {}
        if around is not None:
            where.append("(a.id IN $ids OR b.id IN $ids)")
            params["ids"] = around
        cy = "MATCH (a:Entity)-[f:Fact]->(b:Entity)"
        if where:
            cy += " WHERE " + " AND ".join(where)
        cy += " RETURN f.id, a.id, f.relation, b.id, f.fact, f.created, f.valid, f.invalidated"
        return [dict(id=r[0], source=r[1], relation=r[2], target=r[3], fact=r[4], created=r[5], valid=r[6],
                     invalidated=r[7]) for r in self.q(cy, params)]

    def search(self, query: str, limit: int = 12) -> dict:
        """Lexical seed match on entity names/summaries/facts, then 1-hop graph expansion."""
        kws = keywords(query)
        about_user = bool(re.search(r"\b(i|me|my|mine|myself)\b", query, re.I))
        ents = {e["id"]: e for e in self.entities()}
        score: dict[str, float] = {}
        for e in ents.values():
            name, summ = e["name"].lower(), e["summary"].lower()
            s = 0.0
            for k in kws:
                if re.search(rf"\b{re.escape(k)}", name):
                    s += 3
                elif k in summ:
                    s += 1
            if s:
                score[e["id"]] = s + min(e["mentions"], 10) * 0.05
        all_facts = self.facts()
        fact_hits = []
        for f in all_facts:
            txt = f"{f['fact']} {f['relation']}".lower()
            hits = sum(1 for k in kws if k in txt)
            if hits:
                fact_hits.append((hits, f))
                for n in (f["source"], f["target"]):
                    score[n] = score.get(n, 0) + 0.5 * hits
        if about_user and USER_ID in ents:
            score[USER_ID] = score.get(USER_ID, 0) + 0.5
        seeds = [n for n, _ in sorted(score.items(), key=lambda x: -x[1])[:6]]
        chosen = {f["id"]: f for _, f in sorted(fact_hits, key=lambda x: -x[0])[:limit]}
        if seeds:
            for f in all_facts:
                if len(chosen) >= limit * 2:
                    break
                if f["source"] in seeds or f["target"] in seeds:
                    # around the user node, only keep facts that also match the query
                    if USER_ID in (f["source"], f["target"]) and f["id"] not in chosen and \
                            not ({f["source"], f["target"]} - {USER_ID}) & set(seeds):
                        continue
                    chosen.setdefault(f["id"], f)
        facts = list(chosen.values())[: limit * 2]
        node_ids = list(dict.fromkeys(seeds + [n for f in facts for n in (f["source"], f["target"])]))
        lines = []
        for f in facts:
            a, b = ents.get(f["source"], {}).get("name", f["source"]), ents.get(f["target"], {}).get("name", f["target"])
            lines.append(f"- [{f['id']}] {a} —{f['relation']}→ {b}: {f['fact']} (noted {f['created'][:10]})")
        for n in seeds:
            e = ents.get(n)
            if e and e["summary"] and n != USER_ID:
                lines.append(f"- {e['name']} ({e['type']}): {e['summary']}")
        return {"query": query, "nodes": node_ids, "edges": [f["id"] for f in facts], "text": "\n".join(lines)}

    def user_profile(self, limit: int = 20) -> str:
        ents = {e["id"]: e for e in self.entities()}
        facts = sorted(self.facts(around=[USER_ID]), key=lambda f: f["created"], reverse=True)[:limit]
        out = []
        for f in facts:
            a, b = ents.get(f["source"], {}).get("name", "?"), ents.get(f["target"], {}).get("name", "?")
            out.append(f"- {f['fact']}  [{a} —{f['relation']}→ {b}]")
        return "\n".join(out)

    def graph(self, include_invalid: bool = False) -> dict:
        ents = self.entities()
        return {"nodes": ents, "edges": self.facts(valid_only=not include_invalid)}

    def import_legacy(self):
        """One-time import of the v1 memory.md bullet list."""
        f = config.MEMORY_FILE
        done = config.HOME / ".memory_imported"
        if done.exists() or not f.exists():
            return
        for line in f.read_text("utf-8").splitlines():
            line = line.strip("-• \t")
            if line:
                self.add_fact("USER", "noted", "Notes", line)
        done.write_text("ok")


# ---------------------------------------------------------------------- extraction
EXTRACT_SYSTEM = """You maintain a long-term personal knowledge graph for an AI companion about the person it assists.
From the latest conversation turn, extract only durable, useful knowledge: facts about the user, their
preferences, opinions, habits, goals, plans and deadlines, people in their life and their relationships,
places, organisations, projects, devices and accounts, important events and dates. Also capture explicit
"remember this" requests and corrections.

Rules
- Refer to the user as "USER". Use clear canonical names for everything else ("Mom", "Webnox", "Jarvis project").
- type is one of: Person, Organization, Place, Project, Preference, Event, Task, Thing, Concept, Device, Skill.
- relation is a short snake_case verb phrase (works_at, likes, lives_in, sister_of, deadline_on, uses).
- fact is one self-contained sentence in third person, with dates when known (today is {today}).
- If the turn updates or contradicts an existing fact listed below, put that fact's id in "invalidate"
  and add the corrected fact.
- Skip small talk, the assistant's own actions, transient states and tool output unless they matter later.
- If nothing is worth remembering, return empty lists.

Existing related facts:
{existing}

Reply with JSON only, exactly this shape:
{{"entities":[{{"name":"","type":"","summary":""}}],
 "facts":[{{"source":"","relation":"","target":"","fact":""}}],
 "invalidate":["fact_id"]}}"""


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def extract(settings: dict, mem: GraphMemory, user_text: str, assistant_text: str) -> dict:
    from . import llm
    related = mem.search(user_text + " " + assistant_text, limit=15)["text"] or "(none)"
    system = EXTRACT_SYSTEM.format(today=_dt.date.today().isoformat(), existing=related)
    turn = f"USER said: {user_text}\n\nASSISTANT replied: {assistant_text[:1500]}"
    raw = llm.complete(settings, system, turn, max_tokens=1500)
    return parse_json(raw)
