"""Computer-use, browser-use, shell and file tools.

All tools run on a single dedicated worker thread (Playwright's sync API and
pyautogui both prefer a consistent thread). Each tool returns a ToolOutput with
text and, optionally, a PNG screenshot (base64) for vision-capable models.
"""
from __future__ import annotations

import base64
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
MAX_OUT = 12000
SHOT_MAX_W = 1280


@dataclass
class ToolOutput:
    text: str
    image_b64: str | None = None
    is_error: bool = False


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict
    required: list = field(default_factory=list)
    vision_only: bool = False
    raw_schema: dict | None = None
    group: str = ""            # "" always | "main" main conversation only | "nodes" when other PCs are paired

    def schema(self) -> dict:
        if self.raw_schema is not None:
            return self.raw_schema
        return {"type": "object", "properties": self.params, "required": self.required}


# --------------------------------------------------------------------------- specs
SPECS = [
    ToolSpec("run_shell",
             "Run a shell command on the user's computer and return stdout/stderr. "
             "Windows uses PowerShell, macOS/Linux use the default shell. Set elevated=true for "
             "commands that need administrator/root rights (the OS will show its own prompt).",
             {"command": {"type": "string"},
              "elevated": {"type": "boolean", "description": "Run as administrator/root."},
              "timeout": {"type": "integer", "description": "Seconds, default 120."},
              "cwd": {"type": "string"}},
             ["command"]),
    ToolSpec("screenshot", "Take a screenshot. Returns the image plus its size and the list of monitors; "
             "mouse coordinates are relative to the latest screenshot. monitor: 1 = primary (default: the last one "
             "you looked at), 2.. = other screens, 0 = all screens stitched together.",
             {"monitor": {"type": "integer"}}, [], vision_only=True),
    ToolSpec("mouse",
             "Mouse control using screenshot coordinates. action: move | click | double_click | right_click | "
             "drag | scroll. For drag give to_x/to_y. For scroll give amount (positive = up).",
             {"action": {"type": "string", "enum": ["move", "click", "double_click", "right_click", "drag", "scroll"]},
              "x": {"type": "integer"}, "y": {"type": "integer"},
              "to_x": {"type": "integer"}, "to_y": {"type": "integer"},
              "amount": {"type": "integer"}},
             ["action"]),
    ToolSpec("keyboard",
             "Type text or press keys. action=type types `text`. action=hotkey presses `keys` together, "
             "e.g. ['ctrl','c'] or ['command','space']. action=press presses a single key like 'enter'.",
             {"action": {"type": "string", "enum": ["type", "hotkey", "press"]},
              "text": {"type": "string"},
              "keys": {"type": "array", "items": {"type": "string"}}},
             ["action"]),
    ToolSpec("open", "Open an application by name, a file/folder path, or a URL with the system default handler.",
             {"target": {"type": "string"}}, ["target"]),
    ToolSpec("list_windows", "List open windows / running GUI apps.", {}, []),
    ToolSpec("read_file", "Read a text file (first ~12k chars).", {"path": {"type": "string"}}, ["path"]),
    ToolSpec("write_file", "Create or overwrite a text file.",
             {"path": {"type": "string"}, "content": {"type": "string"},
              "append": {"type": "boolean"}}, ["path", "content"]),
    ToolSpec("list_dir", "List a directory.", {"path": {"type": "string"}}, ["path"]),
    ToolSpec("clipboard", "Read or write the clipboard. action=read|write.",
             {"action": {"type": "string", "enum": ["read", "write"]}, "text": {"type": "string"}}, ["action"]),
    ToolSpec("browser",
             "Control a real Chromium browser (persistent profile, so logins stick). Actions: "
             "goto(url) | click(selector or text) | fill(selector, text, submit?) | press(key) | read() returns "
             "visible page text and links | screenshot() | back() | tabs() | switch_tab(index) | eval(script) | close().",
             {"action": {"type": "string",
                         "enum": ["goto", "click", "fill", "press", "read", "screenshot", "back", "tabs",
                                  "switch_tab", "eval", "close"]},
              "url": {"type": "string"}, "selector": {"type": "string"},
              "text": {"type": "string"}, "submit": {"type": "boolean"},
              "key": {"type": "string"}, "index": {"type": "integer"}, "script": {"type": "string"}},
             ["action"]),
    ToolSpec("wait", "Wait some seconds (for pages/apps to load).", {"seconds": {"type": "number"}}, ["seconds"]),
    ToolSpec("remember", "Save something to long-term graph memory right now (facts about the user, people, "
             "plans, preferences, corrections). Conversations are also saved automatically; use this when the "
             "user explicitly asks you to remember something.",
             {"fact": {"type": "string"}}, ["fact"]),
    ToolSpec("memory_search", "Search your long-term knowledge graph about the user (people, projects, "
             "preferences, past events). Use it whenever past context could help and it isn't already in "
             "the <memory> block.",
             {"query": {"type": "string"}}, ["query"]),
    ToolSpec("system_info", "CPU, memory, disk, battery, monitors and OS details.", {}, []),
    ToolSpec("weather", "Current weather and a 3-day forecast. location: city name; empty = the user's home "
             "location from settings, or a guess from the IP address.", {"location": {"type": "string"}}, []),
    ToolSpec("plan",
             "Show and track a step-by-step plan for multi-step jobs (3+ steps). action=set with `steps` (short "
             "imperative phrases) and a `title` before you start; action=update with `step` (1-based) and `status` "
             "(in_progress | done | failed | skipped) and an optional short `note` as you go. The user sees the plan "
             "and its progress live; each done step is a checkpoint.",
             {"action": {"type": "string", "enum": ["set", "update", "clear"]}, "title": {"type": "string"},
              "steps": {"type": "array", "items": {"type": "string"}}, "step": {"type": "integer"},
              "status": {"type": "string", "enum": ["in_progress", "done", "failed", "skipped"]},
              "note": {"type": "string"}}, ["action"]),
    ToolSpec("schedule",
             "Schedule a reminder (you'll say `message` at that time) or a task (you'll carry out `message` as an "
             "instruction at that time, e.g. 'clean the Downloads folder'). One-off: give `at` as local "
             "'YYYY-MM-DDTHH:MM' or `in_minutes`. Repeating: repeat = daily | weekdays | weekly (with `days` like "
             "['mon','thu']) | hourly | minutes (with `every_minutes`), plus `time` 'HH:MM'.",
             {"title": {"type": "string"}, "message": {"type": "string"},
              "kind": {"type": "string", "enum": ["reminder", "task"]}, "at": {"type": "string"},
              "in_minutes": {"type": "number"},
              "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekly", "hourly", "minutes"]},
              "time": {"type": "string"}, "days": {"type": "array", "items": {"type": "string"}},
              "every_minutes": {"type": "integer"}}, ["message"]),
    ToolSpec("list_schedules", "List scheduled reminders, tasks and briefings with their next run time.", {}, []),
    ToolSpec("cancel_schedule", "Cancel a scheduled job by id (from list_schedules) or exact title.",
             {"id": {"type": "string"}}, ["id"]),
    ToolSpec("use_skill", "Load a saved skill (a workflow you learned) by name, to follow its steps.",
             {"name": {"type": "string"}}, ["name"]),
    ToolSpec("save_skill",
             "Save or improve a reusable skill: a workflow you can repeat later. Use it after finishing a multi-step "
             "job the user is likely to ask for again, when the user says 'remember how to do this', or after using a "
             "skill when you learned something (same name = new version; put what changed in `notes`).",
             {"name": {"type": "string"}, "description": {"type": "string"},
              "steps": {"type": "string", "description": "Numbered steps in Markdown."},
              "notes": {"type": "string"}}, ["name", "description", "steps"]),
    ToolSpec("teach", "Teach mode: action=start begins watching the user demonstrate a workflow (window changes, "
             "clicks, typing, screenshots, narration); action=stop ends it and turns it into a saved skill.",
             {"action": {"type": "string", "enum": ["start", "stop", "cancel"]},
              "name": {"type": "string", "description": "Optional name for the skill."}}, ["action"], group="main"),
    ToolSpec("background_task",
             "Run a long job in the background with its own agent while you keep chatting with the user "
             "(research, downloads, file organising, long builds). action=start with a complete, self-contained "
             "`goal`; action=list; action=status/cancel with `id`. Background agents can't use the mouse or "
             "keyboard (so they don't fight the user); they use the shell, files, browser and connected apps. "
             "You'll be told when one finishes.",
             {"action": {"type": "string", "enum": ["start", "list", "status", "cancel"]},
              "goal": {"type": "string"}, "id": {"type": "string"}}, ["action"], group="main"),
    ToolSpec("remote_pc",
             "Run one of your tools on another paired computer. `pc` is its name (see the PCs list in your "
             "instructions), `tool` is a tool name like run_shell, screenshot, mouse, keyboard, open, list_windows, "
             "read_file, write_file, list_dir, clipboard, system_info, and `args` its arguments.",
             {"pc": {"type": "string"}, "tool": {"type": "string"}, "args": {"type": "object"}},
             ["pc", "tool"], group="nodes"),
    ToolSpec("phone",
             "Use the user's Android phone (through the Jarvis phone app). `device` picks a phone by name when there "
             "are several. Actions:\n"
             "- screen(query?): numbered list of what's on screen ([N] class \"text\" #id (x,y) flags). Read it before "
             "acting; every tap/type/swipe/scroll/key/open_app returns the new screen, so you rarely need to call it twice.\n"
             "- tap(index | x,y), long_press(index | x,y), type(text, index?, clear=true, submit?), "
             "swipe(direction up|down|left|right | x,y,to_x,to_y), scroll(direction, index?), "
             "key(key: back|home|recents|notifications|quick_settings|lock|enter|delete|volume_up|volume_down|power).\n"
             "- screenshot(): image of the screen (same coordinate space as screen).\n"
             "- open_app(app: name or package), list_apps(query?), open_url(url).\n"
             "- notifications(op: list|open|reply|dismiss, key, text, app, limit).\n"
             "- sms(op: list|send, to: name or number, text, query, limit); call(op: dial|answer|end|log, number); "
             "contacts(query).\n"
             "- files(op: list|info|read|write|mkdir|delete|move|pull|push, path, content, append, to); pull copies a phone "
             "file to this PC (save_to, default ~/Downloads); push sends a PC file (from_path) to the phone (path).\n"
             "- device_info, location, ring(on) to find the phone, torch(on), volume(stream, level 0-100, ringer), "
             "media(command: play|pause|toggle|next|previous), clipboard(op get|set, text), "
             "shell(command) with power mode, status (what the phone allows).\n"
             "Paths are relative to the phone's shared storage (e.g. Download/report.pdf, DCIM/Camera).",
             {"action": {"type": "string", "enum": [
                 "screen", "screenshot", "tap", "long_press", "type", "swipe", "scroll", "key", "open_app", "list_apps",
                 "open_url", "notifications", "sms", "call", "contacts", "files", "device_info", "location", "ring",
                 "torch", "volume", "media", "clipboard", "shell", "status"]},
              "device": {"type": "string"}, "index": {"type": "integer"},
              "x": {"type": "number"}, "y": {"type": "number"}, "to_x": {"type": "number"}, "to_y": {"type": "number"},
              "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
              "text": {"type": "string"}, "query": {"type": "string"}, "clear": {"type": "boolean"},
              "submit": {"type": "boolean"}, "key": {"type": "string"}, "app": {"type": "string"},
              "url": {"type": "string"}, "op": {"type": "string"}, "to": {"type": "string"},
              "number": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"},
              "append": {"type": "boolean"}, "save_to": {"type": "string"}, "from_path": {"type": "string"},
              "command": {"type": "string"}, "on": {"type": "boolean"}, "level": {"type": "integer"},
              "stream": {"type": "string", "enum": ["media", "ring", "alarm", "notification", "call"]},
              "ringer": {"type": "string", "enum": ["normal", "vibrate", "silent"]},
              "limit": {"type": "integer"}, "timeout": {"type": "integer"}},
             ["action"], group="phones"),
]

