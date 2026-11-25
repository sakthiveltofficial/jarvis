"""AR companion: what the phone's camera sees.

The Android app's AR tab (ARCore) talks to this module over the paired, encrypted phone link (phones.py) with
request/response frames: {"t": "req", "id", "op", "args"} -> {"t": "resp", "id", "ok", "data" | "error"}.

  look          "What am I looking at?": a camera frame plus a typed or spoken question goes to the main agent,
                which sees the photo and can answer or act on it (the reply comes back as normal chat + voice).
  sighting      "Remember what I see": a frame every so often; the vision model lists the objects in it and where
                they are. Kept for two weeks so "where did I leave my keys?" can be answered (object finder).
  find          Object finder search over those sightings (also the agent's `spatial` tool).
  scan_devices  Smart-home overlay: which Home Assistant devices are in view, with their boxes in the frame.
  ha_call       A tap on a floating control (toggle, brightness, temperature, volume…).
  ha_states     Live state for the controls on screen.
  notes_sync    Spatial notes pinned in the room (the phone keeps the anchors; Jarvis keeps the text so it can
                talk about them).
  note_seen     The phone found a pinned note again.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import json
import re
import threading
import time
import uuid

from . import config, homeassistant, llm
from .tools import ToolOutput, ToolSpec

DIR = config.HOME / "ar"
THUMBS = DIR / "thumbs"
SIGHTINGS_FILE = DIR / "sightings.json"
NOTES_FILE = DIR / "notes.json"
KEEP_DAYS = 14
MAX_SIGHTINGS = 3000
OVERLAY_DOMAINS = ("light", "switch", "fan", "climate", "media_player", "cover", "humidifier", "input_boolean")
STATE_ATTRS = ("brightness", "color_temp_kelvin", "current_temperature", "temperature", "hvac_mode", "hvac_modes",
               "fan_mode", "volume_level", "is_volume_muted", "source", "media_title", "percentage",
               "current_position", "min_temp", "max_temp", "target_temp_step", "unit_of_measurement")

SIGHTING_SYSTEM = (
    "You catalogue what a home camera sees so the owner can later ask where they left things. Reply with JSON only.")
SIGHTING_PROMPT = (
    "List the distinct, nameable physical objects in this photo that someone might later look for or ask about "
    "(keys, wallet, phone, glasses, remote, charger, headphones, bag, bottle, mug, book, tools, documents, "
    "medicine, plants, devices…). Skip walls, floors and other structure. For each give a short name, where it is "
    "relative to nearby things (e.g. 'on the desk, left of the laptop'), and its bounding box as fractions of the "
    "image [x0, y0, x1, y1]. Also name the scene in a few words (e.g. 'home office desk', 'kitchen counter').\n"
    'Format: {"scene": "...", "objects": [{"name": "...", "where": "...", "box": [0.1, 0.2, 0.3, 0.4]}]}\n'
    "At most 25 objects. An empty list is fine.")
DEVICES_SYSTEM = "You match smart-home devices to what a phone camera sees. Reply with JSON only."

SYNONYMS = {
    "key": {"keys", "keychain", "key ring", "car key"}, "phone": {"smartphone", "mobile", "cellphone"},
    "glasses": {"spectacles", "sunglasses", "eyeglasses"}, "wallet": {"purse", "card holder"},
    "remote": {"remote control", "tv remote", "controller"}, "charger": {"cable", "charging cable", "adapter"},
    "headphones": {"earphones", "earbuds", "airpods", "headset"}, "bag": {"backpack", "handbag", "purse"},
    "laptop": {"notebook computer", "macbook"}, "mug": {"cup"}, "bottle": {"water bottle", "flask"},
    "medicine": {"pills", "tablets", "medication"}, "watch": {"smartwatch", "wristwatch"},
}
STOP = {"where", "did", "i", "leave", "my", "the", "a", "an", "is", "are", "was", "were", "have", "you", "seen",
        "find", "last", "put", "of", "me", "please", "can", "could", "jarvis", "see", "saw", "lost", "left", "it",
        "them", "to", "for", "any", "in", "on", "at"}


def _now() -> float:
    return time.time()


def ago(ts: float) -> str:
    d = max(0, _now() - ts)
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        h = int(d // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    days = int(d // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


def _singular(w: str) -> str:
    if w in ("glasses", "headphones", "pills", "earphones", "sunglasses", "eyeglasses"):
        return w
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _words(text: str) -> list[str]:
    return [_singular(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower())]


def query_terms(word: str) -> tuple[set[str], set[str]]:
    """Words and phrases that name the same thing: 'keys' -> ({'key', 'keychain'}, {'key ring', 'car key'})."""
    w = _singular(word)
    words, phrases = {w}, set()
    for base, alts in SYNONYMS.items():
        group = {_singular(base)} | {" ".join(_words(a)) for a in alts}
        if w in group:
            for g in group:
                (phrases if " " in g else words).add(g)
    return words, phrases


def matches(query: str, name: str) -> float:
    """0..1 how well an object name answers the query. The head noun (last word: 'car keys' -> key) must match."""
    head = [w for w in _words(query) if w not in STOP]
    words = _words(name)
    if not head or not words:
        return 0.0
    syn, phrases = query_terms(head[-1])
    joined = " ".join(words)
    if not (set(words) & syn or any(p in joined for p in phrases)):
        return 0.0
    extra = set(head[:-1])
    return min(1.0, 0.7 + (0.3 * len(set(words) & extra) / len(extra) if extra else 0.3))


def to_png(b64img: str, max_side: int = 1280) -> str:
    """JPEG (or anything PIL reads) -> PNG base64, downscaled; the vision code paths expect PNG."""
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64img))).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def thumb_jpeg(b64img: str, max_side: int = 480) -> bytes:
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64img))).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return buf.getvalue()


def _box(v) -> list[float] | None:
    try:
        b = [max(0.0, min(1.0, float(x))) for x in v][:4]
    except (TypeError, ValueError):
        return None
    if len(b) != 4:
        return None
    if b[2] < b[0]:
        b[0], b[2] = b[2], b[0]
    if b[3] < b[1]:
        b[1], b[3] = b[3], b[1]
    return [round(x, 4) for x in b]


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


class ARHub:
    def __init__(self, session):
        self.s = session
        self._lock = threading.Lock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="ar")
        DIR.mkdir(parents=True, exist_ok=True)
        THUMBS.mkdir(parents=True, exist_ok=True)
        self.sightings: list[dict] = self._load(SIGHTINGS_FILE, [])
        self.notes: dict[str, list[dict]] = self._load(NOTES_FILE, {})
        self._states_at = 0.0

    # ------------------------------------------------------------------ storage
    @staticmethod
    def _load(path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default

    def _save_sightings(self):
        cutoff = _now() - KEEP_DAYS * 86400
        keep = [s for s in self.sightings if s.get("at", 0) >= cutoff][-MAX_SIGHTINGS:]
        gone = {s["id"] for s in self.sightings} - {s["id"] for s in keep}
        self.sightings = keep
        for sid in gone:
            try:
                (THUMBS / f"{sid}.jpg").unlink(missing_ok=True)
            except OSError:
                pass
        SIGHTINGS_FILE.write_text(json.dumps(self.sightings), encoding="utf-8")

    def _save_notes(self):
        NOTES_FILE.write_text(json.dumps(self.notes), encoding="utf-8")

    # ------------------------------------------------------------------ dispatch
    def handle(self, device: dict, op: str, a: dict) -> dict:
        fn = {
            "look": self.look, "sighting": self.sighting, "find": self.find_api, "scan_devices": self.scan_devices,
            "ha_call": self.ha_call, "ha_states": self.ha_states, "notes_sync": self.notes_sync,
            "note_seen": self.note_seen, "status": self.status,
        }.get(op or "")
        if fn is None:
            raise ValueError(f"Unknown AR request: {op}")
        return fn(device or {}, a or {})

    def _vision(self) -> bool:
        return bool(getattr(self.s.provider, "vision", False)) and bool(self.s.settings["llm"].get("vision", True))

    def status(self, device: dict, a: dict) -> dict:
        return {"vision": self._vision(), "home": homeassistant.configured(self.s.settings),
                "sightings": len(self.sightings), "notes": sum(len(v) for v in self.notes.values())}

    # ------------------------------------------------------------------ look
    def look(self, device: dict, a: dict) -> dict:
        if not self._vision():
            raise RuntimeError("Your AI model can't see images. Pick a model with vision in Jarvis ⚙ → Brain.")
        png = to_png(a.get("jpeg") or "")
        origin = device.get("id") if a.get("speak", True) else "-"
        note = ("The photo is what the user is pointing their phone camera at right now (Jarvis AR). Answer about "
                "what's in it, briefly, as if you can see it with them. If it shows a problem (an error message, a "
                "broken thing, a document to act on), explain it and offer to handle it with your tools.")
        if a.get("place"):
            note += f" They're in: {a['place']}."
        if a.get("wav"):
            self.s.phone_utterance(base64.b64decode(a["wav"]), origin, images=[png], context=note)
            return {"ok": True, "queued": True}
        text = (a.get("question") or "").strip() or "What am I looking at?"
        asyncio.run_coroutine_threadsafe(self.s.handle_user(text, origin=origin, images=[png], context=note),
                                         self.s.loop)
        return {"ok": True, "queued": True}

    # ------------------------------------------------------------------ sightings / object finder
    def sighting(self, device: dict, a: dict) -> dict:
        if not self._vision():
            return {"ok": False, "objects": [], "error": "vision model needed"}
        img = a.get("jpeg") or ""
        png = to_png(img, 1024)
        raw = llm.complete(self.s.settings, SIGHTING_SYSTEM, SIGHTING_PROMPT, max_tokens=1200, images=[png])
        data = parse_json(raw)
        objs = []
        for o in (data.get("objects") or [])[:25]:
            if not isinstance(o, dict) or not str(o.get("name") or "").strip():
                continue
            objs.append({"name": str(o["name"]).strip()[:60], "where": str(o.get("where") or "").strip()[:140],
                         "box": _box(o.get("box"))})
        sid = uuid.uuid4().hex[:12]
        rec = {"id": sid, "at": _now(), "device": device.get("name") or "phone", "place": str(a.get("place") or "")[:60],
               "scene": str(data.get("scene") or "")[:80], "objects": objs}
        if objs:
            try:
                (THUMBS / f"{sid}.jpg").write_bytes(thumb_jpeg(img))
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self.sightings.append(rec)
                self._save_sightings()
        return {"ok": True, "id": sid, "scene": rec["scene"], "objects": objs}

    def find(self, query: str, limit: int = 5) -> list[dict]:
        hits = []
        with self._lock:
            rows = list(self.sightings)
        for s in reversed(rows):
            for o in s.get("objects") or []:
                sc = matches(query, o.get("name", ""))
                if sc >= 0.6:
                    hits.append({"object": o["name"], "where": o.get("where", ""), "box": o.get("box"),
                                 "scene": s.get("scene", ""), "place": s.get("place", ""), "at": s["at"],
                                 "ago": ago(s["at"]), "sighting": s["id"], "device": s.get("device", ""),
                                 "score": sc})
        # most recent first, but a much better name match wins within the same few minutes
        hits.sort(key=lambda h: (-(h["at"] // 300), -h["score"]))
        out, seen = [], set()
        for h in hits:
            key = (h["object"].lower(), h["sighting"])
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
            if len(out) >= limit:
                break
        return out

    def thumb_b64(self, sid: str) -> str:
        p = THUMBS / f"{sid}.jpg"
        return base64.b64encode(p.read_bytes()).decode() if p.is_file() else ""

    def find_api(self, device: dict, a: dict) -> dict:
        hits = self.find(a.get("query") or "", int(a.get("limit") or 5))
        if hits:
            hits[0]["thumb"] = self.thumb_b64(hits[0]["sighting"])
        return {"query": a.get("query") or "", "hits": hits, "text": self.describe_hits(a.get("query") or "", hits)}

    @staticmethod
    def describe_hits(query: str, hits: list[dict]) -> str:
        if not hits:
            return (f"I haven't seen “{query}” through your phone's camera yet. Turn on “Remember what I see” in the "
                    "AR tab and look around now and then, and I'll know next time.")
        h = hits[0]
        where = h["where"] or h["scene"] or "in view"
        place = f" in the {h['place']}" if h.get("place") and h["place"].lower() not in where.lower() else ""
        line = f"Last seen {h['ago']}: {h['object']}, {where}{place}."
        older = [f"{x['ago']}: {x['where'] or x['scene']}" for x in hits[1:3]]
        return line + (f" Before that: {'; '.join(older)}." if older else "")

    # ------------------------------------------------------------------ smart home overlay
    def _home(self):
        if not homeassistant.configured(self.s.settings):
            raise RuntimeError("Home Assistant isn't connected. Set it up in Jarvis ⚙ → Connections → Smart home.")
        return self.s.home

    def _state_row(self, eid: str, states: list[dict] | None = None) -> dict | None:
        home = self.s.home
        states = states if states is not None else home.states()
        full = next((s for s in states if s.get("entity_id") == eid), None)
        if not full:
            return None
        attrs = full.get("attributes") or {}
        row = {"entity_id": eid, "name": attrs.get("friendly_name") or eid, "domain": eid.split(".")[0],
               "state": full.get("state"), "area": home._areas.get(eid, ""),
               "attrs": {k: attrs[k] for k in STATE_ATTRS if k in attrs}}
        b = attrs.get("brightness")
        if isinstance(b, (int, float)):
            row["attrs"]["brightness_pct"] = round(b / 255 * 100)
        return row

    def scan_devices(self, device: dict, a: dict) -> dict:
        home = self._home()
        if not self._vision():
            raise RuntimeError("Your AI model can't see images, so I can't spot devices in the camera view.")
        devs = [d for d in home.devices() if d["domain"] in OVERLAY_DOMAINS][:150]
        if not devs:
            return {"devices": [], "text": "No lights, AC, TV or other controllable devices in Home Assistant."}
        place = str(a.get("place") or "").strip()
        lines = "\n".join(f"{d['entity_id']} | {d['name']} | {d['area'] or '-'} | {d['state']}" for d in devs)
        prompt = (
            "Smart-home devices in this home (entity_id | name | area | state):\n" + lines + "\n\n"
            + (f"The user says they're in: {place}.\n" if place else "")
            + "Which of these devices can you see in the photo? Use the names, areas and states (a light that's on "
            "glows; a TV that's on shows a picture) to tell similar devices apart; prefer devices in the user's "
            "area. Only include a device when you can point at it. Give its bounding box as fractions of the "
            "image [x0, y0, x1, y1] and a confidence 0-1.\n"
            'Format: {"devices": [{"entity_id": "light.desk_lamp", "box": [0.1, 0.2, 0.3, 0.5], "confidence": 0.8}]}')
        raw = llm.complete(self.s.settings, DEVICES_SYSTEM, prompt, max_tokens=800, images=[to_png(a.get("jpeg") or "", 1024)])
        data = parse_json(raw)
        known = {d["entity_id"] for d in devs}
        states = home.states()
        out = []
        for d in data.get("devices") or []:
            if not isinstance(d, dict) or d.get("entity_id") not in known:
                continue
            row = self._state_row(d["entity_id"], states)
            if not row:
                continue
            row["box"] = _box(d.get("box"))
            row["confidence"] = float(d.get("confidence") or 0.5)
            out.append(row)
        return {"devices": out, "text": f"Found {len(out)} device{'s' if len(out) != 1 else ''}."}

    def ha_call(self, device: dict, a: dict) -> dict:
        home = self._home()
        eid = str(a.get("entity_id") or "")
        dev = next((d for d in home.devices() if d["entity_id"] == eid), None)
        if not dev:
            raise LookupError(f"{eid} isn't in Home Assistant any more.")
        action = str(a.get("action") or "toggle").lower()
        args = {k: a[k] for k in ("brightness", "temperature", "hvac_mode", "fan_mode", "volume", "source", "speed",
                                  "position", "color") if a.get(k) is not None}
        reason = home.risk(dict(args, action=action, device=eid))
        if reason:
            raise PermissionError(f"Ask Jarvis to do this one so it can check with you first ({reason}).")
        text = home._apply(dev, action, args)
        home._at = 0.0
        self.s.emit({"type": "info", "text": f"📱 AR: {text}"})
        time.sleep(0.4)                     # give Home Assistant a moment so the new state reads back
        return {"ok": True, "text": text, "state": self._state_row(eid, home.states(force=True))}

    def ha_states(self, device: dict, a: dict) -> dict:
        home = self._home()
        force = _now() - self._states_at > 3
        if force:
            self._states_at = _now()
        states = home.states(force=force)
        rows = [self._state_row(e, states) for e in (a.get("entity_ids") or [])[:40]]
        return {"states": [r for r in rows if r]}

    # ------------------------------------------------------------------ spatial notes
    def notes_sync(self, device: dict, a: dict) -> dict:
        key = device.get("id") or "phone"
        notes = []
        for n in (a.get("notes") or [])[:500]:
            if not isinstance(n, dict) or not str(n.get("text") or "").strip():
                continue
            notes.append({"id": str(n.get("id") or "")[:40], "text": str(n["text"])[:500],
                          "place": str(n.get("place") or "")[:60], "created": float(n.get("created") or _now()),
                          "remind": bool(n.get("remind")), "done": bool(n.get("done")),
                          "seen": float(n.get("seen") or 0), "device": device.get("name") or "phone"})
        with self._lock:
            self.notes[key] = notes
            self._save_notes()
        return {"ok": True, "count": len(notes)}

    def note_seen(self, device: dict, a: dict) -> dict:
        nid = str(a.get("id") or "")
        with self._lock:
            for n in self.notes.get(device.get("id") or "phone", []):
                if n["id"] == nid:
                    n["seen"] = _now()
                    self._save_notes()
                    if n.get("remind") and not n.get("done"):
                        self.s.context_notes.append(f"The user just came back to the spot where they pinned the AR "
                                                    f"note “{n['text']}” ({n.get('place') or 'no place name'}).")
                    break
        return {"ok": True}

    def all_notes(self) -> list[dict]:
        with self._lock:
            return [n for v in self.notes.values() for n in v]

    # ------------------------------------------------------------------ agent tool
    def tool(self, a: dict) -> ToolOutput:
        act = (a.get("action") or "find").lower()
        if act == "find":
            q = a.get("object") or a.get("query") or ""
            if not q:
                return ToolOutput("Say which object to look for.", is_error=True)
            hits = self.find(q, int(a.get("limit") or 5))
            text = self.describe_hits(q, hits)
            img = ""
            if hits:
                try:
                    img = to_png(self.thumb_b64(hits[0]["sighting"]), 640)
                except Exception:  # noqa: BLE001
                    img = ""
                text += " (Photo from that moment attached.)" if img else ""
            return ToolOutput(text, img or None)
        if act == "notes":
            notes = [n for n in self.all_notes() if not n.get("done") or a.get("include_done")]
            if not notes:
                return ToolOutput("No AR notes pinned. The user pins them from the phone's AR tab.")
            return ToolOutput("\n".join(
                f"- “{n['text']}”" + (f" at {n['place']}" if n.get("place") else "") + f" (pinned {ago(n['created'])}"
                + (", reminder" if n.get("remind") else "") + (", done" if n.get("done") else "") + ")" for n in notes))
        if act == "recent":
            with self._lock:
                rows = list(self.sightings)[-int(a.get("limit") or 8):]
            if not rows:
                return ToolOutput("Nothing seen through the phone camera yet.")
            return ToolOutput("\n".join(
                f"- {ago(s['at'])}, {s.get('scene') or s.get('place') or 'somewhere'}: "
                + ", ".join(o["name"] for o in s["objects"][:12]) for s in reversed(rows)))
        return ToolOutput("action must be find, notes or recent.", is_error=True)


SPEC = ToolSpec(
    "spatial",
    "What the user's phone camera has seen (Jarvis AR on their Android phone). action=find with object='keys' "
    "answers 'where did I leave my keys?' from earlier camera sightings, with a photo of that moment. "
    "action=notes lists the notes the user pinned in their room in AR; action=recent lists recent sightings.",
    {"action": {"type": "string", "description": "find | notes | recent"},
     "object": {"type": "string", "description": "action=find: what to look for, e.g. 'car keys', 'glasses'"},
     "limit": {"type": "integer", "description": "How many results (default 5)"}},
    ["action"], group="phones")
