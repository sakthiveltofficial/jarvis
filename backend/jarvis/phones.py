"""Phone companion hub: pair Android phones running the Jarvis app and use them from the agent.

The phone connects to this PC (not the other way round), so it works from the LAN or Tailscale without opening
ports on the phone. The hub listens on 0.0.0.0:<port> (default 8767) only while phone access is turned on.

Pairing: the PC shows a QR code `jarvis://pair?v=1&name=..&hosts=a,b&port=..&pid=..&k=..` with a one-time 32-byte
secret that expires after 10 minutes. The phone proves it knows the secret; the PC answers with a fresh long-term
device key, sealed under that secret. Later connections authenticate with the device key.

Framing: every message after the plaintext "hello" is AES-256-GCM sealed: {"n": b64 nonce, "c": b64 ciphertext+tag}.
The additional data is a purpose prefix ("auth|", "welcome|", "c2s|", "s2c|") plus the connection's random
challenge, so frames can't be replayed on another connection or reflected; sequence numbers stop replays within one.
The same scheme is implemented in the app (modules/jarvis-bridge/.../Crypto.kt in jarvis-mobile).
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import json
import os
import platform
import secrets
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from . import character, config, nodes, tools

DEFAULT_PORT = 8767
PAIR_TTL = 600
MAX_FILE = 25 * 1024 * 1024

# Desktop UI events forwarded to phones (the rest are desktop-only or too chatty).
FORWARD = {"state", "user", "assistant", "info", "error", "tool_start", "tool_end", "confirm", "confirm_done",
           "memory_search", "bg_update", "ai_mood", "mood"}     # moods colour the AR orb on the phone


# --------------------------------------------------------------------------- crypto
def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def unb64(s: str) -> bytes:
    return base64.b64decode(s)


def seal(key: bytes, obj: dict, aad: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(obj, separators=(",", ":")).encode(), aad)
    return {"n": b64(nonce), "c": b64(ct)}


def unseal(key: bytes, frame: dict, aad: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return json.loads(AESGCM(key).decrypt(unb64(frame["n"]), unb64(frame["c"]), aad))


def pair_uri(name: str, hosts: list[str], port: int, pid: str, secret: bytes) -> str:
    from urllib.parse import quote
    k = base64.urlsafe_b64encode(secret).decode().rstrip("=")
    return (f"jarvis://pair?v=1&name={quote(name)}&hosts={quote(','.join(hosts), safe=',:[]')}"
            f"&port={port}&pid={pid}&k={k}")


def qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):] if "<svg" in svg else svg


# --------------------------------------------------------------------------- connection
class PhoneConn:
    def __init__(self, ws: WebSocket, device: dict, key: bytes, challenge: bytes):
        self.ws = ws
        self.device = device
        self.key = key
        self.challenge = challenge
        self.send_seq = 0
        self.recv_seq = 0
        self.pending: dict[str, asyncio.Future] = {}
        self.caps: dict = {}
        self.lock = asyncio.Lock()
        self.connected_at = time.time()
        self.host = ws.client.host if ws.client else ""

    @property
    def id(self) -> str:
        return self.device["id"]

    async def send(self, obj: dict):
        async with self.lock:
            self.send_seq += 1
            obj = dict(obj, seq=self.send_seq)
            await self.ws.send_text(json.dumps(seal(self.key, obj, b"s2c|" + self.challenge)))

    def open(self, frame: dict) -> dict:
        m = unseal(self.key, frame, b"c2s|" + self.challenge)
        seq = int(m.get("seq") or 0)
        if seq <= self.recv_seq:
            raise ValueError("replayed frame")
        self.recv_seq = seq
        return m


# --------------------------------------------------------------------------- hub
class PhoneHub:
    def __init__(self, session):
        self.s = session
        self.conns: dict[str, PhoneConn] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server = None
        self.thread: threading.Thread | None = None
        self.port: int | None = None
        self.pairing: dict | None = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="phone-audio")

    # settings ----------------------------------------------------------------
    @property
    def cfg(self) -> dict:
        return self.s.settings.setdefault("phones", {})

    def devices(self) -> list[dict]:
        return list(self.cfg.get("devices") or [])

    def _save_devices(self, devs: list[dict]):
        self.cfg["devices"] = devs
        config.save({"phones": {"devices": devs}})

    def find(self, name_or_id: str = "") -> dict | None:
        devs = self.devices()
        q = (name_or_id or "").strip().lower()
        if not q:
            online = [d for d in devs if d["id"] in self.conns]
            return (online or devs or [None])[0]
        for d in devs:
            if d["id"].lower() == q or d.get("name", "").lower() == q:
                return d
        for d in devs:
            if q in d.get("name", "").lower() or q in d.get("model", "").lower():
                return d
        return None

    # server --------------------------------------------------------------------
    def running(self) -> bool:
        return self.server is not None

    def start(self):
        if self.server:
            return
        self.port = int(self.cfg.get("port") or DEFAULT_PORT)
        import uvicorn
        config_ = uvicorn.Config(self._app(), host="0.0.0.0", port=self.port, log_level="warning",
                                 ws_max_size=48 * 1024 * 1024, ws_ping_interval=20, ws_ping_timeout=30)
        self.server = uvicorn.Server(config_)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="phone-hub")
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.should_exit = True
        self.server = None
        self.conns.clear()

    def addresses(self) -> list[str]:
        extra = [h.strip() for h in (self.cfg.get("extra_hosts") or []) if h.strip()]
        return extra + nodes.lan_addresses()

    def pc_name(self) -> str:
        return self.cfg.get("name") or self.s.settings.get("node", {}).get("name") or platform.node() or "My PC"

    def new_pairing(self) -> dict:
        self.start()
        secret = os.urandom(32)
        pid = secrets.token_urlsafe(9)
        self.pairing = {"id": pid, "secret": secret, "expires": time.time() + PAIR_TTL}
        uri = pair_uri(self.pc_name(), self.addresses(), self.port or DEFAULT_PORT, pid, secret)
        try:
            svg = qr_svg(uri)
        except Exception as e:  # noqa: BLE001
            svg = ""
            print(f"[jarvis] QR code unavailable: {e}")
        return {"uri": uri, "svg": svg, "expires_in": PAIR_TTL, "addresses": self.addresses(), "port": self.port}

    def status(self) -> list[dict]:
        out = []
        for d in self.devices():
            c = self.conns.get(d["id"])
            out.append({"id": d["id"], "name": d.get("name"), "model": d.get("model"), "android": d.get("android"),
                        "paired_at": d.get("paired_at"), "last_seen": d.get("last_seen"), "online": c is not None,
                        "host": c.host if c else None, "caps": c.caps if c else {}})
        return out

    def remove(self, device_id: str) -> bool:
        devs = self.devices()
        keep = [d for d in devs if d["id"] != device_id]
        self._save_devices(keep)
        c = self.conns.pop(device_id, None)
        if c and self.loop:
            async def bye():
                try:
                    await c.send({"t": "revoked"})
                    await c.ws.close(code=4401)
                except Exception:
                    pass
            asyncio.run_coroutine_threadsafe(bye(), self.loop)
        return len(keep) != len(devs)

    def online(self, device_id: str | None) -> bool:
        return bool(device_id) and device_id in self.conns

    # websocket -----------------------------------------------------------------
    def _app(self) -> FastAPI:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(_app):
            self.loop = asyncio.get_running_loop()
            yield

        app = FastAPI(lifespan=lifespan)

        @app.get("/phone/ping")
        def ping():
            return {"jarvis": True, "v": 1}

        @app.websocket("/phone")
        async def phone_ws(ws: WebSocket):
            await ws.accept()
            conn = None
            try:
                conn = await self._handshake(ws)
                if conn is None:
                    return
                while True:
                    raw = await ws.receive_text()
                    try:
                        msg = conn.open(json.loads(raw))
                    except Exception:
                        await ws.close(code=4400)
                        return
                    await self._on_message(conn, msg)
            except WebSocketDisconnect:
                pass
            except Exception as e:  # noqa: BLE001
                print(f"[jarvis] phone connection error: {type(e).__name__}: {e}")
            finally:
                if conn and self.conns.get(conn.id) is conn:
                    self.conns.pop(conn.id, None)
                    for f in conn.pending.values():
                        if not f.done():
                            f.set_exception(ConnectionError("The phone disconnected."))
                    self._touch(conn.id)
                    self.s.emit({"type": "phones"})

        return app

    async def _deny(self, ws: WebSocket, reason: str, code: int = 4401):
        try:
            await ws.send_text(json.dumps({"t": "denied", "reason": reason}))
            await ws.close(code=code)
        except Exception:
            pass

    async def _handshake(self, ws: WebSocket) -> PhoneConn | None:
        challenge = os.urandom(16)
        await ws.send_text(json.dumps({"t": "hello", "v": 1, "name": self.pc_name(), "challenge": b64(challenge)}))
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=20))
        if first.get("t") != "auth":
            await ws.close(code=4400)
            return None
        mode, dev_id = first.get("mode"), str(first.get("device") or "")[:80]
        if not dev_id:
            await ws.close(code=4400)
            return None
        welcome = {"name": self.pc_name(), "hosts": self.addresses(), "port": self.port,
                   "user": self.s.settings.get("user_name") or "",
                   "assistant": character.name(self.s.settings), "state": self.s.state}
        if mode == "pair":
            p = self.pairing
            if not p or p["id"] != first.get("pid") or time.time() > p["expires"]:
                await self._deny(ws, "This pairing code has expired or was already used. Make a new one on the PC.", 4403)
                return None
            try:
                body = unseal(p["secret"], first["box"], b"auth|" + challenge)
            except Exception:
                await asyncio.sleep(1)
                await self._deny(ws, "The pairing code didn't match.", 4403)
                return None
            if body.get("challenge") != b64(challenge):
                await self._deny(ws, "Pairing failed.", 4403)
                return None
            self.pairing = None                      # one use only
            device_key = os.urandom(32)
            dev = {"id": dev_id, "name": str(body.get("name") or "Phone")[:60], "model": str(body.get("model") or "")[:80],
                   "android": str(body.get("android") or ""), "key": b64(device_key), "paired_at": time.time(),
                   "last_seen": time.time()}
            self._save_devices([d for d in self.devices() if d["id"] != dev_id] + [dev])
            await ws.send_text(json.dumps({"t": "welcome", "box": seal(p["secret"], dict(welcome, device_key=b64(device_key)),
                                                                     b"welcome|" + challenge)}))
            key = device_key
            self.s.emit({"type": "info", "text": f"📱 Paired {dev['name']}."})
            self.s.emit({"type": "phone_paired", "id": dev_id, "name": dev["name"]})
        elif mode == "device":
            dev = next((d for d in self.devices() if d["id"] == dev_id), None)
            if not dev:
                await self._deny(ws, "This PC doesn't know this phone any more. Pair it again.")
                return None
            key = unb64(dev["key"])
            try:
                body = unseal(key, first["box"], b"auth|" + challenge)
                if body.get("challenge") != b64(challenge):
                    raise ValueError("challenge")
            except Exception:
                await asyncio.sleep(1)
                await ws.close(code=4401)
                return None
            await ws.send_text(json.dumps({"t": "welcome", "box": seal(key, welcome, b"welcome|" + challenge)}))
            for k in ("name", "model", "android"):
                if body.get(k):
                    dev[k] = str(body[k])[:80]
        else:
            await ws.close(code=4400)
            return None
        old = self.conns.get(dev_id)
        conn = PhoneConn(ws, dev, key, challenge)
        self.conns[dev_id] = conn
        if old:
            try:
                await old.ws.close(code=1000)
            except Exception:
                pass
        self._touch(dev_id)
        self.s.emit({"type": "phones"})
        return conn

    def _touch(self, dev_id: str):
        devs = self.devices()
        for d in devs:
            if d["id"] == dev_id:
                d["last_seen"] = time.time()
        self._save_devices(devs)

    async def _on_message(self, conn: PhoneConn, m: dict):
        t = m.get("t")
        s = self.s
        if t == "result":
            f = conn.pending.pop(str(m.get("id")), None)
            if f and not f.done():
                f.set_result(m)
        elif t == "user_text":
            text = str(m.get("text") or "").strip()
            if text:
                origin = conn.id if m.get("speak", True) else "-"
                asyncio.run_coroutine_threadsafe(s.handle_user(text, origin=origin), s.loop)
        elif t == "audio":
            wav = unb64(m.get("wav") or "")
            self.pool.submit(s.phone_utterance, wav, conn.id if m.get("speak", True) else "-")
        elif t == "listening":
            s.stop_speaking()
        elif t == "confirm":
            s.resolve_confirm(str(m.get("id")), bool(m.get("approve")))
        elif t == "stop":
            async def stop():
                await s.interrupt()
                s.set_state(s._idle_state())
            asyncio.run_coroutine_threadsafe(stop(), s.loop)
        elif t == "stop_speaking":
            s.stop_speaking()
        elif t == "caps":
            conn.caps = m.get("caps") or {}
            s.emit({"type": "phones"})
        elif t == "notification":
            s.on_phone_notification(conn.device.get("name", "phone"), m)
        elif t == "unpair":
            self.remove(conn.id)
        elif t == "req":
            ar = getattr(s, "ar", None)
            if ar is None:
                await conn.send({"t": "resp", "id": m.get("id"), "ok": False, "error": "AR isn't available on this PC."})
            else:
                ar.pool.submit(self._serve, conn, ar, m)

    def _serve(self, conn: PhoneConn, ar, m: dict):
        """A request from the phone (AR camera features): runs on a worker thread, answers with "resp"."""
        try:
            data = ar.handle(conn.device, str(m.get("op") or ""), m.get("args") or {})
            out = {"t": "resp", "id": m.get("id"), "ok": True, "data": data}
        except Exception as e:  # noqa: BLE001
            out = {"t": "resp", "id": m.get("id"), "ok": False, "error": str(e) or type(e).__name__}
        try:
            self._submit(self._safe_send(conn, out))
        except Exception:  # noqa: BLE001
            pass

    # outbound ------------------------------------------------------------------
    def _submit(self, coro):
        if not self.loop:
            raise ConnectionError("Phone hub isn't running.")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def forward(self, ev: dict):
        """Mirror a desktop UI event to every connected phone (thread-safe, fire and forget)."""
        if not self.conns or not self.loop or ev.get("type") not in FORWARD:
            return
        ev = dict(ev)
        if ev["type"] == "tool_end":
            ev.pop("image", None)
            ev["text"] = (ev.get("text") or "")[:300]
        elif ev["type"] == "confirm":
            ev.pop("args", None)
        elif ev["type"] == "memory_search":
            ev = {"type": "memory_search", "nodes": [n.get("id") for n in (ev.get("nodes") or [])][:50]}
        for c in list(self.conns.values()):
            self._submit(self._safe_send(c, {"t": "event", "ev": ev}))

    async def _safe_send(self, c: PhoneConn, obj: dict):
        try:
            await c.send(obj)
        except Exception:
            pass

    def send_audio(self, device_id: str, wav: bytes, text: str = ""):
        c = self.conns.get(device_id)
        if c:
            self._submit(self._safe_send(c, {"t": "tts", "wav": b64(wav), "text": text[:300]}))

    def stop_audio(self):
        for c in list(self.conns.values()):
            self._submit(self._safe_send(c, {"t": "stop_audio"}))

    async def _request(self, c: PhoneConn, action: str, args: dict, timeout: float) -> dict:
        rid = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        c.pending[rid] = fut
        try:
            await c.send({"t": "call", "id": rid, "action": action, "args": args})
            return await asyncio.wait_for(fut, timeout)
        finally:
            c.pending.pop(rid, None)

    def raw_call(self, device: dict, action: str, args: dict, timeout: float = 90) -> dict:
        c = self.conns.get(device["id"])
        if not c:
            raise ConnectionError(f"{device.get('name')} is offline. Open the Jarvis app on the phone (it reconnects "
                                  "on its own when it's on the same network or Tailscale).")
        return self._submit(self._request(c, action, args, timeout)).result(timeout + 5)

    # the agent's `phone` tool --------------------------------------------------
    def tool(self, a: dict) -> tools.ToolOutput:
        a = dict(a or {})
        dev = self.find(a.pop("device", "") or "")
        if not dev:
            names = ", ".join(d.get("name", "?") for d in self.devices()) or "none"
            return tools.ToolOutput(f"No paired phone matches. Paired phones: {names}.", is_error=True)
        action = a.pop("action", "")
        name = dev.get("name", "phone")
        try:
            if action == "files" and a.get("op") in ("pull", "push"):
                return self._transfer(dev, a)
            timeout = 60 + float(a.get("timeout") or 0) if action == "shell" else 90
            r = self.raw_call(dev, action, a, timeout=timeout)
        except concurrent.futures.TimeoutError:
            return tools.ToolOutput(f"[{name}] The phone didn't answer in time.", is_error=True)
        except Exception as e:  # noqa: BLE001
            return tools.ToolOutput(f"[{name}] {e}", is_error=True)
        img = r.get("image_b64")
        if img and r.get("image_type", "image/jpeg") != "image/png":
            img = _to_png(img)
        return tools.ToolOutput(f"[{name}] {r.get('text', '')}", img, bool(r.get("is_error")))

    def _transfer(self, dev: dict, a: dict) -> tools.ToolOutput:
        name = dev.get("name", "phone")
        if a["op"] == "pull":
            r = self.raw_call(dev, "files", {"op": "read_b64", "path": a.get("path", "")}, timeout=180)
            if r.get("is_error"):
                return tools.ToolOutput(f"[{name}] {r.get('text')}", is_error=True)
            data = unb64((r.get("data") or {}).get("b64", ""))
            fname = (r.get("data") or {}).get("name") or Path(a.get("path", "file")).name
            dest = Path(os.path.expanduser(a.get("save_to") or str(Path.home() / "Downloads" / fname)))
            if dest.is_dir():
                dest = dest / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return tools.ToolOutput(f"Copied {fname} from {name} to {dest} ({len(data) // 1024} KB).")
        src = Path(os.path.expanduser(a.get("from_path") or ""))
        if not src.is_file():
            return tools.ToolOutput(f"No file at {src} on this PC.", is_error=True)
        if src.stat().st_size > MAX_FILE:
            return tools.ToolOutput("That file is over 25 MB; too big to send this way.", is_error=True)
        dest = a.get("path") or f"Download/{src.name}"
        if dest.endswith("/"):
            dest += src.name
        r = self.raw_call(dev, "files", {"op": "write_b64", "path": dest, "data_b64": b64(src.read_bytes())}, timeout=180)
        return tools.ToolOutput(f"[{name}] {r.get('text')}", is_error=bool(r.get("is_error")))

    def prompt_lines(self) -> str:
        out = []
        for d in self.status():
            caps = d.get("caps") or {}
            on = [k for k in ("accessibility", "notifications", "sms", "calls", "contacts", "files", "location", "shizuku")
                  if caps.get(k)]
            state = "online" if d["online"] else "offline"
            out.append(f"- {d['name']} ({d.get('model') or 'Android'}; {state}"
                       + (f"; allowed: {', '.join(on)}" if d["online"] else "") + ")")
        return "\n".join(out)


def _to_png(b64jpeg: str) -> str:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(unb64(b64jpeg)))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "PNG", optimize=True)
        return b64(buf.getvalue())
    except Exception:  # noqa: BLE001
        return b64jpeg