NODE_TOOLS = {"run_shell", "screenshot", "mouse", "keyboard", "open", "list_windows", "read_file", "write_file",
              "list_dir", "clipboard", "wait", "system_info", "weather"}
GUI_TOOLS = {"mouse", "keyboard", "screenshot", "teach"}


def specs_for(vision: bool, main: bool = True, nodes: bool = False, exclude: set | None = None,
              phones: bool = False) -> list[ToolSpec]:
    out = []
    for s in SPECS:
        if s.vision_only and not vision:
            continue
        if (s.group == "main" and not main) or (s.group == "nodes" and not nodes) or (s.group == "phones" and not phones):
            continue
        if exclude and s.name in exclude:
            continue
        out.append(s)
    return out


# --------------------------------------------------------------------------- risk
_DANGEROUS = re.compile(
    r"(\brm\b|\brmdir\b|\bdel\b|\berase\b|Remove-Item|\bformat\b|\bmkfs|\bdd\b|\bshutdown\b|\breboot\b|"
    r"Restart-Computer|Stop-Computer|\bkill(all)?\b|Stop-Process|taskkill|\bchmod\b|\bchown\b|\bsudo\b|"
    r"\breg\s+(add|delete)|Set-ItemProperty|\bsc\s+(delete|stop|config)|\bnetsh\b|\bdiskpart\b|"
    r"\bgit\s+(push|reset\s+--hard|clean)|\bpip\s+uninstall|\bapt(-get)?\s+(remove|purge)|"
    r"\bbrew\s+uninstall|\bwinget\s+uninstall|\bmv\b|Move-Item|>\s*/dev/|\bcurl\b.*\|\s*(ba)?sh|"
    r"Invoke-Expression|\biex\b|\bcrontab\b|schtasks|\blaunchctl\b|\bsystemctl\b)",
    re.IGNORECASE,
)
_READ_ONLY_BROWSER = {"goto", "read", "screenshot", "back", "tabs", "switch_tab", "close"}


