"""The `self` tool: the companion looks at and changes its own app, end to end (v0.12).

  status            what's on and off right now (character, listening, proactive features, voice, safety)
  settings          every setting it may change, with current values (optionally one section)
  set               change one or more settings by dotted path ({"health.water_min": 45, "ambient.enabled": false})
  switch_character  Jarvis <-> Ultron (the persona prompt tells each how to react first)
  voice_mode        off | wake | handsfree
  wake_phrase       set your own wake word phrase ("" = the built-in "Hey Jarvis"); train it in Settings
  talk_to           say something to the other character; their spoken reply comes back
  open_settings     open a Settings tab in the app (for secrets, connections and voice training)
  stop_speaking     stop talking

Secrets (API keys, tokens), paired devices and app connections are never readable or changeable here. Changes that
loosen safety are confirmed by the user through the normal approval prompt (see risk()).
"""
from __future__ import annotations

import copy
import json
import time

from . import character, config, llm, persona
from .tools import ToolOutput, ToolSpec

SPEC = ToolSpec(
    "self",
    "Look at and change yourself: this app's own settings and modes, end to end. Actions: status; settings(section?) "
    "lists what you can change with current values; set(changes: {\"dotted.path\": value, ...}); "
    "switch_character(to: jarvis|ultron); voice_mode(mode: off|wake|handsfree); wake_phrase(phrase; empty = \"Hey "
    "Jarvis\"); talk_to(message) speaks to the other character and returns their spoken reply; "
    "open_settings(tab: model|voice|models|behaviour|memory|files|proactive|desktop|presence|skills|connections); "
    "stop_speaking.",
    {"action": {"type": "string", "enum": ["status", "settings", "set", "switch_character", "voice_mode",
                                           "wake_phrase", "talk_to", "open_settings", "stop_speaking"]},
     "section": {"type": "string", "description": "settings: one top-level section, e.g. health, ambient, voice."},
     "changes": {"type": "object", "description": "set: {\"section.key\": value}, e.g. {\"health.water_min\": 45}."},
     "to": {"type": "string", "enum": ["jarvis", "ultron"]},
     "mode": {"type": "string", "enum": ["off", "wake", "handsfree"]},
     "phrase": {"type": "string"},
     "message": {"type": "string"},
     "tab": {"type": "string"}},
    ["action"], group="main")

TABS = ("model", "voice", "models", "behaviour", "memory", "files", "proactive", "desktop", "presence", "skills",
        "connections")

# never readable or writable through the tool
_BLOCKED_TOP = {"api_keys", "mcp", "nodes"}
_BLOCKED = {"home_assistant.token", "node.token", "phones.devices", "phones.extra_hosts", "llm.kind"}
# changing these loosens safety or reach: always confirmed (unless the user chose "never ask")
_SENSITIVE = {
    "agent.confirm_mode": "Changes how often I ask before acting",
    "node.enabled": "Lets other computers control this one",
    "node.allow_elevated": "Lets other computers run admin commands here",
    "phones.enabled": "Opens the phone link",
    "presence.lock_on_leave": "Changes auto-locking when you walk away",
    "presence.stranger_lock": "Changes locking when a stranger sits down",
    "presence.stranger_alert": "Changes stranger alerts",
    "home_assistant.url": "Points me at a different smart home server",
    "home_assistant.verify_ssl": "Changes how I check the smart home server's certificate",
}


