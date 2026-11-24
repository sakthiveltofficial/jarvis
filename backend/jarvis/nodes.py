"""Multi-PC control: pair other computers running Jarvis and drive them from this one.

On each extra PC, turn on "Let other PCs control this one" (settings["node"]). That starts a second,
token-protected API on 0.0.0.0:<port> (default 8766) that runs Jarvis's local tools there. On the main PC,
add the PC under Connections → Other PCs with its address and token; the agent then gets a `remote_pc`
tool and sees the PCs in its instructions.

Security: every request needs the 32-byte token (constant-time compare). The node only listens when you
enable it; use it on a trusted LAN or a private network like Tailscale, not the open internet.
Elevated (admin) commands are refused unless the node allows them.
"""
from __future__ import annotations

import concurrent.futures
import hmac
import platform
import secrets
import socket
import threading

import httpx
from fastapi import FastAPI, HTTPException, Request

from . import tools

DEFAULT_PORT = 8766


def new_token() -> str:
    return secrets.token_urlsafe(32)


def lan_addresses() -> list[str]:
    out = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        out.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in out if not a.startswith("127."))


# --------------------------------------------------------------------------- node server
class NodeServer:
    def __init__(self, settings: dict):
        self.settings = settings
        self.server = None
        self.thread = None
        self.port = None
        self.runtime = tools.ToolRuntime(settings)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="node-tools")

    def _app(self):
        app = FastAPI()

        def auth(request: Request):
            tok = self.settings.get("node", {}).get("token", "")
            got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not tok or not hmac.compare_digest(tok.encode(), got.encode()):
                raise HTTPException(401, "bad token")

        @app.get("/node/info")
        def info(request: Request):
            auth(request)
            return {"name": self.settings.get("node", {}).get("name") or platform.node(),
                    "os": f"{platform.system()} {platform.release()}", "tools": sorted(tools.NODE_TOOLS)}

        @app.post("/node/call")
        def call(body: dict, request: Request):
            auth(request)
            name, args = body.get("tool", ""), body.get("args") or {}
            if name not in tools.NODE_TOOLS:
                raise HTTPException(400, f"tool {name} not available on nodes")
            if name == "run_shell" and args.get("elevated") and not self.settings.get("node", {}).get("allow_elevated"):
                return {"text": "This PC doesn't allow admin commands from other PCs.", "is_error": True}
            out = self.pool.submit(self.runtime.call, name, args).result(timeout=600)
            return {"text": out.text, "image_b64": out.image_b64, "is_error": out.is_error}

        return app

    def start(self) -> str:
        import uvicorn
        cfg = self.settings.get("node", {})
        self.port = int(cfg.get("port") or DEFAULT_PORT)
        config = uvicorn.Config(self._app(), host="0.0.0.0", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="node-server")
        self.thread.start()
        return f"listening on port {self.port}"

    def stop(self):
        if self.server:
            self.server.should_exit = True
        self.server = None


# --------------------------------------------------------------------------- client
def _pc(settings: dict, name: str) -> dict:
    pcs = settings.get("nodes") or []
    for p in pcs:
        if p.get("name", "").lower() == (name or "").lower():
            return p
    if len(pcs) == 1 and not name:
        return pcs[0]
    names = ", ".join(p.get("name", "?") for p in pcs) or "none"
    raise ValueError(f"No paired PC called {name!r}. Paired PCs: {names}")


def _base(p: dict) -> str:
    url = p.get("url", "").strip().rstrip("/")
    if not url.startswith("http"):
        url = "http://" + url
    if url.count(":") < 2:
        url += f":{DEFAULT_PORT}"
    return url


def call(settings: dict, pc: str, tool: str, args: dict) -> tools.ToolOutput:
    p = _pc(settings, pc)
    r = httpx.post(_base(p) + "/node/call", json={"tool": tool, "args": args or {}},
                   headers={"Authorization": f"Bearer {p.get('token', '')}"}, timeout=620)
    if r.status_code == 401:
        return tools.ToolOutput(f"{p['name']} rejected the token. Re-pair it in Connections.", is_error=True)
    r.raise_for_status()
    d = r.json()
    return tools.ToolOutput(f"[{p['name']}] {d.get('text', '')}", d.get("image_b64"), bool(d.get("is_error")))


def ping(p: dict) -> dict:
    try:
        r = httpx.get(_base(p) + "/node/info", headers={"Authorization": f"Bearer {p.get('token', '')}"}, timeout=5)
        if r.status_code == 401:
            return {"name": p.get("name"), "ok": False, "error": "bad token"}
        r.raise_for_status()
        info = r.json()
        return {**info, "name": p.get("name"), "host": info.get("name"), "ok": True}
    except Exception as e:  # noqa: BLE001
        return {"name": p.get("name"), "ok": False, "error": str(e)[:200]}
