"""Skills Jarvis learns: reusable, improvable workflows.

A skill is a Markdown file in ~/.jarvis/skills/<slug>.md:

    ---
    name: Export monthly invoices
    description: Download last month's invoices from the billing portal and file them in Documents/Invoices.
    version: 3
    uses: 7
    updated: 2026-11-25T10:12:00
    ---
    ## Steps
    1. ...
    ## Notes
    - Learned 2026-11-25: the portal's "Export" button moved to the ⋯ menu.

Skills come from three places: Jarvis saving one after finishing a multi-step job, the user saying
"remember how to do this", and teach mode (the user demonstrates; Jarvis watches window changes,
clicks, typing and screenshots, then writes the steps). Each time a skill is used and something
was learned, Jarvis updates it, bumping the version. That's how skills improve over time.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time

from . import config

DIR = config.HOME / "skills"
DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "skill"


def _parse(text: str) -> tuple[dict, str]:
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = m.group(2)
    return meta, body.strip()


def _render(meta: dict, body: str) -> str:
    head = "\n".join(f"{k}: {str(v).replace(chr(10), ' ')}" for k, v in meta.items())
    return f"---\n{head}\n---\n{body.strip()}\n"


def list_skills() -> list[dict]:
    out = []
    for p in sorted(DIR.glob("*.md")):
        try:
            meta, body = _parse(p.read_text("utf-8"))
        except OSError:
            continue
        out.append({"id": p.stem, "name": meta.get("name", p.stem), "description": meta.get("description", ""),
                    "version": int(meta.get("version", 1) or 1), "uses": int(meta.get("uses", 0) or 0),
                    "updated": meta.get("updated", ""), "source": meta.get("source", "")})
    return sorted(out, key=lambda s: (-s["uses"], s["name"].lower()))


def find(name: str) -> str | None:
    """Best match by slug, then by name/description words."""
    sl = slugify(name)
    if (DIR / f"{sl}.md").exists():
        return sl
    words = set(re.findall(r"[a-z0-9]+", name.lower()))
    best, score = None, 0
    for s in list_skills():
        hay = set(re.findall(r"[a-z0-9]+", f"{s['name']} {s['description']} {s['id']}".lower()))
        sc = len(words & hay)
        if sc > score:
            best, score = s["id"], sc
    return best if score >= max(1, len(words) // 2) else None


def get(name: str, count_use: bool = True) -> dict | None:
    sid = find(name)
    if not sid:
        return None
    p = DIR / f"{sid}.md"
    with _lock:
        meta, body = _parse(p.read_text("utf-8"))
        if count_use:
            meta["uses"] = int(meta.get("uses", 0) or 0) + 1
            meta["last_used"] = dt.datetime.now().isoformat(timespec="seconds")
            p.write_text(_render(meta, body), "utf-8")
    return {"id": sid, **meta, "body": body}


def save(name: str, description: str, steps: str, notes: str = "", source: str = "agent") -> dict:
    """Create a skill, or improve an existing one with the same name (version + 1, old notes kept)."""
    sid = find(name) if (DIR / f"{slugify(name)}.md").exists() else slugify(name)
    p = DIR / f"{sid}.md"
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _lock:
        if p.exists():
            meta, old = _parse(p.read_text("utf-8"))
            version = int(meta.get("version", 1) or 1) + 1
            old_notes = re.search(r"## Notes\s*\n(.*)$", old, re.S)
            kept = old_notes.group(1).strip() if old_notes else ""
            meta.update({"name": name, "description": description or meta.get("description", ""),
                         "version": version, "updated": now})
        else:
            meta = {"name": name, "description": description, "version": 1, "uses": 0, "updated": now,
                    "created": now, "source": source}
            kept = ""
        steps = steps.strip()
        if not re.match(r"^#+\s", steps):
            steps = "## Steps\n" + steps
        note_lines = [kept] if kept else []
        if notes.strip():
            note_lines.append(f"- v{meta['version']} ({now[:10]}): {notes.strip()}")
        body = steps + ("\n\n## Notes\n" + "\n".join(note_lines) if note_lines else "")
        p.write_text(_render(meta, body), "utf-8")
    return {"id": sid, **meta}


def delete(sid: str) -> bool:
    p = DIR / f"{slugify(sid)}.md"
    if p.exists():
        p.unlink()
        return True
    return False


def prompt_index(limit: int = 40) -> str:
    rows = list_skills()[:limit]
    return "\n".join(f"- {s['name']}: {s['description']} (v{s['version']}, used {s['uses']}x)" for s in rows)


# --------------------------------------------------------------------------- teach mode
class Recorder:
    """Watches the user demonstrate a workflow: active window changes, mouse clicks, typed text
    (masked in password/sign-in windows), periodic screenshots and anything the user narrates."""

    def __init__(self, screenshot_fn, active_window_fn, max_shots: int = 14):
        self.shot = screenshot_fn
        self.active = active_window_fn
        self.max_shots = max_shots
        self.events: list[tuple[float, str]] = []
        self.shots: list[tuple[float, str, str]] = []   # (t, caption, png b64)
        self.running = False
        self.t0 = 0.0
        self._typed = ""
        self._last_title = ""

    def start(self):
        self.events, self.shots, self._typed = [], [], ""
        self.running = True
        self.t0 = time.time()
        threading.Thread(target=self._watch, daemon=True).start()
        self._start_input_hooks()

    def note(self, text: str):
        self._flush_typed()
        self.events.append((time.time() - self.t0, f'User said: "{text}"'))

    def _start_input_hooks(self):
        try:
            from pynput import keyboard, mouse
        except Exception:  # noqa: BLE001 - optional dependency
            return

        def on_click(x, y, button, pressed):
            if pressed and self.running:
                self._flush_typed()
                self.events.append((time.time() - self.t0, f"Clicked {button.name} at ({x}, {y})"))

        def on_press(key):
            if not self.running:
                return
            title = (self._last_title or "").lower()
            secret = re.search(r"password|sign.?in|log.?in|credential|bank|otp|2fa", title)
            if hasattr(key, "char") and key.char:
                self._typed += "•" if secret else key.char
            elif key == keyboard.Key.space:
                self._typed += " "
            elif key == keyboard.Key.backspace:
                self._typed = self._typed[:-1]
            else:
                self._flush_typed()
                self.events.append((time.time() - self.t0, f"Pressed {str(key).replace('Key.', '')}"))

        self._mouse = mouse.Listener(on_click=on_click)
        self._kb = keyboard.Listener(on_press=on_press)
        self._mouse.start()
        self._kb.start()

    def _flush_typed(self):
        if self._typed.strip():
            self.events.append((time.time() - self.t0, f'Typed "{self._typed.strip()[:200]}"'))
        self._typed = ""

    def _watch(self):
        last_title, last_shot = None, 0.0
        while self.running:
            try:
                title = self.active() or ""
            except Exception:  # noqa: BLE001
                title = ""
            self._last_title = title
            now = time.time()
            changed = title != last_title
            if changed:
                self._flush_typed()
                self.events.append((now - self.t0, f"Active window: {title or '(unknown)'}"))
                last_title = title
            if (changed or now - last_shot > 8) and len(self.shots) < self.max_shots:
                try:
                    out = self.shot()
                    if out.image_b64:
                        self.shots.append((now - self.t0, title, out.image_b64))
                        last_shot = now
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1.0)

    def stop(self) -> dict:
        self.running = False
        self._flush_typed()
        for l in (getattr(self, "_mouse", None), getattr(self, "_kb", None)):
            try:
                l and l.stop()
            except Exception:  # noqa: BLE001
                pass
        timeline = "\n".join(f"[{t:5.1f}s] {e}" for t, e in self.events)
        return {"timeline": timeline, "shots": self.shots, "seconds": time.time() - self.t0}


TEACH_SYSTEM = """You turn a recorded demonstration of a computer workflow into a reusable skill for an AI assistant
that controls the computer (shell, mouse/keyboard, browser automation, files). Write steps the assistant can
follow next time, generalising specifics (e.g. "this month's" instead of a date) and preferring robust methods
(keyboard shortcuts, URLs, shell commands, browser selectors by visible text) over raw pixel positions.
Mention where inputs vary and ask-the-user points. Reply with JSON only:
{"name": "short title", "description": "one sentence: what it does and when to use it",
 "steps": "## Steps\\n1. ...\\n2. ...", "notes": "gotchas observed"}"""