def needs_confirmation(name: str, args: dict, mode: str) -> str | None:
    """Return a human-readable reason if this call should be confirmed, else None."""
    if mode == "never":
        return None
    if name == "run_shell":
        cmd = args.get("command", "")
        if args.get("elevated"):
            return "Runs a command with administrator rights"
        if _DANGEROUS.search(cmd):
            return "Command may change or delete things on your system"
        return "Runs a shell command" if mode == "always" else None
    if name == "write_file":
        p = Path(os.path.expanduser(args.get("path", "")))
        if p.exists():
            return "Overwrites an existing file" if not args.get("append") else ("Appends to a file" if mode == "always" else None)
        return "Creates a file" if mode == "always" else None
    if name == "browser":
        if args.get("action") == "eval":
            return "Runs JavaScript in the browser"
        if args.get("action") in ("fill",) and args.get("submit"):
            return "Submits a form on a web page" if mode == "always" else None
        return None if args.get("action") in _READ_ONLY_BROWSER or mode != "always" else "Interacts with a web page"
    if name == "remote_pc":
        inner = needs_confirmation(args.get("tool", ""), args.get("args") or {}, mode)
        if inner:
            return f"On {args.get('pc')}: {inner[0].lower()}{inner[1:]}"
        return f"Controls {args.get('pc')}" if mode == "always" else None
    if name == "phone":
        return _phone_risk(args, mode)
    if name == "schedule" and args.get("kind") == "task":
        return "Schedules a task I'll carry out automatically" if mode == "always" else None
    if mode == "always" and name in ("mouse", "keyboard", "open", "clipboard"):
        return "Controls your mouse/keyboard/apps"
    return None


