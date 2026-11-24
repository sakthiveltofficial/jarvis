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

    def schema(self) -> dict:
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
    ToolSpec("screenshot", "Take a screenshot of the whole screen. Returns the image plus its size; "
             "use those image coordinates for mouse tools.", {}, [], vision_only=True),
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
    ToolSpec("system_info", "CPU, memory, disk, battery and OS details.", {}, []),
]


def specs_for(vision: bool) -> list[ToolSpec]:
    return [s for s in SPECS if vision or not s.vision_only]


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
    if mode == "always" and name in ("mouse", "keyboard", "open", "clipboard"):
        return "Controls your mouse/keyboard/apps"
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
    return ", ".join(f"{k}={v}" for k, v in args.items())[:200]


# --------------------------------------------------------------------------- runtime
class ToolRuntime:
    def __init__(self, settings: dict):
        self.settings = settings
        self.memory = None          # GraphMemory, set by the session
        self.on_memory = None       # callback(event dict) for UI updates
        self._scale = 1.0          # screenshot px -> screen px
        self._pw = None
        self._ctx = None
        self._page = None

    # dispatcher -------------------------------------------------------------
    def call(self, name: str, args: dict) -> ToolOutput:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return ToolOutput(f"Unknown tool {name}", is_error=True)
        try:
            return fn(**(args or {}))
        except TypeError as e:
            return ToolOutput(f"Bad arguments for {name}: {e}", is_error=True)
        except Exception as e:  # noqa: BLE001 - report every failure to the model
            return ToolOutput(f"{type(e).__name__}: {e}", is_error=True)

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
    def t_screenshot(self):
        import mss
        from PIL import Image
        with mss.mss() as sct:
            mon = sct.monitors[1]
            raw = sct.grab(mon)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        import pyautogui
        sw, sh = pyautogui.size()           # logical size (handles HiDPI)
        img = img.resize((sw, sh)) if img.size != (sw, sh) else img
        self._scale = 1.0
        if img.width > SHOT_MAX_W:
            self._scale = img.width / SHOT_MAX_W
            img = img.resize((SHOT_MAX_W, round(img.height / self._scale)))
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return ToolOutput(f"Screenshot {img.width}x{img.height} (screen {sw}x{sh}).",
                          base64.b64encode(buf.getvalue()).decode())

    def _xy(self, x, y):
        return (round(x * self._scale), round(y * self._scale)) if x is not None and y is not None else (None, None)

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
        return ToolOutput("\n".join(lines))

    # browser ----------------------------------------------------------------
    def _browser(self):
        if self._ctx is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            headless = bool(self.settings.get("agent", {}).get("headless_browser"))
            kw = dict(headless=headless, viewport={"width": 1280, "height": 800})
            try:   # prefer the user's installed Chrome/Edge, fall back to bundled Chromium
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(config.BROWSER_PROFILE), channel="msedge" if IS_WIN else "chrome", **kw)
            except Exception:
                self._ctx = self._pw.chromium.launch_persistent_context(str(config.BROWSER_PROFILE), **kw)
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


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)
