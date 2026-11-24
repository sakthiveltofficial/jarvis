"""The conversational agent loop, voice pipeline and confirmation gate."""
from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import re
import threading
import traceback
import uuid
from typing import Awaitable, Callable

import numpy as np

from . import config, llm, persona, tools
from .audio import Mic, Player
from .speech import STT, TTS

YES = re.compile(r"^\W*(yes|yeah|yep|sure|ok(ay)?|go( ahead)?|do it|confirm(ed)?|approved?|proceed|please do)\b", re.I)
NO = re.compile(r"^\W*(no|nope|don'?t|stop|cancel|deny|negative|abort|wait)\b", re.I)


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


class Session:
    def __init__(self, emit: Callable[[dict], Awaitable[None]]):
        self._emit_async = emit
        self.loop = asyncio.get_event_loop()
        self.settings = config.load()
        self.provider: llm.Provider = llm.make_provider(self.settings)
        self.tool_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tools")
        self.runtime = tools.ToolRuntime(self.settings)
        self.mic = Mic(self.settings, self._on_mic_level, self._on_utterance)
        self.player = Player(self.settings)
        self.task: asyncio.Task | None = None
        self.cancelled = False
        self.pending: dict[str, asyncio.Future] = {}
        self.state = "idle"
        self._speech_q: queue.Queue = queue.Queue()
        self._audio_q: queue.Queue = queue.Queue()
        self._speech_gen = 0
        threading.Thread(target=self._synth_worker, daemon=True).start()
        threading.Thread(target=self._play_worker, daemon=True).start()
        STT.preload(self.settings)

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

    def _on_mic_level(self, lvl: float):
        if self.mic.mode != "off":
            self.emit({"type": "level", "source": "mic", "value": round(lvl, 3)})

    # ------------------------------------------------------------------ settings
    def apply_settings(self, new: dict):
        keys = new.pop("api_keys", None) or {}
        keys = {k: v for k, v in keys.items() if v is not None and not str(v).startswith("••••")}
        if keys:
            new["api_keys"] = keys
        old_llm = dict(self.settings["llm"])
        self.settings.clear()
        self.settings.update(config.save(new))
        if self.settings["llm"] != old_llm or keys:
            self.provider = llm.make_provider(self.settings)
            self.emit({"type": "info", "text": "Model settings changed — starting a fresh conversation."})
        STT.preload(self.settings)

    def reset(self):
        self.provider.reset()

    # ------------------------------------------------------------------ voice input
    def ptt_start(self):
        self.stop_speaking()
        self.mic.ptt_start()
        self.set_state("listening")

    def ptt_stop(self):
        audio = self.mic.ptt_stop()
        if len(audio) < 16000 * 0.3:
            self.set_state("idle")
            return
        threading.Thread(target=self._on_utterance, args=(audio,), daemon=True).start()

    def set_handsfree(self, on: bool):
        self.mic.set_handsfree(on)
        self.emit({"type": "handsfree", "on": on})
        if on and self.state == "idle":
            self.set_state("listening")

    def _on_utterance(self, audio: np.ndarray):
        self.set_state("transcribing")
        try:
            text = STT(self.settings).transcribe(audio)
        except Exception as e:  # noqa: BLE001
            self.emit({"type": "error", "text": f"Speech recognition failed: {e}"})
            self.set_state("idle")
            return
        if not text or re.fullmatch(r"[\W_]*(thank you\.?|you)?[\W_]*", text, re.I):
            self.set_state("listening" if self.mic.mode == "handsfree" else "idle")
            return
        asyncio.run_coroutine_threadsafe(self.handle_user(text, voice=True), self.loop)

    # ------------------------------------------------------------------ voice output
    def speak(self, text: str):
        if not self.settings["agent"].get("speak_replies", True) or self.settings["tts"].get("provider") == "none":
            return
        for s in sentences(speakable(text)):
            self._speech_q.put((self._speech_gen, s))

    def stop_speaking(self):
        self._speech_gen += 1
        self.player.stop()

    def _synth_worker(self):
        while True:
            gen, text = self._speech_q.get()
            if gen != self._speech_gen:
                continue
            try:
                audio, sr = TTS(self.settings).synthesize(text)
                self._audio_q.put((gen, audio, sr))
            except Exception as e:  # noqa: BLE001
                self.emit({"type": "error", "text": f"Text-to-speech failed: {e}"})

    def _play_worker(self):
        while True:
            gen, audio, sr = self._audio_q.get()
            if gen != self._speech_gen:
                continue
            self.mic.muted = True
            prev = self.state
            self.set_state("speaking")
            try:
                self.player.play(audio, sr, lambda v: self.emit({"type": "level", "source": "tts", "value": round(v, 3)}))
            except Exception as e:  # noqa: BLE001
                self.emit({"type": "error", "text": f"Audio playback failed: {e}"})
            finally:
                if self._audio_q.empty() and self._speech_q.empty():
                    self.mic.muted = False
                    busy = self.task and not self.task.done()
                    self.set_state(prev if busy and prev != "speaking" else
                                   ("listening" if self.mic.mode == "handsfree" else "idle"))

    # ------------------------------------------------------------------ conversation
    async def handle_user(self, text: str, voice: bool = False):
        # A spoken yes/no answers a pending confirmation.
        if self.pending and voice:
            fid = next(iter(self.pending))
            if YES.match(text) or NO.match(text):
                self.emit({"type": "user", "text": text})
                self.resolve_confirm(fid, bool(YES.match(text)))
                return
        self.emit({"type": "user", "text": text})
        if self.task and not self.task.done():
            await self.interrupt()
        self.cancelled = False
        self.task = asyncio.create_task(self._run(text))

    async def interrupt(self):
        self.cancelled = True
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

    async def _confirm(self, call: llm.ToolCall, reason: str) -> bool:
        fid = uuid.uuid4().hex[:8]
        fut = self.loop.create_future()
        self.pending[fid] = fut
        self.emit({"type": "confirm", "id": fid, "tool": call.name, "summary": tools.describe(call.name, call.args),
                   "reason": reason, "args": call.args})
        self.speak(f"{reason}. Shall I go ahead?")
        try:
            return await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(fid, None)
            self.emit({"type": "confirm_done", "id": fid})

    async def _run(self, text: str):
        p = self.provider
        mark = len(p.history)
        p.add_user(text)
        tool_specs = tools.specs_for(p.vision)
        max_steps = int(self.settings["agent"].get("max_steps", 40))
        try:
            for _ in range(max_steps):
                self.set_state("thinking")
                system = persona.system_prompt(self.settings, p.vision)
                step = await asyncio.to_thread(p.step, system, tool_specs)
                if step.text.strip():
                    self.emit({"type": "assistant", "text": step.text.strip()})
                    self.speak(step.text)
                if not step.tool_calls:
                    break
                results = []
                for call in step.tool_calls:
                    if self.cancelled:
                        results.append((call, tools.ToolOutput("Cancelled: the user interrupted.", is_error=True)))
                        continue
                    reason = tools.needs_confirmation(call.name, call.args,
                                                      self.settings["agent"].get("confirm_mode", "risky"))
                    if reason and not await self._confirm(call, reason):
                        results.append((call, tools.ToolOutput("The user declined this action.", is_error=True)))
                        continue
                    self.set_state("acting")
                    cid = uuid.uuid4().hex[:8]
                    self.emit({"type": "tool_start", "id": cid, "tool": call.name,
                               "summary": tools.describe(call.name, call.args)})
                    out = await self.loop.run_in_executor(self.tool_pool, self.runtime.call, call.name, call.args)
                    self.emit({"type": "tool_end", "id": cid, "ok": not out.is_error, "text": out.text[:1500],
                               "image": out.image_b64})
                    results.append((call, out))
                p.add_tool_results(results)
                if self.cancelled:
                    break
            else:
                self.emit({"type": "assistant", "text": "I've hit my step limit for this task. Want me to keep going?"})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            del p.history[mark:]   # drop the failed turn so the conversation stays valid
            self.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
            self.speak("Sorry, I ran into a problem talking to the model. The details are on screen.")
        finally:
            if self.state in ("thinking", "acting"):
                self.set_state("listening" if self.mic.mode == "handsfree" else "idle")

    def shutdown(self):
        self.mic.close()
        self.tool_pool.submit(self.runtime.shutdown)