_PHONE_READ_ONLY = {"screen", "screenshot", "list_apps", "contacts", "device_info", "location", "status"}


def _phone_risk(a: dict, mode: str) -> str | None:
    act, op = a.get("action", ""), a.get("op", "")
    if act == "sms" and op == "send":
        return "Sends a text message from your phone"
    if act == "call" and op in ("dial", "", None):
        return "Makes a phone call"
    if act == "call" and op in ("answer", "end"):
        return "Answers or ends a phone call"
    if act == "notifications" and op == "reply":
        return "Replies to a message from your phone"
    if act == "files" and op in ("delete", "move"):
        return "Deletes or moves files on your phone"
    if act == "files" and op in ("write", "push"):
        return "Writes a file on your phone"
    if act == "shell":
        return "Runs a shell command on your phone"
    if mode == "always" and act not in _PHONE_READ_ONLY and not (act in ("sms", "call", "files", "notifications")
                                                                  and op in ("list", "log", "read", "info", "pull")):
        return "Controls your phone"
    return None


def describe(name: str, args: dict) -> str:
    if name == "run_shell":
        return ("[admin] " if args.get("elevated") else "") + args.get("command", "")
    if name == "browser":
        a = args.get("action")
        extra = args.get("url") or args.get("selector") or args.get("key") or args.get("script") or ""
        return f"{a} {extra}".strip()
    if name in ("write_file", "read_file", "list_dir"):
        return args.get("path", "")
    if name == "keyboard":
        return args.get("text") or "+".join(args.get("keys") or []) or ""
    if name == "mouse":
        return f"{args.get('action')} ({args.get('x')}, {args.get('y')})"
    if name == "open":
        return args.get("target", "")
    if name == "remote_pc":
        return f"{args.get('pc')}: {args.get('tool')} " + describe(args.get("tool", ""), args.get("args") or {})
    if name == "phone":
        a = args
        bits = [a.get("device") or "", a.get("action", ""), a.get("op") or ""]
        detail = (a.get("to") or a.get("number") or a.get("app") or a.get("url") or a.get("path") or a.get("command")
                  or a.get("key") or a.get("direction") or "")
        if a.get("index") is not None:
            detail = f"#{a['index']}"
        elif a.get("x") is not None:
            detail = f"({a.get('x')}, {a.get('y')})"
        text = f" \"{a['text'][:80]}\"" if a.get("text") else ""
        return " ".join(b for b in bits + [str(detail)] if b).strip() + text
    if name == "smart_home":
        a = args
        bits = [a.get("action", ""), a.get("device") or a.get("service") or a.get("text") or ""]
        for k, unit in (("brightness", "%"), ("temperature", "°"), ("volume", "%"), ("color", ""), ("hvac_mode", ""),
                        ("source", "")):
            if a.get(k) not in (None, ""):
                bits.append(f"{a[k]}{unit}")
        return " ".join(str(b) for b in bits if b)
    if name == "plan":
        return args.get("title") or f"step {args.get('step')} {args.get('status', '')}".strip()
    if name in ("schedule", "background_task"):
        return args.get("title") or args.get("goal") or args.get("message") or args.get("action", "")
    return ", ".join(f"{k}={v}" for k, v in args.items())[:200]


