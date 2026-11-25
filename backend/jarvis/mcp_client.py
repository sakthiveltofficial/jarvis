"""A small, dependency-free Model Context Protocol (MCP) client.

Connects to MCP servers configured in settings["mcp"]["servers"] and exposes their tools to the agent
as `mcp_<server>_<tool>`. Two transports:

  * stdio:  {"name": "notion", "command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"],
             "env": {"NOTION_TOKEN": "..."}}
  * HTTP:   {"name": "github", "url": "https://api.githubcopilot.com/mcp/",
             "headers": {"Authorization": "Bearer <token>"}}   (Streamable HTTP, JSON or SSE replies)

OAuth sign-in flows aren't implemented; use servers that take a token or run locally.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import threading
import time

import httpx

from . import config

PROTOCOL = "2025-06-18"
CLIENT_INFO = {"name": "jarvis", "version": "0.5.0"}
READ_VERBS = re.compile(r"^(get|list|search|read|fetch|query|find|describe|show|view|retrieve|lookup|check)", re.I)
LOG_DIR = config.HOME / "logs"


class MCPError(Exception):
    pass


def slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", s).strip("_").lower() or "server"


# --------------------------------------------------------------------------- transports
class StdioConn:
    def __init__(self, cfg: dict):
        cmd = cfg["command"]
        args = cfg.get("args") or []
        if isinstance(args, str):
            import shlex
            args = shlex.split(args, posix=os.name != "nt")
        exe = shutil.which(cmd) or cmd
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (cfg.get("env") or {}).items()})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.errlog = open(LOG_DIR / f"mcp-{slug(cfg.get('name', 'server'))}.log", "ab")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen([exe, *map(str, args)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.errlog, env=env, cwd=cfg.get("cwd") or None, **kw)
        self.pending: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.on_notify = None
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                self._dispatch(m)
        for slot in list(self.pending.values()):
            slot["error"] = "server exited"
            slot["ev"].set()

    def _dispatch(self, m: dict):
        if "method" in m and "id" in m:      # server -> client request
            result = {"roots": []} if m["method"] == "roots/list" else {}
            if m["method"] not in ("ping", "roots/list"):
                self._write({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported"}})
            else:
                self._write({"jsonrpc": "2.0", "id": m["id"], "result": result})
            return
        if "method" in m:                    # notification
            if self.on_notify:
                self.on_notify(m)
            return
        slot = self.pending.get(m.get("id"))
        if slot:
            slot["msg"] = m
            slot["ev"].set()

    def _write(self, obj: dict):
        data = (json.dumps(obj) + "\n").encode()
        with self.lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def request(self, rid: int, method: str, params: dict, timeout: float) -> dict:
        slot = {"ev": threading.Event(), "msg": None, "error": None}
        self.pending[rid] = slot
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if not slot["ev"].wait(timeout):
                raise MCPError(f"{method} timed out after {timeout:.0f}s")
            if slot["error"]:
                raise MCPError(slot["error"])
            return slot["msg"]
        finally:
            self.pending.pop(rid, None)

    def notify(self, method: str, params: dict | None = None):
        self._write({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self):
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.proc.terminate()
            self.proc.wait(3)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self.errlog.close()


class HttpConn:
    def __init__(self, cfg: dict):
        self.url = cfg["url"]
        self.headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.session_id = None
        self.client = httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=True)
        self.on_notify = None

    def _post(self, body: dict, timeout: float, want_id=None) -> dict | None:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL, **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        with self.client.stream("POST", self.url, json=body, headers=h, timeout=timeout) as r:
            if r.headers.get("mcp-session-id"):
                self.session_id = r.headers["mcp-session-id"]
            if r.status_code == 202 or want_id is None:
                return None
            if r.status_code >= 400:
                r.read()
                raise MCPError(f"HTTP {r.status_code}: {r.text[:300]}")
            ctype = r.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                data: list[str] = []
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        msg = json.loads("\n".join(data))
                        data = []
                        if isinstance(msg, dict) and msg.get("id") == want_id and "method" not in msg:
                            return msg
                        if isinstance(msg, dict) and "method" in msg and "id" not in msg and self.on_notify:
                            self.on_notify(msg)
                raise MCPError("stream ended without a reply")
            r.read()
            msg = r.json()
            if isinstance(msg, list):
                msg = next((m for m in msg if m.get("id") == want_id), {})
            return msg

    def request(self, rid: int, method: str, params: dict, timeout: float) -> dict:
        return self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, timeout, rid)

    def notify(self, method: str, params: dict | None = None):
        try:
            self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, 30)
        except Exception:  # noqa: BLE001
            pass

    def alive(self) -> bool:
        return True

    def close(self):
        try:
            if self.session_id:
                self.client.delete(self.url, headers={"Mcp-Session-Id": self.session_id, **self.headers}, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self.client.close()


# --------------------------------------------------------------------------- server
class MCPServer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg.get("name") or "server"
        self.key = slug(self.name)
        self.ids = itertools.count(1)
        self.conn = None
        self.tools: list[dict] = []
        self.state = "connecting"
        self.error = ""
        self.info: dict = {}
        self.lock = threading.Lock()

    def _req(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        msg = self.conn.request(next(self.ids), method, params or {}, timeout)
        if msg.get("error"):
            e = msg["error"]
            raise MCPError(f"{e.get('message')} ({e.get('code')})")
        return msg.get("result") or {}

    def connect(self):
        try:
            self.conn = HttpConn(self.cfg) if self.cfg.get("url") else StdioConn(self.cfg)
            self.conn.on_notify = self._on_notify
            res = self._req("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                           "clientInfo": CLIENT_INFO}, timeout=90)
            self.info = res.get("serverInfo") or {}
            self.conn.notify("notifications/initialized")
            self.refresh_tools()
            self.state, self.error = "ready", ""
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            self.close()

    def refresh_tools(self):
        tools, cursor = [], None
        for _ in range(20):
            res = self._req("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools") or []
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    def _on_notify(self, m: dict):
        if m.get("method") == "notifications/tools/list_changed":
            threading.Thread(target=self._safe_refresh, daemon=True).start()

    def _safe_refresh(self):
        try:
            self.refresh_tools()
        except Exception:  # noqa: BLE001
            pass

    def call(self, tool: str, args: dict, timeout: float = 180) -> dict:
        with self.lock:
            if self.state != "ready" or not self.conn or not self.conn.alive():
                self.close()
                self.connect()
                if self.state != "ready":
                    raise MCPError(f"{self.name} is not connected: {self.error}")
            return self._req("tools/call", {"name": tool, "arguments": args or {}}, timeout)

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self.conn = None


def _cfg_hash(cfg: dict) -> str:
    return hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- manager
class MCPManager:
    def __init__(self):
        self.servers: dict[str, MCPServer] = {}
        self.hashes: dict[str, str] = {}
        self.map: dict[str, tuple[str, dict]] = {}     # exposed tool name -> (server key, tool def)
        self.lock = threading.Lock()

    def reload(self, settings: dict):
        wanted = {}
        for cfg in (settings.get("mcp", {}).get("servers") or []):
            if not cfg.get("enabled", True) or not (cfg.get("command") or cfg.get("url")):
                continue
            wanted[slug(cfg.get("name") or cfg.get("command") or cfg.get("url"))] = cfg
        with self.lock:
            for key in list(self.servers):
                if key not in wanted or self.hashes.get(key) != _cfg_hash(wanted[key]):
                    self.servers.pop(key).close()
                    self.hashes.pop(key, None)
            for key, cfg in wanted.items():
                if key not in self.servers:
                    srv = MCPServer(dict(cfg, name=cfg.get("name") or key))
                    self.servers[key] = srv
                    self.hashes[key] = _cfg_hash(cfg)
                    threading.Thread(target=srv.connect, daemon=True, name=f"mcp-{key}").start()

    def status(self) -> list[dict]:
        return [{"name": s.name, "state": s.state, "error": s.error, "tools": [t["name"] for t in s.tools],
                 "server": s.info.get("name", "")} for s in self.servers.values()]

    def specs(self):
        from .tools import ToolSpec
        out, self.map = [], {}
        for key, s in list(self.servers.items()):
            if s.state != "ready":
                continue
            for t in s.tools:
                name = f"mcp_{key}_{slug(t['name'])}"[:64]
                self.map[name] = (key, t)
                schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                if schema.get("type") != "object":
                    schema = {"type": "object", "properties": {}}
                desc = f"[{s.name}] {(t.get('description') or t.get('title') or t['name']).strip()}"[:1000]
                out.append(ToolSpec(name, desc, {}, raw_schema=schema))
        return out

    def is_mcp(self, name: str) -> bool:
        return name in self.map

    def risk(self, name: str, mode: str) -> str | None:
        key, t = self.map.get(name, (None, {}))
        srv = self.servers.get(key)
        if mode == "never" or srv is None:
            return None
        ann = t.get("annotations") or {}
        read_only = ann.get("readOnlyHint")
        if read_only is None:
            read_only = bool(READ_VERBS.match(t.get("name", "")))
        if read_only:
            return None
        if srv.cfg.get("trusted"):
            return f"Uses {srv.name}: {t.get('name')}" if mode == "always" else None
        if ann.get("destructiveHint") is False:
            return f"Uses {srv.name}: {t.get('name')}" if mode == "always" else None
        return f"Uses {srv.name} to {t.get('name', '').replace('_', ' ')} (can change things)"

    def call(self, name: str, args: dict):
        from .tools import ToolOutput
        key, t = self.map[name]
        res = self.servers[key].call(t["name"], args)
        texts, image = [], None
        for c in res.get("content") or []:
            if c.get("type") == "text":
                texts.append(c.get("text", ""))
            elif c.get("type") == "image" and not image and c.get("mimeType", "").startswith("image/png"):
                image = c.get("data")
            elif c.get("type") == "image":
                texts.append(f"[image {c.get('mimeType')}]")
            elif c.get("type") == "resource":
                r = c.get("resource") or {}
                texts.append(r.get("text") or f"[resource {r.get('uri')}]")
            elif c.get("type") == "resource_link":
                texts.append(f"[link {c.get('name')}: {c.get('uri')}]")
        if not texts and res.get("structuredContent") is not None:
            texts.append(json.dumps(res["structuredContent"])[:12000])
        return ToolOutput("\n".join(texts)[:16000] or "(no output)", image, bool(res.get("isError")))

    def close(self):
        with self.lock:
            for s in self.servers.values():
                s.close()
            self.servers.clear()


def wait_ready(mgr: MCPManager, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if all(s.state != "connecting" for s in mgr.servers.values()):
            return True
        time.sleep(0.2)
    return False
