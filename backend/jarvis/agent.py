"""The conversational agent: voice pipeline, agent loops (foreground + background), confirmation gate,
wake word, scheduler, skills, MCP apps, other PCs, screen awareness and emotion-aware speech."""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import queue
import re
import threading
import time
import traceback
import uuid
from typing import Awaitable, Callable

import numpy as np

from . import ar, config, emotion, homeassistant, lang, lifelog, llm, nodes, persona, phones, scheduler, skills, tools
from . import memory as gm
from .ambient import Ambient
from .consolidation import Consolidator
from .files import FileIndex
from .audio import Mic, Player
from .awareness import Awareness
from .wellbeing import Wellbeing
from .mcp_client import MCPManager
from .speech import STT, TTS

YES = re.compile(r"^\W*(yes|yeah|yep|sure|ok(ay)?|go( ahead)?|do it|confirm(ed)?|approved?|proceed|please do)\b"
                 rf"|^\W*({lang.YES_TA})", re.I)
BACKCHANNEL = set("""mm mhm mmhm hmm uh um huh yeah yep yes yup ok okay right sure cool nice wow oh ah
uh-huh aha great alright got it i see true exactly totally indeed""".split())
INTERRUPT_WORDS = re.compile(r"\b(stop|wait|hold on|hang on|jarvis|no no|cancel|enough|pause|shut up|excuse me|actually)\b"
                             rf"|{lang.STOP_TA}", re.I)
NO = re.compile(r"^\W*(no|nope|don'?t|stop|cancel|deny|negative|abort|wait)\b" rf"|{lang.NO_TA}", re.I)
STOP_TEACH = re.compile(r"\b(stop (teaching|watching|recording)|i'?m done|that'?s it|that'?s all|done teaching|finished)\b", re.I)
PLANS_FILE = config.HOME / "plans.json"