# --------------------------------------------------------------------------- runtime
class ToolRuntime:
    def __init__(self, settings: dict):
        self.settings = settings
        self.memory = None          # GraphMemory, set by the session
        self.on_memory = None       # callback(event dict) for UI updates
        self._scale = 1.0          # screenshot px -> screen px
        self._offset = (0, 0)      # top-left of the monitor in the latest screenshot
        self._mon = 1              # monitor index of the latest screenshot
        self.vision = False        # set by the session: attach after-action screenshots?
        self.extra: dict = {}      # name -> callable(args) -> ToolOutput (session-level tools)
        self.profile_dir = config.BROWSER_PROFILE
        self._pw = None
        self._ctx = None
        self._page = None

    # dispatcher -------------------------------------------------------------
    def call(self, name: str, args: dict) -> ToolOutput:
        args = args or {}
        if name in self.extra:
            try:
                return self.extra[name](args)
            except Exception as e:  # noqa: BLE001
                return ToolOutput(f"{type(e).__name__}: {e}", is_error=True)
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return ToolOutput(f"Unknown tool {name}", is_error=True)
        try:
            out = fn(**args)
        except TypeError as e:
            return ToolOutput(f"Bad arguments for {name}: {e}", is_error=True)
        except Exception as e:  # noqa: BLE001 - report every failure to the model
            return ToolOutput(f"{type(e).__name__}: {e}", is_error=True)
        if self._should_verify(name, args, out):
            self._verify(out)
        return out

    # self-check -------------------------------------------------------------
    def _should_verify(self, name: str, args: dict, out: ToolOutput) -> bool:
        if out.is_error or not self.settings.get("agent", {}).get("self_check", True):
            return False
        if name == "mouse":
            return args.get("action") != "move"
        return name in ("keyboard", "open")

    def _verify(self, out: ToolOutput):
        """After acting on the GUI, look again so the model can confirm it actually worked."""
        time.sleep(0.6)
        title = active_window_title()
        note = f"\n[self-check] Active window now: {title or '(unknown)'}."
        if self.vision:
            try:
                shot = self.t_screenshot(self._mon)
                out.image_b64 = shot.image_b64
                note += " After-action screenshot attached: check it worked before moving on; if not, try another way."
            except Exception as e:  # noqa: BLE001
                note += f" (screenshot failed: {e})"
        out.text += note

    # shell ------------------------------------------------------------------
    def t_run_shell(self, command: str, elevated: bool = False, timeout: int = 120, cwd: str | None = None):
        cwd = os.path.expanduser(cwd) if cwd else os.path.expanduser("~")
        if elevated:
            return self._run_elevated(command, timeout, cwd)
        if IS_WIN:
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            p = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                               executable=os.environ.get("SHELL", "/bin/sh"))
        return self._fmt_proc(p.returncode, p.stdout, p.stderr)

    def _run_elevated(self, command: str, timeout: int, cwd: str) -> ToolOutput:
        if IS_WIN:
            out = Path(tempfile.gettempdir()) / f"jarvis_elev_{os.getpid()}_{int(time.time())}.txt"
            inner = f"Set-Location '{cwd}'; & {{ {command} }} *> '{out}'"
            enc = base64.b64encode(inner.encode("utf-16-le")).decode()
            ps = (f"Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
                  f"-ArgumentList '-NoProfile','-EncodedCommand','{enc}'")
            p = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                               timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            body = out.read_text("utf-8", errors="replace") if out.exists() else ""
            out.unlink(missing_ok=True)
            return self._fmt_proc(p.returncode, body, p.stderr)
        if IS_MAC:
            esc = f"cd {shlex_quote(cwd)} && {command}".replace("\\", "\\\\").replace('"', '\\"')
            argv = ["osascript", "-e", f'do shell script "{esc}" with administrator privileges']
        else:
            argv = ["pkexec", "sh", "-c", f"cd {shlex_quote(cwd)} && {command}"] if shutil.which("pkexec") \
                else ["sudo", "-A", "sh", "-c", command]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return self._fmt_proc(p.returncode, p.stdout, p.stderr)

    @staticmethod
    def _fmt_proc(code, out, err) -> ToolOutput:
        text = f"exit code {code}\n"
        if out:
            text += f"stdout:\n{out[-MAX_OUT:]}\n"
        if err:
            text += f"stderr:\n{err[-4000:]}\n"
        return ToolOutput(text.strip(), is_error=code != 0)

    # screen -----------------------------------------------------------------
    @staticmethod
    def monitors() -> list[dict]:
        import mss
        with mss.mss() as sct:
            return [dict(m) for m in sct.monitors]

    def t_screenshot(self, monitor: int | None = None):
        import mss
        from PIL import Image
        with mss.mss() as sct:
            mons = sct.monitors                      # [0] = all screens, [1] = primary, [2..] = others
            idx = self._mon if monitor is None else int(monitor)
            idx = max(0, min(idx, len(mons) - 1))
            mon = mons[idx]
            raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            listing = "; ".join(f"{i}: {m['width']}x{m['height']} at ({m['left']},{m['top']})" + (" primary" if i == 1 else "")
                                for i, m in enumerate(mons) if i > 0)
        lw, lh = mon["width"], mon["height"]           # logical size (handles HiDPI / Retina)
        if img.size != (lw, lh):
            img = img.resize((lw, lh))
        self._mon, self._offset, self._scale = idx, (mon["left"], mon["top"]), 1.0
        if img.width > SHOT_MAX_W:
            self._scale = img.width / SHOT_MAX_W
            img = img.resize((SHOT_MAX_W, round(img.height / self._scale)))
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        which = "all screens" if idx == 0 else f"monitor {idx}"
        return ToolOutput(f"Screenshot of {which}: {img.width}x{img.height} (screen {lw}x{lh}). Monitors: {listing}.",
                          base64.b64encode(buf.getvalue()).decode())

    def _xy(self, x, y):
        if x is None or y is None:
            return None, None
        return self._offset[0] + round(x * self._scale), self._offset[1] + round(y * self._scale)

    def t_mouse(self, action: str, x=None, y=None, to_x=None, to_y=None, amount=None):
        import pyautogui
        pyautogui.FAILSAFE = True
        sx, sy = self._xy(x, y)
        if action == "move":
            pyautogui.moveTo(sx, sy, duration=0.15)
        elif action == "click":
            pyautogui.click(sx, sy)
        elif action == "double_click":
            pyautogui.doubleClick(sx, sy)
        elif action == "right_click":
            pyautogui.rightClick(sx, sy)
        elif action == "drag":
            tx, ty = self._xy(to_x, to_y)
            pyautogui.moveTo(sx, sy)
            pyautogui.dragTo(tx, ty, duration=0.4, button="left")
        elif action == "scroll":
            if sx is not None:
                pyautogui.moveTo(sx, sy)
            pyautogui.scroll(int(amount or -5) * (1 if IS_MAC else 100))
        time.sleep(0.3)
        return ToolOutput(f"mouse {action} done")

    def t_keyboard(self, action: str, text: str = "", keys: list | None = None):
        import pyautogui
        if action == "type":
            if text.isascii():
                pyautogui.write(text, interval=0.01)
            else:  # unicode: paste via clipboard
                import pyperclip
                pyperclip.copy(text)
                pyautogui.hotkey("command" if IS_MAC else "ctrl", "v")
        elif action == "hotkey":
            pyautogui.hotkey(*[k.lower() for k in (keys or [])])
        elif action == "press":
            pyautogui.press((keys or [text])[0].lower())
        time.sleep(0.2)
        return ToolOutput(f"keyboard {action} done")

    def t_open(self, target: str):
        t = os.path.expanduser(target)
        if IS_WIN:
            if re.match(r"^[a-z]+://", t) or os.path.exists(t):
                os.startfile(t)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["powershell", "-NoProfile", "-Command", f"Start-Process '{t}'"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif IS_MAC:
            argv = ["open", t] if (os.path.exists(t) or "://" in t) else ["open", "-a", t]
            subprocess.Popen(argv)
        else:
            if os.path.exists(t) or "://" in t:
                subprocess.Popen(["xdg-open", t])
            else:
                subprocess.Popen([t], start_new_session=True)
        time.sleep(1.0)
        return ToolOutput(f"Opened {target}")

    def t_list_windows(self):
        if IS_WIN:
            return self.t_run_shell("Get-Process | Where-Object {$_.MainWindowTitle} | "
                                    "Select-Object ProcessName,Id,MainWindowTitle | Format-Table -AutoSize | Out-String -Width 200")
        if IS_MAC:
            return self.t_run_shell("osascript -e 'tell application \"System Events\" to get name of "
                                    "(processes where background only is false)'")
        if shutil.which("wmctrl"):
            return self.t_run_shell("wmctrl -l")
        return self.t_run_shell("ps -eo pid,comm --sort=-%mem | head -40")

    # files ------------------------------------------------------------------
    def t_read_file(self, path: str):
        p = Path(os.path.expanduser(path))
        return ToolOutput(p.read_text("utf-8", errors="replace")[:MAX_OUT])

    def t_write_file(self, path: str, content: str, append: bool = False):
        p = Path(os.path.expanduser(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8") as f:
            f.write(content)
        return ToolOutput(f"Wrote {len(content)} chars to {p}")

    def t_list_dir(self, path: str):
        p = Path(os.path.expanduser(path))
        rows = []
        for e in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))[:300]:
            rows.append(("[dir] " if e.is_dir() else "      ") + e.name)
        return ToolOutput("\n".join(rows) or "(empty)")

    def t_clipboard(self, action: str, text: str = ""):
        import pyperclip
        if action == "write":
            pyperclip.copy(text)
            return ToolOutput("Copied to clipboard")
        return ToolOutput(pyperclip.paste()[:MAX_OUT])

    def t_wait(self, seconds: float):
        time.sleep(min(float(seconds), 30))
        return ToolOutput(f"Waited {seconds}s")

    def t_remember(self, fact: str):
        if self.memory is None:
            return ToolOutput("Memory is unavailable", is_error=True)
        from . import memory as gm
        try:
            data = gm.extract(self.settings, self.memory, f"Please remember: {fact}", "")
        except Exception:  # noqa: BLE001 - fall back to a plain note
            data = {}
        if not data.get("facts"):
            data = {"facts": [{"source": "USER", "relation": "noted", "target": "Notes", "fact": fact}]}
        changed = self.memory.apply(data, f"remember: {fact}")
        if self.on_memory:
            self.on_memory({"type": "memory_update", **changed})
        return ToolOutput(f"Saved to memory ({len(changed['edges'])} facts, {len(changed['nodes'])} entities)")

    def t_memory_search(self, query: str):
        if self.memory is None:
            return ToolOutput("Memory is unavailable", is_error=True)
        res = self.memory.search(query)
        if self.on_memory:
            self.on_memory({"type": "memory_search", "query": query, "nodes": res["nodes"], "edges": res["edges"]})
        return ToolOutput(res["text"] or "Nothing relevant in memory.")

    def t_system_info(self):
        import psutil
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(os.path.expanduser("~"))
        bat = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        lines = [
            f"OS: {platform.platform()}",
            f"CPU: {psutil.cpu_count()} cores, {psutil.cpu_percent(interval=0.5)}% used",
            f"RAM: {vm.used / 1e9:.1f}/{vm.total / 1e9:.1f} GB",
            f"Disk (home): {du.used / 1e9:.0f}/{du.total / 1e9:.0f} GB",
        ]
        if bat:
            lines.append(f"Battery: {bat.percent:.0f}% {'charging' if bat.power_plugged else 'on battery'}")
        try:
            mons = self.monitors()[1:]
            lines.append("Monitors: " + "; ".join(f"{i + 1}: {m['width']}x{m['height']}" for i, m in enumerate(mons)))
        except Exception:  # noqa: BLE001
            pass
        return ToolOutput("\n".join(lines))

    def t_weather(self, location: str = ""):
        return ToolOutput(weather_report(location or self.settings.get("briefings", {}).get("location", "")))

    # browser ----------------------------------------------------------------
    def _browser(self):
        if self._ctx is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            headless = bool(self.settings.get("agent", {}).get("headless_browser"))
            kw = dict(headless=headless, viewport={"width": 1280, "height": 800})
            try:   # prefer the user's installed Chrome/Edge, fall back to bundled Chromium
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), channel="msedge" if IS_WIN else "chrome", **kw)
            except Exception:
                self._ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kw)
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        if self._page.is_closed():
            self._page = self._ctx.pages[-1] if self._ctx.pages else self._ctx.new_page()
        return self._page

    def t_browser(self, action: str, url: str = "", selector: str = "", text: str = "", submit: bool = False,
                  key: str = "", index: int = 0, script: str = ""):
        if action == "close":
            if self._ctx:
                self._ctx.close()
                self._pw.stop()
            self._ctx = self._pw = self._page = None
            return ToolOutput("Browser closed")
        page = self._browser()
        if action == "goto":
            if not re.match(r"^[a-z]+://", url):
                url = "https://" + url
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return ToolOutput(f"Loaded {page.url} — title: {page.title()}\n\n{self._page_text(page, 3000)}")
        if action == "click":
            loc = self._locate(page, selector or text)
            loc.click(timeout=10000)
            page.wait_for_load_state("domcontentloaded")
            return ToolOutput(f"Clicked. Now at {page.url}")
        if action == "fill":
            loc = self._locate(page, selector)
            loc.fill(text, timeout=10000)
            if submit:
                loc.press("Enter")
                page.wait_for_load_state("domcontentloaded")
            return ToolOutput(f"Filled{' and submitted' if submit else ''}. Now at {page.url}")
        if action == "press":
            page.keyboard.press(key or text)
            return ToolOutput(f"Pressed {key or text}")
        if action == "read":
            return ToolOutput(f"{page.url} — {page.title()}\n\n{self._page_text(page, MAX_OUT)}")
        if action == "screenshot":
            png = page.screenshot()
            return ToolOutput(f"Browser screenshot of {page.url}", base64.b64encode(png).decode())
        if action == "back":
            page.go_back()
            return ToolOutput(f"Back at {page.url}")
        if action == "tabs":
            return ToolOutput("\n".join(f"{i}: {p.title()} — {p.url}" for i, p in enumerate(self._ctx.pages)))
        if action == "switch_tab":
            self._page = self._ctx.pages[index]
            self._page.bring_to_front()
            return ToolOutput(f"Switched to {self._page.url}")
        if action == "eval":
            return ToolOutput(str(page.evaluate(script))[:MAX_OUT])
        return ToolOutput(f"Unknown browser action {action}", is_error=True)

    @staticmethod
    def _locate(page, target: str):
        if not target:
            raise ValueError("selector or text required")
        if re.match(r"^([#.\[/]|[a-z]+[\[.#:]|xpath=|css=|text=|role=)", target):
            return page.locator(target).first
        # treat as visible text: buttons/links first, then anything
        for loc in (page.get_by_role("button", name=target), page.get_by_role("link", name=target),
                    page.get_by_placeholder(target), page.get_by_label(target), page.get_by_text(target)):
            if loc.count():
                return loc.first
        return page.locator(target).first

    @staticmethod
    def _page_text(page, limit: int) -> str:
        js = """() => {
          const t = document.body ? document.body.innerText : '';
          const links = [...document.querySelectorAll('a[href]')].slice(0, 60)
            .map(a => (a.innerText || '').trim().slice(0, 80) + ' -> ' + a.href).filter(s => !s.startsWith(' ->'));
          const inputs = [...document.querySelectorAll('input,textarea,select,button')].slice(0, 40)
            .map(e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (e.name ? `[name="${e.name}"]` : '')
                 + ' ' + (e.placeholder || e.innerText || e.value || '').trim().slice(0, 50));
          return t + '\\n\\nLINKS:\\n' + links.join('\\n') + '\\n\\nINPUTS:\\n' + inputs.join('\\n');
        }"""
        try:
            return page.evaluate(js)[:limit]
        except Exception as e:  # noqa: BLE001
            return f"(could not read page: {e})"

    def shutdown(self):
        try:
            if self._ctx:
                self._ctx.close()
                self._pw.stop()
        except Exception:
            pass


