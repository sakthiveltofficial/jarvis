"""Smart home control through Home Assistant's REST API (lights, AC, TV, fans, plugs, scenes…).

Settings → Connections → Smart home: the Home Assistant URL (e.g. http://homeassistant.local:8123)
and a long-lived access token (Profile → Security → Long-lived access tokens in Home Assistant).

The agent gets one `smart_home` tool. Devices are found by name ("bedroom AC", "living room lights",
"TV") with fuzzy matching over friendly names, entity ids and areas, so the model never needs ids.
"""
from __future__ import annotations

import difflib
import re
import threading
import time

import httpx

from .tools import ToolOutput, ToolSpec

# Domains worth showing the model / matching names against.
CONTROLLABLE = ("light", "switch", "climate", "media_player", "fan", "cover", "scene", "script", "lock",
                "vacuum", "input_boolean", "humidifier", "water_heater", "remote", "button", "alarm_control_panel")
RISKY_DOMAINS = {"lock", "alarm_control_panel", "cover"}     # unlocking, disarming, opening the garage
COLOURS = {"warm": (255, 180, 107), "cool": (200, 220, 255), "white": (255, 255, 255), "red": (255, 0, 0),
           "green": (0, 255, 0), "blue": (0, 0, 255), "purple": (160, 32, 240), "pink": (255, 105, 180),
           "orange": (255, 140, 0), "yellow": (255, 230, 0), "cyan": (0, 255, 255)}


def configured(settings: dict) -> bool:
    ha = settings.get("home_assistant") or {}
    return bool(ha.get("enabled", True) and ha.get("url") and ha.get("token"))