def speakable(text: str) -> str:
    t = re.sub(r"```.*?```", " (code shown on screen) ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "the link", t)
    t = re.sub(r"[*_#>|]+", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    return re.sub(r"\s+", " ", t).strip()


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text)
    out, cur = [], ""
    for p in parts:
        cur = f"{cur} {p}".strip()
        if len(cur) > 40:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def wake_regex(name: str) -> re.Pattern:
    low = (name or "Jarvis").lower()
    n = re.escape(low)
    ta = lang.NAME_TA.get(low)
    alt = f"|{re.escape(ta)}" if ta else ""
    return re.compile(rf"^\W*((hey|hi|hello|ok|okay|yo|டேய்|ஹே)\W+)?({n}\b{alt})[\W]*", re.I)


# =========================================================================== agent loop
class Runner:
    """One agent loop (the main conversation, or a background agent with its own history)."""

    def __init__(self, session: "Session", provider: llm.Provider, runtime: tools.ToolRuntime,
                 pool: concurrent.futures.Executor, agent_id: str = "main", label: str = ""):
        self.s = session
        self.p = provider
        self.runtime = runtime
        self.pool = pool
        self.id = agent_id
        self.label = label
        self.background = agent_id != "main"
        self.cancelled = False
        self.steps_done = 0
        self.last_text = ""

    # events ------------------------------------------------------------------
    def emit(self, ev: dict):
        if self.background:
            ev = dict(ev, agent=self.id)
            if ev["type"] == "assistant":
                ev = {"type": "bg_update", "id": self.id, "text": ev["text"]}
            elif ev["type"] == "state":
                return
        self.s.emit(ev)

    def set_state(self, st: str):
        if not self.background:
            self.s.set_state(st)

    # tools -------------------------------------------------------------------
    def specs(self) -> list[tools.ToolSpec]:
        st = self.s.settings
        out = tools.specs_for(self.p.vision, main=not self.background, nodes=bool(st.get("nodes")),
                              exclude=tools.GUI_TOOLS if self.background else None,
                              phones=bool((st.get("phones") or {}).get("devices")))
        if homeassistant.configured(st):
            out = out + [homeassistant.SPEC]
        if not self.background and (st.get("phones") or {}).get("devices"):
            out = out + [ar.SPEC]
        if not self.background:
            out = out + [lifelog.SPEC]
        return out + self.s.mcp.specs()

    async def call_tool(self, call: llm.ToolCall) -> tools.ToolOutput:
        name, args = call.name, call.args or {}
        if name == "plan":
            return self.s.plan_tool(self.id, args)
        if self.s.mcp.is_mcp(name):
            return await asyncio.to_thread(self._safe, self.s.mcp.call, name, args)
        if name == "smart_home":
            return await asyncio.to_thread(self._safe, self.s.home.tool, args)
        if name in self.s.meta_tools:
            return await asyncio.to_thread(self._safe, self.s.meta_tools[name], args)
        return await self.s.loop.run_in_executor(self.pool, self.runtime.call, name, args)

    @staticmethod
    def _safe(fn, *a) -> tools.ToolOutput:
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001
            return tools.ToolOutput(f"{type(e).__name__}: {e}", is_error=True)

    def risk(self, call: llm.ToolCall) -> str | None:
        mode = self.s.settings["agent"].get("confirm_mode", "risky")
        if self.s.mcp.is_mcp(call.name):
            return self.s.mcp.risk(call.name, mode)
        if call.name == "smart_home":
            if mode == "never":
                return None
            a = call.args or {}
            if mode == "always" and (a.get("action") or "") not in ("list", "state"):
                return f"Change your smart home: {tools.describe('smart_home', a)}"
            return self.s.home.risk(a)
        return tools.needs_confirmation(call.name, call.args, mode)

    # loop --------------------------------------------------------------------
    async def run(self, text: str, images: list[str] | None = None) -> tuple[list[str], bool]:
        p = self.p
        mark = len(p.history)
        p.add_user(text, images) if images else p.add_user(text)
        replies: list[str] = []
        specs = self.specs()
        max_steps = int(self.s.settings["agent"].get("max_steps", 40)) * (3 if self.background else 1)
        ok = True
        try:
            for _ in range(max_steps):
                self.set_state("thinking")
                system = self.s.system_prompt(p.vision, background=self.background, agent_id=self.id)
                step = await asyncio.to_thread(p.step, system, specs)
                text, mood = persona.split_mood(step.text)
                if mood and not self.background:
                    self.emit({"type": "ai_mood", "mood": mood})
                if text.strip():
                    self.last_text = text.strip()
                    self.emit({"type": "assistant", "text": self.last_text})
                    replies.append(self.last_text)
                    if not self.background:
                        self.s.speak(text)
                if not step.tool_calls:
                    break
                results = []
                for call in step.tool_calls:
                    if self.cancelled:
                        results.append((call, tools.ToolOutput("Cancelled: the user interrupted.", is_error=True)))
                        continue
                    reason = self.risk(call)
                    if reason and not await self.s.confirm(call, reason, self):
                        results.append((call, tools.ToolOutput("The user declined this action.", is_error=True)))
                        continue
                    self.set_state("acting")
                    cid = uuid.uuid4().hex[:8]
                    self.emit({"type": "tool_start", "id": cid, "tool": call.name,
                               "summary": tools.describe(call.name, call.args)})
                    out = await self.call_tool(call)
                    self.steps_done += 1
                    self.emit({"type": "tool_end", "id": cid, "ok": not out.is_error, "text": out.text[:1500],
                               "image": out.image_b64})
                    results.append((call, out))
                p.add_tool_results(results)
                if self.cancelled:
                    break
                specs = self.specs()          # MCP servers may have connected meanwhile
            else:
                msg = "I've hit my step limit for this task. Want me to keep going?"
                self.emit({"type": "assistant", "text": msg})
                replies.append(msg)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            ok = False
            del p.history[mark:]   # drop the failed turn so the conversation stays valid
            self.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
            if not self.background:
                self.s.speak("Sorry, I ran into a problem talking to the model. The details are on screen.")
        return replies, ok


# =========================================================================== session
class Session:
    def __init__(self, emit: Callable[[dict], Awaitable[None]]):
        self._emit_async = emit
        self.loop = asyncio.get_event_loop()
        self.settings = config.load()
        self.provider: llm.Provider = llm.make_provider(self.settings)
        self.tool_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tools")
        self.runtime = tools.ToolRuntime(self.settings)
        self.runtime.vision = self.provider.vision
        self.main = Runner(self, self.provider, self.runtime, self.tool_pool)
        self.mic = Mic(self.settings, self._on_mic_level, self._on_utterance, self._on_barge, self._on_wake,
                       on_clap=self._on_clap)
        self.last_lang = ""                     # language of the user's last message: ta | mixed | tanglish | en
        self.home = homeassistant.HomeAssistant(self.settings)
        TTS.notice = lambda msg: self.emit({"type": "info", "text": msg})
        self._now_speaking = ""
        self._mem_q: queue.Queue = queue.Queue()
        try:
            self.memory: gm.GraphMemory | None = gm.GraphMemory()
            self.memory.ensure_user(self.settings.get("user_name") or "")
            self.memory.import_legacy()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.memory = None
            print(f"[jarvis] graph memory unavailable: {e}")
        self.runtime.memory = self.memory
        self.runtime.on_memory = self.emit
        threading.Thread(target=self._memory_worker, daemon=True).start()
        self.consolidator: Consolidator | None = None
        self.files: FileIndex | None = None
        if self.memory is not None:
            self.consolidator = Consolidator(self.settings, self.memory, self.emit, self._busy)
            self.consolidator.start()
            try:
                self.files = FileIndex(self.settings, self.memory, self.emit)
                self.files.start()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                print(f"[jarvis] file index unavailable: {e}")
        self.player = Player(self.settings)
        self.task: asyncio.Task | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.state = "idle"
        self._speech_q: queue.Queue = queue.Queue()
        self._audio_q: queue.Queue = queue.Queue()
        self._speech_gen = 0
        # v0.4: proactive + smarter agent
        self.plans: dict[str, dict] = self._load_plans()
        self.background: dict[str, dict] = {}
        self.context_notes: list[str] = []      # things to tell the main agent with the next message
        self.pending_offer: dict | None = None
        self.mood = {"mood": "neutral", "at": 0.0, "cues": []}
        self.teacher: skills.Recorder | None = None
        self.voice_mode = "off"
        self.engaged_until = 0.0
        self.wake_ready = False
        self.mcp = MCPManager()
        self.mcp.reload(self.settings)
        self.scheduler = scheduler.Scheduler(self.settings, self._on_schedule)
        self.node_server: nodes.NodeServer | None = None
        self._sync_node_server()
        self.reply_to: str | None = None       # phone device id that should hear this turn's replies ("-" = nobody)
        self.phones = phones.PhoneHub(self)
        self.ar = ar.ARHub(self)
        self._sync_phone_hub()
        self.meta_tools = {
            "schedule": self._t_schedule, "list_schedules": self._t_list_schedules,
            "cancel_schedule": self._t_cancel_schedule, "use_skill": self._t_use_skill,
            "save_skill": self._t_save_skill, "teach": self._t_teach, "background_task": self._t_background,
            "remote_pc": self._t_remote_pc, "phone": self.phones.tool, "spatial": self.ar.tool,
            "search_files": self._t_search_files,
        }
        threading.Thread(target=self._synth_worker, daemon=True).start()
        threading.Thread(target=self._play_worker, daemon=True).start()
        threading.Thread(target=self._wake_watchdog, daemon=True).start()
        self.awareness = Awareness(self)
        self.ambient = Ambient(self)
        # v0.8: screen HUD overlay + webcam hand gestures
        self.runtime.on_hud = self.emit
        self.gestures = None
        self._gesture_ptt = False
        self._gesture_ptt_at = 0.0
        self._ptt_pose_off = 0.0
        self._gesture_lock = threading.Lock()
        # v0.10: one webcam stream shared by gestures and Presence (face at the desk, posture), health nudges,
        # life log and the weekly life report
        from .camera import CameraHub
        self.camera = CameraHub(self.settings)
        self.wellbeing = Wellbeing(self, self.camera)
        self.meta_tools["life"] = self.wellbeing.tool
        self._sync_gestures()
        STT.preload(self.settings)
        mode = self.settings.get("voice", {}).get("mode", "off")
        if mode != "off":
            threading.Thread(target=self.set_voice_mode, args=(mode, False), daemon=True).start()

    # ------------------------------------------------------------------ events
    def emit(self, ev: dict):
        """Thread-safe emit."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            asyncio.ensure_future(self._emit_async(ev))
        else:
            asyncio.run_coroutine_threadsafe(self._emit_async(ev), self.loop)

    def set_state(self, s: str):
        self.state = s
        self.emit({"type": "state", "state": s})

    def _idle_state(self) -> str:
        if self.mic.mode == "handsfree":
            return "listening"
        return "idle"

    def _on_mic_level(self, lvl: float):
        if self.mic.mode in ("handsfree", "ptt"):
            self.emit({"type": "level", "source": "mic", "value": round(lvl, 3)})

    @property
    def cancelled(self) -> bool:
        return self.main.cancelled

    # ------------------------------------------------------------------ settings
    def apply_settings(self, new: dict):
        keys = new.pop("api_keys", None) or {}
        keys = {k: v for k, v in keys.items() if v is not None and not str(v).startswith("••••")}
        if keys:
            new["api_keys"] = keys
        if "mcp" in new:
            new["mcp"]["servers"] = config.restore_masked(new["mcp"].get("servers") or [],
                                                          self.settings.get("mcp", {}).get("servers") or [])
        if "nodes" in new:
            new["nodes"] = config.restore_masked(new["nodes"] or [], self.settings.get("nodes") or [])
        ha = new.get("home_assistant")
        if isinstance(ha, dict) and str(ha.get("token", "")).startswith("••••"):
            ha["token"] = (self.settings.get("home_assistant") or {}).get("token", "")
        old_llm = dict(self.settings["llm"])
        merged = config.save(new)
        for k in ("mcp", "nodes"):           # lists replace rather than merge
            if k in new:
                merged[k] = new[k]
        merged = config.save_raw(merged)
        self.settings.clear()
        self.settings.update(merged)
        if self.memory:
            self.memory.ensure_user(self.settings.get("user_name") or "")
        if self.settings["llm"] != old_llm or keys:
            self.provider = llm.make_provider(self.settings)
            self.main = Runner(self, self.provider, self.runtime, self.tool_pool)
            self.emit({"type": "info", "text": "Model settings changed — starting a fresh conversation."})
        self.runtime.vision = self.provider.vision
        self.mcp.reload(self.settings)
        self.scheduler.sync_briefings()
        if self.files:
            self.files.sync()
        self._sync_phone_hub()
        self._sync_node_server()
        self._sync_clap()
        self._sync_gestures()
        threading.Thread(target=self.wellbeing.sync, name="presence-sync", daemon=True).start()
        self.camera.sync()
        self.home._at = self.home._sum_at = 0.0
        self.emit({"type": "agenda"})
        STT.preload(self.settings)

    def reset(self):
        self.provider.reset()

    # ------------------------------------------------------------------ voice modes + wake word
    def set_voice_mode(self, mode: str, persist: bool = True):
        """off | wake ("Hey Jarvis") | handsfree (always listening)."""
        if mode not in ("off", "wake", "handsfree"):
            mode = "off"
        if mode == "wake" and self.mic.wake is None:
            self.emit({"type": "info", "text": "Loading the wake word model…"})
            try:
                from .wakeword import WakeWord
                v = self.settings.get("voice", {})
                self.mic.wake = WakeWord(v.get("wake_word", "hey_jarvis"), float(v.get("wake_threshold", 0.5)))
                self.wake_ready = True
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.wake_ready = False
                self.emit({"type": "info", "text": f"Wake word model unavailable ({e}); I'll listen for my name instead."})
        self.voice_mode = mode
        self._sync_clap()
        try:
            self.mic.set_idle_mode(mode)
        except Exception as e:  # noqa: BLE001
            self.voice_mode = "off"
            self.emit({"type": "error", "text": f"Microphone unavailable: {e}"})
        if persist:
            self.settings.setdefault("voice", {})["mode"] = self.voice_mode
            config.save({"voice": {"mode": self.voice_mode}})
        self.emit({"type": "voice_mode", "mode": self.voice_mode, "wake_ready": self.mic.wake is not None})
        self.emit({"type": "handsfree", "on": self.voice_mode != "off"})
        if self.state in ("idle", "listening"):
            self.set_state(self._idle_state())

    def set_handsfree(self, on: bool):
        self.set_voice_mode("handsfree" if on else "off")

    def _sync_clap(self):
        """Double-clap wake: on whenever the mic is listening (wake word / always) and the setting is on."""
        v = self.settings.get("voice", {})
        if v.get("clap", True) and self.voice_mode != "off":
            sens = float(v.get("clap_sensitivity", 0.5))
            if self.mic.clap is None:
                from .clap import ClapDetector
                self.mic.clap = ClapDetector(sens)
            else:
                self.mic.clap.set_sensitivity(sens)
        else:
            self.mic.clap = None

    def _on_clap(self):
        """Double clap heard: like the wake word, but answer out loud (Iron Man style) when clap_ack is on."""
        v = self.settings.get("voice", {})
        ack = v.get("clap_ack", True) and self.settings["tts"].get("provider") != "none" \
            and self.settings["agent"].get("speak_replies", True)
        self._on_wake(source="clap", chime=not ack)
        if ack:
            from .clap import ack_line
            line = ack_line()
            if self.last_lang in ("ta", "mixed"):
                line = "சொல்லுங்க?"
            elif self.last_lang == "tanglish":
                line = "Sollunga?"
            self.emit({"type": "assistant", "text": line, "proactive": "clap"})
            self.speak(line)

    # ------------------------------------------------------------------ hand gestures (gestures.py)
    def _sync_gestures(self):
        """Start, stop or restart the webcam gesture tracker to match the settings (off the event loop)."""
        def work():
            with self._gesture_lock:
                g = self.settings.get("gestures") or {}
                if g.get("enabled"):
                    if self.gestures is None:
                        from .gestures import GestureControl
                        self.gestures = GestureControl(self.settings, self._on_gesture, self._on_hand, self.camera)
                    self.gestures.start()
                elif self.gestures is not None and self.gestures.running:
                    self.gestures.stop()
                    self._on_hand(None)
        threading.Thread(target=work, name="gestures-sync", daemon=True).start()

    def gesture_status(self) -> dict:
        if self.gestures is None:
            from .gestures import model_ready
            return {"state": "off", "error": "", "fps": 0, "model_ready": model_ready(), "last_gesture": "",
                    "hand_visible": False}
        return self.gestures.status()

    def _on_hand(self, h: dict | None):
        self.emit({"type": "hand", "hand": h})
        if self._gesture_ptt:            # palm push-to-talk ends when the palm comes down (or after 30 s)
            now = time.time()
            if h is None or now - self._gesture_ptt_at > 30:
                self._end_gesture_ptt()
            elif h.get("pose") == "palm":
                self._ptt_pose_off = 0.0
            elif not self._ptt_pose_off:
                self._ptt_pose_off = now
            elif now - self._ptt_pose_off > 0.8:
                self._end_gesture_ptt()

    def _end_gesture_ptt(self):
        if self._gesture_ptt:
            self._gesture_ptt = False
            threading.Thread(target=self.ptt_stop, daemon=True).start()

    async def _gesture_stop(self):
        await self.interrupt()
        self.set_state(self._idle_state())

    def _on_gesture(self, g: str):
        """A hand gesture from the webcam: act on it and tell the UI (the orb and HUD react too)."""
        from .gestures import window_keys
        cfg = self.settings.get("gestures") or {}
        if g == "lost":
            self._end_gesture_ptt()
            self.emit({"type": "gesture", "gesture": "lost", "action": ""})
            return
        action = ""
        if g == "palm" and cfg.get("palm", True):
            if self.state == "speaking":
                self.stop_speaking()
                action = "stopped talking"
            elif self.voice_mode != "off":
                if self.mic.mode == "wake":
                    self.mic.engage()
                self._on_wake(source="gesture")
                action = "listening"
            elif not self._gesture_ptt and self.state not in ("thinking", "acting", "transcribing"):
                self.ptt_start()
                self._gesture_ptt, self._gesture_ptt_at, self._ptt_pose_off = True, time.time(), 0.0
                action = "listening while your palm is up"
        elif g == "fist" and cfg.get("fist", True):
            if self._gesture_ptt:
                self._end_gesture_ptt()
                action = "sent"
            elif self._busy() or self.state == "speaking":
                asyncio.run_coroutine_threadsafe(self._gesture_stop(), self.loop)
                action = "stopped"
        elif g.startswith("swipe_") and cfg.get("windows", True):
            keys = window_keys(g)
            if keys:
                try:
                    import pyautogui
                    pyautogui.hotkey(*keys)
                    action = {"swipe_left": "previous window", "swipe_right": "next window",
                              "swipe_up": "maximise", "swipe_down": "minimise"}.get(g, "+".join(keys))
                except Exception as e:  # noqa: BLE001
                    action = f"couldn't switch windows: {e}"
        elif g == "pinch_tap" and cfg.get("orb", True):
            action = "show or hide Jarvis"      # the desktop app does this when it sees the event
        self.emit({"type": "gesture", "gesture": g, "action": action})

    def _follow_up(self) -> float:
        return float(self.settings.get("voice", {}).get("follow_up_s", 10))

    def _on_wake(self, source: str = "word", chime: bool = True):
        """The wake word (or a double clap) was heard: stop talking, chime, listen."""
        self.stop_speaking()
        self.engaged_until = time.time() + float(self.settings.get("voice", {}).get("listen_s", 8))
        self.set_state("listening")
        self.emit({"type": "wake", "source": source})
        if chime and self.settings.get("voice", {}).get("chime", True):
            from .wakeword import chime
            threading.Thread(target=lambda: Player(self.settings).play(chime(), 24000), daemon=True).start()

    def _busy(self) -> bool:
        return (bool(self.task and not self.task.done()) or self.state in ("transcribing", "thinking", "acting")
                or not self._speech_q.empty() or not self._audio_q.empty() or self.mic.speaking or bool(self.pending))

    def _wake_watchdog(self):
        """Back to waiting for the wake word after the conversation goes quiet."""
        while True:
            time.sleep(0.25)
            if self.voice_mode != "wake" or self.mic.wake is None or self.mic.mode != "handsfree":
                continue
            if self._busy() or self.mic.in_speech:
                self.engaged_until = max(self.engaged_until, time.time() + 1.0)
                continue
            if time.time() > self.engaged_until:
                self.mic.mode = "wake"
                self.emit({"type": "wake_sleep"})
                if self.state == "listening":
                    self.set_state("idle")

    def _engage_follow_up(self):
        self._engaged_text_until = time.time() + self._follow_up()
        if self.voice_mode == "wake" and self.mic.wake is not None:
            self.engaged_until = time.time() + self._follow_up()
            if self.mic.mode == "wake":
                self.mic.engage()

    # ------------------------------------------------------------------ voice input
    def ptt_start(self):
        self.stop_speaking()
        self.mic.ptt_start()
        self.set_state("listening")

    def ptt_stop(self):
        audio = self.mic.ptt_stop()
        if len(audio) < 16000 * 0.3:
            self.set_state(self._idle_state())
            return
        threading.Thread(target=self._on_utterance, args=(audio, True), daemon=True).start()

    def _on_utterance(self, audio: np.ndarray, ptt: bool = False):
        self.engaged_until = time.time() + 60
        self.set_state("transcribing")
        try:
            text = STT(self.settings).transcribe(audio)
        except Exception as e:  # noqa: BLE001
            self.emit({"type": "error", "text": f"Speech recognition failed: {e}"})
            self.set_state(self._idle_state())
            return
        wre = wake_regex(self.settings.get("assistant_name"))
        if not ptt and self.voice_mode == "wake" and self.mic.wake is None and not self._recently_engaged():
            # text-based wake fallback (no wake model): only respond when addressed by name
            if not wre.search(text or ""):
                self.set_state(self._idle_state())
                return
        self._engaged_text_until = time.time() + 120
        text = wre.sub("", text or "", count=1).strip() if wre.search(text or "") else (text or "").strip()
        if not text or re.fullmatch(r"[\W_]*(thank you\.?|you)?[\W_]*", text, re.I):
            if not ptt:
                self.engaged_until = time.time() + self._follow_up()
            self.set_state(self._idle_state())
            return
        mood = None
        if self.settings.get("voice", {}).get("emotion", True):
            try:
                mood = emotion.analyze(audio, text)
                self.mood = {**mood, "at": time.time()}
                self.emit({"type": "mood", **mood})
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        asyncio.run_coroutine_threadsafe(self.handle_user(text, voice=True, mood=mood), self.loop)

    def _recently_engaged(self) -> bool:
        return time.time() < getattr(self, "_engaged_text_until", 0)

    # ------------------------------------------------------------------ voice output
    def speak(self, text: str):
        if not self.settings["agent"].get("speak_replies", True) or self.settings["tts"].get("provider") == "none":
            return
        if self.reply_to == "-":
            return
        if self.reply_to and self.phones.online(self.reply_to):
            device = self.reply_to
            gen = self._speech_gen
            for s in sentences(speakable(text)):
                self.phones.pool.submit(self._speak_on_phone, device, s, gen)
            return
        style = None
        if self.mood.get("mood", "neutral") != "neutral" and time.time() - self.mood.get("at", 0) < 300 \
                and self.settings.get("voice", {}).get("emotion", True):
            style = emotion.STYLES.get(self.mood["mood"])
        for s in sentences(speakable(text)):
            self._speech_q.put((self._speech_gen, s, style))

    def stop_speaking(self):
        self._speech_gen += 1
        self.player.stop()
        self.phones.stop_audio()

    def _speak_on_phone(self, device: str, text: str, gen: int):
        if gen != self._speech_gen:
            return
        try:
            from .speech import to_wav_bytes
            audio, sr = TTS(self.settings).synthesize(text)
            if gen == self._speech_gen:
                self.phones.send_audio(device, to_wav_bytes(audio, sr), text)
        except Exception as e:  # noqa: BLE001
            self.emit({"type": "error", "text": f"Text-to-speech failed: {e}"})

    # ------------------------------------------------------------------ phones
    def phone_utterance(self, wav: bytes, origin: str, images: list[str] | None = None, context: str = ""):
        """Push-to-talk audio recorded on a phone (16 kHz WAV); images/context come with an AR "look"."""
        from .speech import SR, from_wav_bytes
        try:
            audio, sr = from_wav_bytes(wav)
            if sr != SR:
                n = int(len(audio) * SR / sr)
                audio = np.interp(np.linspace(0, len(audio), n, endpoint=False), np.arange(len(audio)), audio).astype(np.float32)
        except Exception as e:  # noqa: BLE001
            self.emit({"type": "error", "text": f"Couldn't read the phone's audio: {e}"})
            return
        self.set_state("transcribing")
        try:
            text = (STT(self.settings).transcribe(audio) or "").strip()
        except Exception as e:  # noqa: BLE001
            self.emit({"type": "error", "text": f"Speech recognition failed: {e}"})
            self.set_state(self._idle_state())
            return
        if not text or re.fullmatch(r"[\W_]*(thank you\.?|you)?[\W_]*", text, re.I):
            self.emit({"type": "info", "text": "I didn't catch that."})
            self.set_state(self._idle_state())
            return
        mood = None
        if self.settings.get("voice", {}).get("emotion", True):
            try:
                mood = emotion.analyze(audio, text)
                self.mood = {**mood, "at": time.time()}
                self.emit({"type": "mood", **mood})
            except Exception:  # noqa: BLE001
                pass
        asyncio.run_coroutine_threadsafe(self.handle_user(text, voice=True, mood=mood, origin=origin, images=images,
                                                          context=context), self.loop)

    def on_phone_notification(self, phone: str, n: dict):
        app, title, text = n.get("app", ""), n.get("title", ""), (n.get("text") or "")[:300]
        self.context_notes.append(f"New notification on {phone}: {app}: {title}: {text} (key={n.get('key')})")
        self.context_notes[:] = self.context_notes[-10:]
        self.emit({"type": "info", "text": f"📱 {app}: {title} — {text[:120]}"})

    def _sync_phone_hub(self):
        cfg = self.settings.get("phones") or {}
        want = int(cfg.get("port") or phones.DEFAULT_PORT)
        if cfg.get("enabled"):
            if self.phones.running() and self.phones.port != want:
                self.phones.stop()
            if not self.phones.running():
                try:
                    self.phones.start()
                except Exception as e:  # noqa: BLE001
                    self.emit({"type": "error", "text": f"Couldn't start the phone link: {e}"})
        elif self.phones.running():
            self.phones.stop()

    def _synth_worker(self):
        while True:
            gen, text, style = self._speech_q.get()
            if gen != self._speech_gen:
                continue
            try:
                audio, sr = TTS(self.settings).synthesize(text, style)
                self._audio_q.put((gen, audio, sr, text))
            except Exception as e:  # noqa: BLE001
                self.emit({"type": "error", "text": f"Text-to-speech failed: {e}"})

    def _play_worker(self):
        while True:
            gen, audio, sr, text = self._audio_q.get()
            if gen != self._speech_gen:
                continue
            self.mic.speaking = True
            self._now_speaking = text
            prev = self.state
            self.set_state("speaking")
            try:
                self.player.play(audio, sr, self._on_tts_level)
            except Exception as e:  # noqa: BLE001
                self.emit({"type": "error", "text": f"Audio playback failed: {e}"})
            finally:
                self.mic.out_level = 0.0
                if self._audio_q.empty() and self._speech_q.empty():
                    self.mic.speaking = False
                    busy = self.task and not self.task.done()
                    if not busy:
                        self._engage_follow_up()
                    self.set_state(prev if busy and prev != "speaking" else self._idle_state())

    def _on_tts_level(self, v: float):
        self.mic.out_level = self.player.current_rms
        self.emit({"type": "level", "source": "tts", "value": round(v, 3)})

    # ------------------------------------------------------------------ barge-in
    def _on_barge(self, audio: np.ndarray) -> bool:
        """Called by the mic when someone seems to talk over the assistant.
        Returns True (and stops speaking) only for a real interruption."""
        try:
            text = STT(self.settings).transcribe(audio)
        except Exception:  # noqa: BLE001
            return False
        ok, why = self.is_interruption(text, self._now_speaking)
        self.emit({"type": "barge", "text": text, "accepted": ok, "reason": why})
        if ok:
            self.stop_speaking()
            self.mic.speaking = False
            self.set_state("listening")
        return ok

    @staticmethod
    def is_interruption(text: str, speaking: str) -> tuple[bool, str]:
        words = re.findall(r"[a-z']+|[\u0B80-\u0BFF]+", text.lower())
        if not words:
            return False, "no words (noise)"
        if INTERRUPT_WORDS.search(text):
            return True, "interrupt phrase"
        # Our own voice leaking from the speakers: most words match what we're saying.
        spoken = set(re.findall(r"[a-z']+", speaking.lower()))
        if spoken and sum(w in spoken for w in words) / len(words) >= 0.6:
            return False, "echo of my own voice"
        if all(w in BACKCHANNEL for w in words):
            return False, "backchannel"
        if len(words) < 2:
            return False, "too short"
        return True, "speech"

    # ------------------------------------------------------------------ memory
    def _memory_worker(self):
        while True:
            user_text, reply = self._mem_q.get()
            if not self.memory or not self.settings.get("memory", {}).get("auto_save", True):
                continue
            try:
                self.emit({"type": "memory_saving"})
                data = gm.extract(self.settings, self.memory, user_text, reply)
                changed = self.memory.apply(data, f"USER: {user_text}\nASSISTANT: {reply[:1000]}")
                self.emit({"type": "memory_update", **changed})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.emit({"type": "memory_update", "nodes": [], "edges": [], "invalidated": [], "error": str(e)})

    def _recall(self, text: str) -> str:
        if not self.memory:
            return text
        try:
            res = self.memory.search(text)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return text
        self.emit({"type": "memory_search", "query": text, "nodes": res["nodes"], "edges": res["edges"]})
        out = text
        if res["text"]:
            out += f"\n\n<memory>\nRelevant things you remember (from your knowledge graph):\n{res['text']}\n</memory>"
        return out + self._file_recall(text)

    def _file_recall(self, text: str) -> str:
        """Attach the best passages from the user's indexed files when they clearly match the message."""
        if not self.files or not (self.settings.get("files") or {}).get("auto_recall", True) or not self.files.folders():
            return ""
        if len(gm.keywords(text)) < 2:
            return ""
        try:
            hits = [h for h in self.files.search(text, limit=3) if h["coverage"] >= 0.5]
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return ""
        if not hits:
            return ""
        self.emit({"type": "file_search", "query": text, "auto": True,
                   "sources": [self.files.public_hit(h) for h in hits], "nodes": [h["doc_id"] for h in hits]})
        return ("\n\n<files>\nPassages from the user's own files that may be relevant (cite file and page if used):\n"
                + self.files.format_hits(hits, max_chars=900) + "\n</files>")

    def _t_search_files(self, a: dict) -> tools.ToolOutput:
        if not self.files:
            return tools.ToolOutput("File search is unavailable (graph memory failed to open).", is_error=True)
        return self.files.tool(a)

    # ------------------------------------------------------------------ prompt
    def system_prompt(self, vision: bool, background: bool = False, agent_id: str = "main") -> str:
        profile = self.memory.user_profile() if self.memory else ""
        ex = {
            "skills": skills.prompt_index(),
            "schedules": "\n".join(f"- [{j['id']}] {j['title']} ({j['kind']}, {j['when']}, next {j.get('next_run', '')[:16]})"
                                   for j in self.scheduler.upcoming(48)[:8]),
            "pcs": "\n".join(f"- {p.get('name')}" for p in self.settings.get("nodes") or []),
            "phones": self.phones.prompt_lines(),
            "apps": ", ".join(f"{s['name']} ({len(s['tools'])} tools)" for s in self.mcp.status() if s["state"] == "ready"),
            "background": "\n".join(f"- [{b['id']}] {b['goal'][:80]} — {b['status']}" for b in self.background.values()
                                    if b["status"] == "running"),
            "plan": self._plan_text(agent_id),
            "background_agent": background,
            "mood_tag": (self.settings.get("ui") or {}).get("orb", True),
            "home": self.home.cached_summary() if homeassistant.configured(self.settings) else "",
            "last_lang": lang.LABELS.get(self.last_lang, ""),
        }
        return persona.system_prompt(self.settings, vision, profile, ex)

    # ------------------------------------------------------------------ conversation
    async def handle_user(self, text: str, voice: bool = False, mood: dict | None = None, origin: str | None = None,
                          images: list[str] | None = None, context: str = ""):
        """origin: None = this PC; a phone device id = reply through that phone; "-" = a phone that wants no audio.
        images: base64 PNGs the user is showing (the phone's AR "what am I looking at?"), with a context note."""
        self.last_lang = lang.detect(text) or self.last_lang
        # A spoken yes/no answers a pending confirmation.
        if self.pending and voice and not images:
            fid = next(iter(self.pending))
            no = bool(NO.match(text) or re.search(lang.NO_TA, text, re.I))
            if YES.match(text) or no:
                self.emit({"type": "user", "text": text})
                self.resolve_confirm(fid, not no)
                return
        self.emit({"type": "user", "text": text})
        if self.teacher is not None:
            if STOP_TEACH.search(text):
                await asyncio.to_thread(self.teach_stop)
            else:
                self.teacher.note(text)
            return
        if self.task and not self.task.done():
            await self.interrupt()
        self.main.cancelled = False
        self.reply_to = origin
        self.task = asyncio.create_task(self._run(text, mood, images=images, context=context))

    async def interrupt(self):
        self.main.cancelled = True
        self.stop_speaking()
        for f in list(self.pending.values()):
            if not f.done():
                f.set_result(False)
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=60)
            except Exception:
                pass

    def resolve_confirm(self, fid: str, approve: bool):
        f = self.pending.get(fid)
        if f and not f.done():
            self.loop.call_soon_threadsafe(f.set_result, approve)

    async def confirm(self, call: llm.ToolCall, reason: str, runner: Runner | None = None) -> bool:
        fid = uuid.uuid4().hex[:8]
        fut = self.loop.create_future()
        self.pending[fid] = fut
        who = f"Background task “{runner.label}”: " if runner and runner.background else ""
        self.emit({"type": "confirm", "id": fid, "tool": call.name, "summary": tools.describe(call.name, call.args),
                   "reason": who + reason, "args": call.args, "agent": runner.id if runner else "main"})
        self.speak(f"{'A background task needs your OK. ' if who else ''}{reason}. Shall I go ahead?")
        try:
            return await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(fid, None)
            self.emit({"type": "confirm_done", "id": fid})

    def _decorate(self, text: str, mood: dict | None) -> str:
        extra = []
        if self.pending_offer and time.time() - self.pending_offer["at"] < 300:
            extra.append(f"<context>You just offered on your own: \"{self.pending_offer['text']}\" "
                         f"(you noticed {self.pending_offer['context']}). The message below may be the reply.</context>")
        self.pending_offer = None
        while self.context_notes:
            extra.append(f"<context>{self.context_notes.pop(0)}</context>")
        if mood:
            note = emotion.tone_note(self.settings.get("user_name") or "the user", mood)
            if note:
                extra.append(note)
        return "\n".join(extra + [text]) if extra else text

    async def _run(self, text: str, mood: dict | None = None, auto: bool = False, images: list[str] | None = None,
                   context: str = ""):
        prompt = self._decorate(await asyncio.to_thread(self._recall, text), mood)
        if context:
            prompt = f"<context>{context}</context>\n" + prompt
        if self.reply_to and not auto:
            prompt = "<context>The user is talking to you from their phone, away from this PC's screen.</context>\n" + prompt
        try:
            replies, ok = await self.main.run(prompt, images=images)
        finally:
            self.reply_to = None
        if ok and not auto:
            self._mem_q.put((text, " ".join(replies)))
        if self.state in ("thinking", "acting"):
            self.set_state(self._idle_state())
        if not replies or not self.settings["agent"].get("speak_replies", True):
            self._engage_follow_up()

    # ------------------------------------------------------------------ proactive
    def announce(self, text: str, kind: str = "info", context: str = ""):
        """Say something unprompted (reminder, briefing, offer, background result)."""
        self.emit({"type": "assistant", "text": text, "proactive": kind})
        self.speak(text)
        if context:
            self.context_notes.append(context)

    def offer(self, text: str, context: str):
        self.pending_offer = {"text": text, "context": context, "at": time.time()}
        self.emit({"type": "assistant", "text": text, "proactive": "offer"})
        self.speak(text)

    async def run_auto(self, prompt: str, title: str):
        """Run an automatic (scheduled) instruction through the main agent once it is free."""
        for _ in range(900):
            if not self._busy() and self.teacher is None:
                break
            await asyncio.sleep(2)
        self.emit({"type": "info", "text": f"⏰ {title}"})
        self.main.cancelled = False
        self.task = asyncio.create_task(self._run(prompt, auto=True))
        await self.task

    def _on_schedule(self, job: dict):
        user = self.settings.get("user_name") or "the user"
        if job["kind"] == "reminder":
            self.announce(f"Reminder: {job['message']}", "reminder",
                          f"At {dt.datetime.now():%H:%M} you reminded {user}: {job['message']}")
        elif job["kind"] == "briefing":
            asyncio.run_coroutine_threadsafe(self.run_auto(persona.briefing_prompt(self.settings, job["message"]),
                                                           job["title"]), self.loop)
        else:
            prompt = (f"[Automatic: scheduled task “{job['title']}”, not typed by {user} just now] "
                      f"{job['message']}\nDo it now, then tell {user} briefly what you did.")
            asyncio.run_coroutine_threadsafe(self.run_auto(prompt, f"Scheduled task: {job['title']}"), self.loop)
        self.emit({"type": "agenda"})

    # ------------------------------------------------------------------ plans (checkpoints)
    def _load_plans(self) -> dict:
        try:
            return json.loads(PLANS_FILE.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_plans(self):
        try:
            PLANS_FILE.write_text(json.dumps({k: v for k, v in self.plans.items() if k == "main"}, indent=1), "utf-8")
        except OSError:
            pass

    def _plan_text(self, agent_id: str) -> str:
        p = self.plans.get(agent_id)
        if not p or not p.get("steps"):
            return ""
        marks = {"done": "✓", "in_progress": "→", "failed": "✕", "skipped": "–", "pending": " "}
        rows = "\n".join(f"  [{marks.get(s['status'], ' ')}] {i + 1}. {s['text']}" + (f" ({s['note']})" if s.get("note") else "")
                         for i, s in enumerate(p["steps"]))
        return f"{p.get('title', 'Plan')} (updated {p.get('updated', '')[:16]}):\n{rows}"

    def plan_tool(self, agent_id: str, args: dict) -> tools.ToolOutput:
        action = args.get("action")
        now = dt.datetime.now().isoformat(timespec="seconds")
        if action == "set":
            steps = [s for s in (args.get("steps") or []) if str(s).strip()][:25]
            if not steps:
                return tools.ToolOutput("Give the steps.", is_error=True)
            self.plans[agent_id] = {"title": args.get("title") or "Plan", "updated": now, "created": now,
                                    "steps": [{"text": str(s), "status": "pending", "note": ""} for s in steps]}
        elif action == "update":
            p = self.plans.get(agent_id)
            if not p:
                return tools.ToolOutput("No plan yet: use action=set first.", is_error=True)
            i = int(args.get("step") or 0) - 1
            if not 0 <= i < len(p["steps"]):
                return tools.ToolOutput(f"Step must be 1..{len(p['steps'])}", is_error=True)
            p["steps"][i]["status"] = args.get("status") or "done"
            if args.get("note"):
                p["steps"][i]["note"] = args["note"][:200]
            p["updated"] = now
        elif action == "clear":
            self.plans.pop(agent_id, None)
        p = self.plans.get(agent_id)
        self._save_plans()
        self.emit({"type": "plan", "agent": agent_id, "plan": p})
        if not p:
            return tools.ToolOutput("Plan cleared.")
        done = sum(s["status"] in ("done", "skipped") for s in p["steps"])
        if done == len(p["steps"]):
            p["complete"] = True
            self._save_plans()
        return tools.ToolOutput(f"Plan saved ({done}/{len(p['steps'])} done). Checkpoint stored.")

    # ------------------------------------------------------------------ session-level tools
    def _t_schedule(self, a: dict) -> tools.ToolOutput:
        job = self.scheduler.add(a.get("title") or "", a["message"], a.get("kind") or "reminder", a.get("at"),
                                 a.get("in_minutes"), a.get("repeat") or "none", a.get("time"), a.get("days"),
                                 a.get("every_minutes"))
        self.emit({"type": "agenda"})
        return tools.ToolOutput(f"Scheduled [{job['id']}] “{job['title']}” ({job['kind']}, {scheduler.describe(job)}); "
                                f"next run {job['next_run'].replace('T', ' ')[:16]}.")

    def _t_list_schedules(self, a: dict) -> tools.ToolOutput:
        rows = [f"[{j['id']}] {j['title']} — {j['kind']}, {j['when']}, next {str(j.get('next_run'))[:16]}"
                + ("" if j.get("enabled") else " (disabled)") for j in self.scheduler.list()]
        return tools.ToolOutput("\n".join(rows) or "Nothing scheduled.")

    def _t_cancel_schedule(self, a: dict) -> tools.ToolOutput:
        ok = self.scheduler.cancel(a["id"])
        self.emit({"type": "agenda"})
        return tools.ToolOutput("Cancelled." if ok else "No such scheduled job.", is_error=not ok)

    def _t_use_skill(self, a: dict) -> tools.ToolOutput:
        sk = skills.get(a["name"])
        if not sk:
            return tools.ToolOutput("No skill like that. Known skills:\n" + (skills.prompt_index() or "(none)"), is_error=True)
        self.emit({"type": "skills"})
        return tools.ToolOutput(f"Skill “{sk.get('name')}” v{sk.get('version')}:\n{sk['body']}\n\nFollow it, adapting "
                                "to what you see. If something differs or you find a better way, call save_skill with "
                                "the same name and improved steps plus a note on what changed.")

    def _t_save_skill(self, a: dict) -> tools.ToolOutput:
        sk = skills.save(a["name"], a.get("description", ""), a["steps"], a.get("notes", ""))
        self.emit({"type": "skills"})
        return tools.ToolOutput(f"Saved skill “{sk['name']}” (v{sk['version']}).")

    def _t_teach(self, a: dict) -> tools.ToolOutput:
        act = a.get("action")
        if act == "start":
            return tools.ToolOutput(self.teach_start(a.get("name", "")))
        if act == "cancel":
            if self.teacher:
                self.teacher.stop()
            self.teacher = None
            self.emit({"type": "teach", "on": False})
            return tools.ToolOutput("Teach mode cancelled.")
        return tools.ToolOutput(self.teach_stop(announce=False))

    def teach_start(self, name: str = "") -> str:
        if self.teacher is not None:
            return "Already watching."
        rt = tools.ToolRuntime(self.settings)
        self.teacher = skills.Recorder(lambda: rt.t_screenshot(), tools.active_window_title)
        self.teacher.name = name
        self.teacher.start()
        self.emit({"type": "teach", "on": True})
        return ("Teach mode on: I'm watching. Do the task as you normally would and narrate if you like; "
                "say “I'm done” (or press Teach again) when finished.")

    def teach_stop(self, announce: bool = True) -> str:
        rec, self.teacher = self.teacher, None
        self.emit({"type": "teach", "on": False})
        if rec is None:
            return "I wasn't watching anything."
        data = rec.stop()
        if not data["timeline"].strip():
            msg = "I didn't catch any steps that time."
            if announce:
                self.announce(msg)
            return msg
        shots = data["shots"]
        pick = [shots[i] for i in sorted({round(i * (len(shots) - 1) / 5) for i in range(6)})] if len(shots) > 6 else shots
        prompt = (f"Name hint: {getattr(rec, 'name', '') or '(none)'}\nDuration: {data['seconds']:.0f}s\n"
                  f"Timeline of the demonstration:\n{data['timeline'][:12000]}\n\n"
                  + ("Screenshots attached in order: " + "; ".join(f"{t:.0f}s {c}" for t, c, _ in pick) if pick else ""))
        try:
            out = llm.complete(self.settings, skills.TEACH_SYSTEM, prompt, max_tokens=2000,
                               images=[b for _, _, b in pick] or None)
            d = gm.parse_json(out)
            sk = skills.save(d.get("name") or getattr(rec, "name", "") or "Learned workflow",
                             d.get("description", ""), d.get("steps") or out, d.get("notes", ""), source="teach")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            msg = f"I couldn't turn that into a skill: {e}"
            if announce:
                self.announce(msg)
            return msg
        self.emit({"type": "skills"})
        msg = f"Got it. I've learned “{sk['name']}”. Just ask and I'll do it."
        if announce:
            self.announce(msg, "skill", f"You learned a new skill by watching: {sk['name']}")
        return msg

    # background agents --------------------------------------------------------
    def _t_background(self, a: dict) -> tools.ToolOutput:
        act = a.get("action")
        if act == "start":
            if not a.get("goal"):
                return tools.ToolOutput("Give a goal.", is_error=True)
            fut = asyncio.run_coroutine_threadsafe(self.start_background(a["goal"]), self.loop)
            bid = fut.result(10)
            return tools.ToolOutput(f"Started background task {bid}. I'll be told when it finishes.")
        if act == "list":
            rows = [f"[{b['id']}] {b['status']} — {b['goal'][:100]} ({b['steps']} actions)" for b in self.background.values()]
            return tools.ToolOutput("\n".join(rows) or "No background tasks.")
        b = self.background.get(a.get("id", ""))
        if not b:
            return tools.ToolOutput("No such background task.", is_error=True)
        if act == "cancel":
            self.cancel_background(b["id"])
            return tools.ToolOutput("Cancelling it.")
        return tools.ToolOutput(f"{b['status']}: {b['result'] or b['last'] or '(no output yet)'}"[:4000])

    async def start_background(self, goal: str) -> str:
        bid = "bg" + uuid.uuid4().hex[:5]
        provider = llm.make_provider(self.settings)
        rt = tools.ToolRuntime(self.settings)
        rt.profile_dir = config.HOME / "browser-profile-bg"
        rt.vision, rt.memory, rt.on_memory = provider.vision, self.memory, self.emit
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=bid)
        runner = Runner(self, provider, rt, pool, agent_id=bid, label=goal[:60])
        rec = {"id": bid, "goal": goal, "status": "running", "started": dt.datetime.now().isoformat(timespec="seconds"),
               "result": "", "last": "", "steps": 0, "runner": runner}
        self.background[bid] = rec
        rec["task"] = asyncio.create_task(self._run_background(rec))
        self._emit_bg(rec)
        return bid

    async def _run_background(self, rec: dict):
        r: Runner = rec["runner"]
        user = self.settings.get("user_name") or "the user"
        prompt = (f"You are a background agent working for {user} while they keep chatting with the main assistant. "
                  f"Goal:\n{rec['goal']}\n\nWork autonomously to completion. Use the plan tool for multi-step work. "
                  f"When done, reply with a short summary of what you did and the result (2-4 sentences).")
        try:
            replies, ok = await r.run(prompt)
            rec["result"] = (replies[-1] if replies else "") or "Done."
            rec["status"] = "cancelled" if r.cancelled else ("done" if ok else "failed")
        except Exception as e:  # noqa: BLE001
            rec["status"], rec["result"] = "failed", str(e)
        finally:
            rec["steps"] = r.steps_done
            r.pool.submit(r.runtime.shutdown)
            r.pool.shutdown(wait=False)
        self._emit_bg(rec)
        if rec["status"] != "cancelled":
            word = "finished" if rec["status"] == "done" else "ran into trouble"
            self.announce(f"Background task {word}: {rec['result']}", "background",
                          f"Background task [{rec['id']}] “{rec['goal'][:80]}” {rec['status']}: {rec['result'][:600]}")

    def cancel_background(self, bid: str):
        b = self.background.get(bid)
        if b and b["status"] == "running":
            b["runner"].cancelled = True
            for fid, f in list(self.pending.items()):
                if not f.done():
                    f.set_result(False)

    def _emit_bg(self, rec: dict):
        rec["steps"] = rec["runner"].steps_done
        self.emit({"type": "bg", **self.bg_public(rec)})

    @staticmethod
    def bg_public(rec: dict) -> dict:
        return {k: rec[k] for k in ("id", "goal", "status", "started", "result", "steps")}

    # other PCs -----------------------------------------------------------------
    def _t_remote_pc(self, a: dict) -> tools.ToolOutput:
        tool = a.get("tool", "")
        if tool not in tools.NODE_TOOLS:
            return tools.ToolOutput(f"{tool} isn't available on other PCs. Use one of: {', '.join(sorted(tools.NODE_TOOLS))}",
                                    is_error=True)
        return nodes.call(self.settings, a.get("pc", ""), tool, a.get("args") or {})

    def _sync_node_server(self):
        cfg = self.settings.get("node", {})
        if cfg.get("enabled"):
            if not cfg.get("token"):
                cfg["token"] = nodes.new_token()
                config.save({"node": {"token": cfg["token"]}})
            want = int(cfg.get("port") or nodes.DEFAULT_PORT)
            if self.node_server and self.node_server.port != want:
                self.node_server.stop()
                self.node_server = None
            if not self.node_server:
                try:
                    self.node_server = nodes.NodeServer(self.settings)
                    self.node_server.start()
                except Exception as e:  # noqa: BLE001
                    self.node_server = None
                    self.emit({"type": "error", "text": f"Couldn't start remote control for other PCs: {e}"})
        elif self.node_server:
            self.node_server.stop()
            self.node_server = None

    # ------------------------------------------------------------------ status for the UI
    def agenda(self) -> dict:
        return {
            "plan": self.plans.get("main"),
            "background": [self.bg_public(b) for b in self.background.values()][-8:],
            "schedules": self.scheduler.list(),
            "teach": self.teacher is not None,
            "voice_mode": self.voice_mode,
            "wake_ready": self.mic.wake is not None,
            "mood": {k: self.mood.get(k) for k in ("mood", "cues")},
        }

    def shutdown(self):
        if self.gestures is not None:
            self.gestures.stop()
        self.wellbeing.stop()
        self.camera.stop()
        self.mic.close()
        self.scheduler.stop()
        if self.consolidator:
            self.consolidator.stop()
        if self.files:
            self.files.stop()
        self.awareness.stop()
        self.ambient.stop()
        self.mcp.close()
        if self.node_server:
            self.node_server.stop()
        self.phones.stop()
        self.tool_pool.submit(self.runtime.shutdown)