def _flat(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        p = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out.update(_flat(v, p + "."))
        else:
            out[p] = v
    return out


def allowed(path: str) -> bool:
    top, last = path.split(".", 1)[0], path.split(".")[-1].lower()
    if top in _BLOCKED_TOP or path in _BLOCKED or last.endswith("token") or last.endswith("key"):
        return False
    return path in _flat(config.DEFAULTS)


def _coerce(path: str, value):
    """Make the value the same type as the default (the model sends "45" or "true" now and then)."""
    default = _flat(config.DEFAULTS).get(path)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    if isinstance(default, list):
        if isinstance(value, str):
            return [x.strip() for x in value.split(",") if x.strip()]
        return list(value)
    if default is None:
        return value
    return str(value)


def _nest(path: str, value) -> dict:
    out: dict = {}
    cur = out
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    return out


def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v
    return a


def risk(args: dict, settings: dict) -> str | None:
    """Why this call should be approved by the user, or None."""
    mode = settings["agent"].get("confirm_mode", "risky")
    if mode == "never":
        return None
    act = args.get("action")
    if act == "set":
        for path, value in (args.get("changes") or {}).items():
            why = _SENSITIVE.get(path)
            if why:
                return f"{why}: {path} = {value}"
        if mode == "always":
            return "Changes my settings: " + ", ".join(f"{k} = {v}" for k, v in (args.get("changes") or {}).items())
    if mode == "always" and act in ("switch_character", "voice_mode", "wake_phrase"):
        return f"Changes my {act.replace('_', ' ')}"
    return None


class SelfControl:
    def __init__(self, session):
        self.s = session
        self.convo: list[tuple[str, str]] = []      # recent lines between the two characters
        self.convo_at = 0.0

    # ------------------------------------------------------------------ tool entry
    def tool(self, args: dict) -> ToolOutput:
        act = (args.get("action") or "status").strip()
        fn = getattr(self, "_a_" + act, None)
        if fn is None:
            return ToolOutput(f"Unknown action {act}.", is_error=True)
        return fn(args)

    # ------------------------------------------------------------------ actions
    def _a_status(self, _args: dict) -> ToolOutput:
        st = self.s.settings
        v, ag = st.get("voice", {}), st.get("agent", {})
        on = lambda sec: "on" if (st.get(sec) or {}).get("enabled") else "off"  # noqa: E731
        lines = [
            f"Character: {character.name(st)} ({character.key(st)}); character voice "
            f"{'on' if st.get('character_voice', True) else 'off'}.",
            f"Listening: {getattr(self.s, 'voice_mode', v.get('mode'))}; wake phrase \"{wake_phrase(st)}\""
            f"{' (custom)' if v.get('wake_phrase') else ''}; clap {'on' if v.get('clap', True) else 'off'}.",
            f"Brain: {st['llm'].get('provider')} / {st['llm'].get('model')}. Hearing: {st['stt'].get('provider')}. "
            f"Voice: {st['tts'].get('provider')}.",
            f"Safety: ask before acting = {ag.get('confirm_mode')}; speak replies {ag.get('speak_replies')}.",
            f"Proactive: ambient {on('ambient')}, health nudges {on('health')}, awareness {on('awareness')}, "
            f"morning briefing {'on' if st['briefings']['morning'].get('enabled') else 'off'}, evening briefing "
            f"{'on' if st['briefings']['evening'].get('enabled') else 'off'}, life report "
            f"{'on' if st['life'].get('report') else 'off'}.",
            f"Camera: presence {on('presence')}, gestures {on('gestures')}. HUD {on('hud')}. "
            f"Orb {'on' if st['ui'].get('orb', True) else 'off'}.",
            f"Memory: auto save {st['memory'].get('auto_save')}; folders indexed: {len(st['files'].get('folders') or [])}.",
            f"Phones: {'on' if st['phones'].get('enabled') else 'off'} ({len(st['phones'].get('devices') or [])} paired). "
            f"Other PCs: {len(st.get('nodes') or [])}. Apps (MCP): {len(st['mcp'].get('servers') or [])}.",
        ]
        return ToolOutput("\n".join(lines))

    def _a_settings(self, args: dict) -> ToolOutput:
        sec = (args.get("section") or "").strip().strip(".")
        flat = _flat(config.public(self.s.settings))
        rows = [f"{k} = {json.dumps(v, default=str)}" for k, v in flat.items()
                if allowed(k) and (not sec or k == sec or k.startswith(sec + "."))]
        if not rows:
            return ToolOutput(f"No changeable settings under '{sec}'. Sections: "
                              + ", ".join(sorted({k.split('.')[0] for k in _flat(config.DEFAULTS) if allowed(k)})))
        return ToolOutput("\n".join(rows))

    def _a_set(self, args: dict) -> ToolOutput:
        changes = args.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            return ToolOutput("Pass changes as {\"section.key\": value}.", is_error=True)
        new: dict = {}
        done, refused = [], []
        voice_mode = None
        for path, value in changes.items():
            path = str(path).strip()
            if not allowed(path):
                refused.append(path)
                continue
            try:
                value = _coerce(path, value)
            except (TypeError, ValueError):
                refused.append(f"{path} (bad value {value!r})")
                continue
            if path == "voice.mode":
                voice_mode = value
                continue
            if path == "character":
                value = value if value in character.CHARACTERS else character.DEFAULT
            _merge(new, _nest(path, value))
            done.append(f"{path} = {value}")
        if new:
            if "character" in new:
                new.update(_stock_name(self.s.settings, new["character"]))
            self.s.apply_settings(copy.deepcopy(new))
            self.s.emit({"type": "settings_changed"})
        if voice_mode:
            self.s.set_voice_mode(voice_mode)
            done.append(f"voice.mode = {voice_mode}")
        msg = ("Changed: " + "; ".join(done) + ".") if done else "Nothing changed."
        if refused:
            msg += (" Not changeable from here (secrets, connections or unknown): " + ", ".join(refused)
                    + ". Offer to open Settings for those.")
        return ToolOutput(msg, is_error=not done)

    def _a_switch_character(self, args: dict) -> ToolOutput:
        to = (args.get("to") or character.other(self.s.settings)).lower()
        if to not in character.CHARACTERS:
            return ToolOutput("to must be jarvis or ultron.", is_error=True)
        before = character.key(self.s.settings)
        if to == before:
            return ToolOutput(f"Already {character.CHARACTERS[to]['name']}.")
        self.s.apply_settings({"character": to, **_stock_name(self.s.settings, to)})
        self.s.emit({"type": "settings_changed"})
        name = character.name(self.s.settings)
        return ToolOutput(f"Switched. You are now {name}, from your very next words, with {name}'s voice, manner and "
                          f"orb colour. Make your entrance in character (one or two lines) and react to "
                          f"{character.CHARACTERS[before]['name']} having just handed over.")

    def _a_voice_mode(self, args: dict) -> ToolOutput:
        mode = args.get("mode") or "wake"
        self.s.set_voice_mode(mode)
        return ToolOutput(f"Listening mode is now {self.s.voice_mode}.")

    def _a_wake_phrase(self, args: dict) -> ToolOutput:
        phrase = clean_phrase(args.get("phrase") or "")
        self.s.apply_settings({"voice": {"wake_phrase": phrase}})
        self.s.emit({"type": "settings_changed"})
        if not phrase:
            return ToolOutput("Wake phrase reset to the built-in \"Hey Jarvis\".")
        return ToolOutput(f"Wake phrase set to \"{phrase}\". Until it's trained I listen for it in what I transcribe "
                          "(slower). Suggest training it: Settings → Proactive → Train wake word (five takes). Offer to "
                          "open that tab.")

    def _a_open_settings(self, args: dict) -> ToolOutput:
        tab = (args.get("tab") or "behaviour").lower()
        if tab not in TABS:
            return ToolOutput("tab must be one of " + ", ".join(TABS), is_error=True)
        self.s.emit({"type": "open_settings", "tab": tab})
        return ToolOutput(f"Opened Settings → {tab}.")

    def _a_stop_speaking(self, _args: dict) -> ToolOutput:
        self.s.stop_speaking()
        return ToolOutput("Stopped speaking.")

    def _a_talk_to(self, args: dict) -> ToolOutput:
        msg = (args.get("message") or "").strip()
        if not msg:
            return ToolOutput("Say something to them: pass message.", is_error=True)
        st = self.s.settings
        me, them = character.key(st), character.other(st)
        user = st.get("user_name") or "the user"
        if time.time() - self.convo_at > 600:
            self.convo = []
        self.convo_at = time.time()
        self.convo.append((character.CHARACTERS[me]["name"], msg))
        transcript = "\n".join(f"{who}: {line}" for who, line in self.convo[-10:])
        try:
            reply = llm.complete(st, character.other_prompt(st, user),
                                 f"The conversation so far:\n{transcript}\n\nYour reply as "
                                 f"{character.CHARACTERS[them]['name']}:", max_tokens=200)
        except Exception as e:  # noqa: BLE001
            return ToolOutput(f"{character.CHARACTERS[them]['name']} couldn't answer ({e}).", is_error=True)
        reply, _mood = persona.split_mood(reply or "")
        reply = reply.strip().strip('"')
        for prefix in (character.CHARACTERS[them]["name"] + ":",):
            if reply.startswith(prefix):
                reply = reply[len(prefix):].strip()
        if not reply:
            return ToolOutput("They said nothing.", is_error=True)
        self.convo.append((character.CHARACTERS[them]["name"], reply))
        self.s.emit({"type": "assistant", "text": reply, "character": them,
                     "speaker": character.CHARACTERS[them]["name"]})
        self.s.speak(reply, who=them)
        return ToolOutput(f"{character.CHARACTERS[them]['name']} said (already spoken aloud to {user} in their own "
                          f"voice; don't repeat it): {reply}")


def _stock_name(settings: dict, to: str) -> dict:
    """A stock assistant name follows the character; a custom one stays."""
    cur = str(settings.get("assistant_name") or "").strip().lower()
    if not cur or cur in character.default_names():
        return {"assistant_name": character.CHARACTERS.get(to, character.CHARACTERS["jarvis"])["name"]}
    return {}


# ------------------------------------------------------------------ wake phrase helpers (also used by the session)
def clean_phrase(p: str) -> str:
    return " ".join(str(p or "").replace("\n", " ").split())[:60]


def wake_phrase(settings: dict) -> str:
    return clean_phrase((settings.get("voice") or {}).get("wake_phrase") or "") or "Hey Jarvis"


def custom_wake(settings: dict) -> bool:
    return bool(clean_phrase((settings.get("voice") or {}).get("wake_phrase") or ""))


def wake_profile_name(settings: dict) -> str:
    """The name a trained wake profile is saved under: the stock model, or the custom phrase."""
    v = settings.get("voice") or {}
    if custom_wake(settings):
        return "phrase:" + clean_phrase(v.get("wake_phrase")).lower()
    return v.get("wake_word", "hey_jarvis")