class HomeAssistant:
    def __init__(self, settings: dict, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport                     # tests inject a MockTransport
        self._states: list[dict] = []
        self._areas: dict[str, str] = {}
        self._at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ http
    def cfg(self) -> dict:
        return self.settings.get("home_assistant") or {}

    def _client(self) -> httpx.Client:
        c = self.cfg()
        url = (c.get("url") or "").strip().rstrip("/")
        if not url:
            raise RuntimeError("Home Assistant isn't set up: add its URL and token in Settings → Connections.")
        if not re.match(r"^https?://", url):
            url = "http://" + url
        return httpx.Client(base_url=url, timeout=10, transport=self.transport, verify=bool(c.get("verify_ssl", True)),
                            headers={"Authorization": f"Bearer {c.get('token', '')}", "Content-Type": "application/json"})

    def _get(self, path: str):
        with self._client() as c:
            r = c.get(path)
            self._check(r)
            return r.json()

    def _post(self, path: str, body: dict | None = None):
        with self._client() as c:
            r = c.post(path, json=body or {})
            self._check(r)
            ct = r.headers.get("content-type", "")
            return r.json() if "json" in ct else r.text

    @staticmethod
    def _check(r: httpx.Response):
        if r.status_code == 401:
            raise RuntimeError("Home Assistant rejected the token (401). Create a new long-lived access token.")
        if r.status_code >= 400:
            raise RuntimeError(f"Home Assistant error {r.status_code}: {r.text[:200]}")

    # ------------------------------------------------------------------ status
    def test(self) -> dict:
        """Check URL + token. Returns {ok, message, version, counts}."""
        try:
            info = self._get("/api/")
            conf = {}
            try:
                conf = self._get("/api/config")
            except Exception:  # noqa: BLE001
                pass
            states = self.states(force=True)
            counts: dict[str, int] = {}
            for s in states:
                d = s["entity_id"].split(".")[0]
                if d in CONTROLLABLE:
                    counts[d] = counts.get(d, 0) + 1
            return {"ok": True, "message": info.get("message", "ok") if isinstance(info, dict) else str(info),
                    "version": conf.get("version", ""), "location": conf.get("location_name", ""), "counts": counts}
        except httpx.ConnectError:
            return {"ok": False, "message": "Can't reach Home Assistant at that address. Check the URL (include :8123) "
                                            "and that this PC is on the same network."}
        except httpx.TimeoutException:
            return {"ok": False, "message": "Home Assistant didn't answer in time. Check the URL."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def states(self, force: bool = False) -> list[dict]:
        with self._lock:
            if force or time.time() - self._at > 20 or not self._states:
                self._states = self._get("/api/states") or []
                self._at = time.time()
                if force or not self._areas:
                    self._areas = self._load_areas()
            return self._states

    def _load_areas(self) -> dict[str, str]:
        tpl = ("{% for s in states %}{% set a = area_name(s.entity_id) %}{% if a %}"
               "{{ s.entity_id }}|{{ a }}\n{% endif %}{% endfor %}")
        try:
            out = self._post("/api/template", {"template": tpl})
        except Exception:  # noqa: BLE001
            return {}
        areas = {}
        for line in str(out).splitlines():
            if "|" in line:
                eid, area = line.split("|", 1)
                areas[eid.strip()] = area.strip()
        return areas

    def devices(self, domain: str = "", query: str = "") -> list[dict]:
        out = []
        for s in self.states():
            eid = s["entity_id"]
            d = eid.split(".")[0]
            if d not in CONTROLLABLE or (domain and d != domain):
                continue
            row = {"entity_id": eid, "name": s.get("attributes", {}).get("friendly_name") or eid, "domain": d,
                   "state": s.get("state"), "area": self._areas.get(eid, "")}
            if query and self._score(query, row) < 0.5:
                continue
            out.append(row)
        return out

    def cached_summary(self) -> str:
        """Never blocks (it's used while building the prompt): refreshes in the background when stale."""
        if time.time() - getattr(self, "_sum_at", 0) > 60 and not getattr(self, "_sum_busy", False):
            self._sum_busy = True

            def refresh():
                try:
                    self._sum = self.summary()
                finally:
                    self._sum_at, self._sum_busy = time.time(), False
            threading.Thread(target=refresh, daemon=True).start()
        return getattr(self, "_sum", "")

    def summary(self, limit: int = 60) -> str:
        """Compact device list for the system prompt."""
        try:
            devs = self.devices()
        except Exception:  # noqa: BLE001
            return ""
        order = {d: i for i, d in enumerate(("light", "climate", "media_player", "fan", "switch", "cover", "scene"))}
        devs.sort(key=lambda r: (order.get(r["domain"], 99), r["area"], r["name"]))
        rows = [f"{r['name']}" + (f" ({r['area']})" if r["area"] and r["area"].lower() not in r["name"].lower() else "")
                + f" [{r['domain']}: {r['state']}]" for r in devs[:limit]]
        more = f"\n…and {len(devs) - limit} more (use smart_home action=list)" if len(devs) > limit else ""
        return "\n".join(rows) + more

    # ------------------------------------------------------------------ name matching
    @staticmethod
    def _norm(s: str) -> str:
        s = s.lower().replace("_", " ").replace(".", " ")
        s = re.sub(r"\b(the|my|a|an|please)\b", " ", s)
        s = s.replace("a/c", "ac").replace("air conditioner", "ac").replace("aircon", "ac")
        s = s.replace("television", "tv").replace("lamp", "light").replace("lights", "light")
        return re.sub(r"\s+", " ", s).strip()

    def _score(self, query: str, row: dict) -> float:
        q = self._norm(query)
        name = self._norm(row["name"])
        full = self._norm(f"{row.get('area', '')} {row['name']}")
        eid = self._norm(row["entity_id"])
        if q in (name, full, eid, self._norm(row["entity_id"].split(".", 1)[-1])):
            return 1.0
        qw = set(q.split())
        best = 0.0
        for cand in (name, full, eid):
            cw = set(cand.split())
            overlap = len(qw & cw) / max(1, len(qw))
            ratio = difflib.SequenceMatcher(None, q, cand).ratio()
            best = max(best, 0.7 * overlap + 0.3 * ratio)
        # "AC" / "TV" / "light" in the query should match the device type, too.
        kinds = {"ac": "climate", "tv": "media_player", "light": "light", "fan": "fan"}
        for w, dom in kinds.items():
            if w in qw and row["domain"] == dom:
                best += 0.25
        return min(best, 0.99)

    def find(self, name: str, domain: str = "") -> tuple[dict | None, list[dict]]:
        """Best match for a spoken name, plus close alternatives."""
        if not name:
            return None, []
        devs = self.devices(domain)
        if "." in name and any(d["entity_id"] == name for d in devs):
            return next(d for d in devs if d["entity_id"] == name), []
        scored = sorted(((self._score(name, d), d) for d in devs), key=lambda x: -x[0])
        if not scored or scored[0][0] < 0.5:
            return None, [d for _, d in scored[:5]]
        top = scored[0][0]
        close = [d for s, d in scored[1:6] if s >= top - 0.05 and s >= 0.5]
        return scored[0][1], close

    def find_all(self, name: str, domain: str = "") -> list[dict]:
        """For "all lights" / "living room lights": every device in that area/domain."""
        q = self._norm(name)
        q = re.sub(r"\b(all|every|everything)\b", " ", q).strip()
        devs = self.devices(domain)
        if not q or q in ("light", "ac", "tv", "fan", "switch"):
            return devs
        return [d for d in devs if q.replace(" light", "").strip() in self._norm(f"{d['area']} {d['name']}")]

    # ------------------------------------------------------------------ actions
    def call_service(self, domain: str, service: str, data: dict) -> list:
        return self._post(f"/api/services/{domain}/{service}", data)

    def control(self, a: dict) -> str:
        action = (a.get("action") or "").lower()
        target = a.get("device") or a.get("entity") or ""
        domain = a.get("domain") or ""
        many = bool(re.search(r"\b(all|every)\b", target.lower())) or a.get("all")
        if many:
            devs = self.find_all(target, domain)
            if not devs:
                return f"I couldn't find any devices matching “{target}”."
        else:
            dev, alts = self.find(target, domain)
            if dev is None:
                hint = ", ".join(d["name"] for d in alts) or "none"
                raise LookupError(f"No device matches “{target}”. Closest: {hint}. Use action=list to see devices.")
            devs = [dev]
        done = []
        for dev in devs:
            done.append(self._apply(dev, action, a))
        self._at = 0.0                                  # re-read states next time
        return "; ".join(done)

    def _apply(self, dev: dict, action: str, a: dict) -> str:
        eid, d, name = dev["entity_id"], dev["domain"], dev["name"]
        data: dict = {"entity_id": eid}
        if action in ("on", "turn_on", "open", "start", "play_on"):
            action = "turn_on"
        if action in ("off", "turn_off", "close", "stop_device"):
            action = "turn_off"

        if d in ("scene", "script") or (d == "button"):
            svc = "press" if d == "button" else "turn_on"
            self.call_service(d, svc, data)
            return f"{name} activated"
        if d == "lock":
            svc = {"turn_on": "lock", "lock": "lock", "turn_off": "unlock", "unlock": "unlock"}.get(action, action)
            self.call_service("lock", svc, data)
            return f"{name}: {svc}ed"
        if d == "cover":
            svc = {"turn_on": "open_cover", "turn_off": "close_cover", "stop": "stop_cover"}.get(action, action)
            if a.get("position") is not None:
                svc, data["position"] = "set_cover_position", int(a["position"])
            self.call_service("cover", svc, data)
            return f"{name}: {svc.replace('_', ' ')}"

        if d == "climate":
            if a.get("temperature") is not None:
                data2 = dict(data, temperature=float(a["temperature"]))
                if a.get("hvac_mode"):
                    data2["hvac_mode"] = a["hvac_mode"]
                self.call_service("climate", "set_temperature", data2)
                return f"{name} set to {a['temperature']}°"
            if a.get("hvac_mode"):
                self.call_service("climate", "set_hvac_mode", dict(data, hvac_mode=a["hvac_mode"]))
                return f"{name} mode {a['hvac_mode']}"
            if a.get("fan_mode"):
                self.call_service("climate", "set_fan_mode", dict(data, fan_mode=a["fan_mode"]))
                return f"{name} fan {a['fan_mode']}"
            if action in ("turn_on", "turn_off", "toggle"):
                self.call_service("climate", action, data)
                return f"{name} {action.replace('turn_', '')}"

        if d == "media_player":
            media = {"play": "media_play", "pause": "media_pause", "play_pause": "media_play_pause",
                     "next": "media_next_track", "previous": "media_previous_track", "stop": "media_stop",
                     "volume_up": "volume_up", "volume_down": "volume_down", "mute": "volume_mute",
                     "unmute": "volume_mute"}
            if a.get("volume") is not None:
                v = float(a["volume"])
                self.call_service("media_player", "volume_set", dict(data, volume_level=max(0.0, min(1.0, v / 100 if v > 1 else v))))
                return f"{name} volume {round(v if v > 1 else v * 100)}%"
            if a.get("source"):
                self.call_service("media_player", "select_source", dict(data, source=a["source"]))
                return f"{name} source {a['source']}"
            if action in media:
                extra = {"is_volume_muted": action == "mute"} if action in ("mute", "unmute") else {}
                self.call_service("media_player", media[action], dict(data, **extra))
                return f"{name}: {action.replace('_', ' ')}"

        if d == "light" and action in ("turn_on", "set", "dim", "brighten", ""):
            if a.get("brightness") is not None:
                data["brightness_pct"] = max(0, min(100, int(a["brightness"])))
            elif action == "dim":
                data["brightness_step_pct"] = -25
            elif action == "brighten":
                data["brightness_step_pct"] = 25
            col = (a.get("color") or "").lower().strip()
            if col in ("warm", "cool"):
                data["color_temp_kelvin"] = 2700 if col == "warm" else 5500
            elif col in COLOURS:
                data["rgb_color"] = list(COLOURS[col])
            elif col:
                data["color_name"] = col
            self.call_service("light", "turn_on", data)
            bits = [f"{data['brightness_pct']}%" if "brightness_pct" in data else "", col]
            return f"{name} on" + (f" ({', '.join(b for b in bits if b)})" if any(bits) else "")

        if d == "fan" and a.get("speed") is not None:
            self.call_service("fan", "set_percentage", dict(data, percentage=int(a["speed"])))
            return f"{name} speed {a['speed']}%"

        if action in ("turn_on", "turn_off", "toggle"):
            svc_domain = d if d not in ("input_boolean",) else "input_boolean"
            self.call_service(svc_domain, action, data)
            return f"{name} {action.replace('turn_', '')}"
        raise ValueError(f"I don't know how to '{action}' a {d}. Use action=service for anything unusual.")

    def assist(self, text: str, language: str = "") -> str:
        """Hand a sentence to Home Assistant's own Assist (handles its local names/aliases, many languages)."""
        body = {"text": text}
        if language:
            body["language"] = language
        out = self._post("/api/conversation/process", body)
        try:
            return out["response"]["speech"]["plain"]["speech"]
        except Exception:  # noqa: BLE001
            return str(out)[:500]

    # ------------------------------------------------------------------ tool
    def tool(self, a: dict) -> ToolOutput:
        try:
            act = (a.get("action") or "").lower()
            if act == "list":
                rows = self.devices(a.get("domain") or "", a.get("device") or "")
                if not rows:
                    return ToolOutput("No matching devices.")
                return ToolOutput("\n".join(f"{r['name']} — {r['entity_id']}" + (f", {r['area']}" if r["area"] else "")
                                            + f": {r['state']}" for r in rows[:150]))
            if act == "state":
                dev, alts = self.find(a.get("device") or "", a.get("domain") or "")
                if not dev:
                    return ToolOutput(f"No device like that. Closest: {', '.join(d['name'] for d in alts) or 'none'}",
                                      is_error=True)
                full = next((s for s in self.states() if s["entity_id"] == dev["entity_id"]), {})
                attrs = {k: v for k, v in (full.get("attributes") or {}).items()
                         if k in ("current_temperature", "temperature", "hvac_mode", "hvac_modes", "fan_mode",
                                  "brightness", "color_temp_kelvin", "volume_level", "is_volume_muted", "source",
                                  "source_list", "media_title", "current_position", "percentage", "unit_of_measurement")}
                return ToolOutput(f"{dev['name']} ({dev['entity_id']}): {full.get('state')} {attrs if attrs else ''}".strip())
            if act == "assist":
                return ToolOutput(self.assist(a.get("text") or a.get("device") or "", a.get("language") or ""))
            if act == "service":
                svc = a.get("service") or ""
                if "." not in svc:
                    return ToolOutput("service must look like domain.service, e.g. light.turn_on", is_error=True)
                dom, s = svc.split(".", 1)
                data = dict(a.get("data") or {})
                if a.get("device") and "entity_id" not in data:
                    dev, _ = self.find(a["device"])
                    if dev:
                        data["entity_id"] = dev["entity_id"]
                self.call_service(dom, s, data)
                self._at = 0.0
                return ToolOutput(f"Called {svc}.")
            return ToolOutput(self.control(a) or "Done.")
        except Exception as e:  # noqa: BLE001
            return ToolOutput(f"{type(e).__name__}: {e}", is_error=True)

    def risk(self, a: dict) -> str | None:
        """Ask first before unlocking doors, disarming alarms or opening covers (garage)."""
        act = (a.get("action") or "").lower()
        if act in ("list", "state"):
            return None
        if act == "service":
            dom, _, svc = (a.get("service") or "").lower().partition(".")
            if dom in RISKY_DOMAINS and svc not in ("lock", "close_cover", "alarm_arm_away", "alarm_arm_home"):
                return f"Call {dom}.{svc} in Home Assistant"
            return None
        if act == "assist" and re.search(r"\b(unlock|disarm|open)\b", (a.get("text") or "").lower()):
            return f"Ask Home Assistant: {a.get('text')}"
        try:
            dev, _ = self.find(a.get("device") or "", a.get("domain") or "")
        except Exception:  # noqa: BLE001
            return None
        if not dev:
            return None
        safe = {"lock": ("lock", "turn_on"), "cover": ("close", "turn_off", "stop"),
                "alarm_control_panel": ()}.get(dev["domain"])
        if safe is not None and act not in safe:
            return f"{act or 'change'} {dev['name']}"
        return None


SPEC_PROPS = {
    "action": {"type": "string", "description": "turn_on | turn_off | toggle | dim | brighten | set | play | pause | "
               "next | previous | volume_up | volume_down | mute | unmute | lock | unlock | open | close | list | "
               "state | assist | service"},
    "device": {"type": "string", "description": "Spoken device name, e.g. 'bedroom AC', 'living room lights', "
               "'TV', 'all lights'. Use 'all …' to act on every match."},
    "domain": {"type": "string", "description": "Optional filter: light, climate, media_player, fan, switch, cover, scene…"},
    "brightness": {"type": "integer", "description": "Light brightness 0-100 (%)"},
    "color": {"type": "string", "description": "Light colour: warm, cool, white, red, blue… or any CSS colour name"},
    "temperature": {"type": "number", "description": "AC / thermostat target temperature"},
    "hvac_mode": {"type": "string", "description": "AC mode: cool, heat, dry, fan_only, auto, off"},
    "fan_mode": {"type": "string", "description": "AC fan: low, medium, high, auto"},
    "speed": {"type": "integer", "description": "Fan speed 0-100 (%)"},
    "volume": {"type": "number", "description": "Media/TV volume 0-100"},
    "source": {"type": "string", "description": "TV input/app, e.g. HDMI 1, Netflix, YouTube"},
    "position": {"type": "integer", "description": "Cover/blind position 0-100"},
    "text": {"type": "string", "description": "action=assist: a sentence for Home Assistant's own Assist"},
    "service": {"type": "string", "description": "action=service: domain.service, e.g. vacuum.start"},
    "data": {"type": "object", "description": "action=service: service data"},
}

SPEC = ToolSpec(
    "smart_home",
    "Control the user's smart home through Home Assistant: lights, AC/thermostats, TV and media players, fans, "
    "plugs, blinds, scenes. Name devices the way the user does ('bedroom AC', 'living room lights', 'TV', "
    "'all lights'); matching is fuzzy. Examples: {action: turn_on, device: 'bedroom light', brightness: 40, "
    "color: warm}; {action: set, device: 'AC', temperature: 24, hvac_mode: cool}; {action: turn_off, device: 'TV'}; "
    "{action: set, device: 'TV', volume: 20}; {action: set, device: 'TV', source: 'Netflix'}. action=list shows "
    "devices, state reads one, assist hands a sentence to Home Assistant's own Assist, service calls any "
    "domain.service. Confirm briefly what changed.",
    SPEC_PROPS, ["action"], group="main")