WMO = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "light drizzle",
       53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
       67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "showers",
       81: "showers", 82: "violent showers", 85: "snow showers", 86: "snow showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with hail"}


def weather_report(location: str = "") -> str:
    """Open-Meteo (free, no key). Location by name, else by IP."""
    import httpx
    with httpx.Client(timeout=15) as c:
        if location.strip():
            g = c.get("https://geocoding-api.open-meteo.com/v1/search",
                      params={"name": location.split(",")[0].strip(), "count": 1}).json()
            if not g.get("results"):
                return f"Couldn't find a place called {location}."
            r = g["results"][0]
            lat, lon, place = r["latitude"], r["longitude"], f"{r['name']}, {r.get('country', '')}"
        else:
            ip = c.get("https://ipinfo.io/json").json()
            lat, lon = map(float, ip["loc"].split(","))
            place = f"{ip.get('city')}, {ip.get('country')} (approximate, from IP)"
        w = c.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 3,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"}).json()
    cur, d = w["current"], w["daily"]
    lines = [f"{place}: now {cur['temperature_2m']}°C (feels {cur['apparent_temperature']}°C), "
             f"{WMO.get(cur['weather_code'], 'code ' + str(cur['weather_code']))}, humidity {cur['relative_humidity_2m']}%, "
             f"wind {cur['wind_speed_10m']} km/h."]
    for i, day in enumerate(d["time"]):
        lines.append(f"{day}: {WMO.get(d['weather_code'][i], '?')}, {d['temperature_2m_min'][i]}–{d['temperature_2m_max'][i]}°C, "
                     f"rain chance {d['precipitation_probability_max'][i]}%")
    return "\n".join(lines)


def active_window_title() -> str:
    try:
        if IS_WIN:
            import ctypes
            u = ctypes.windll.user32  # type: ignore[attr-defined]
            h = u.GetForegroundWindow()
            n = u.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            return buf.value
        if IS_MAC:
            r = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of first '
                                'application process whose frontmost is true'], capture_output=True, text=True, timeout=3)
            return r.stdout.strip()
        if shutil.which("xdotool"):
            r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=3)
            return r.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)
