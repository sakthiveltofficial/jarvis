"""HTTP + WebSocket API for the Jarvis UI. Run: python -m jarvis.server"""
from __future__ import annotations

import asyncio
import base64
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import audio, config, homeassistant, models, nodes, skills, voiceclone
from .agent import Session
from .speech import TTS, to_wav_bytes

HOST = "127.0.0.1"
PORT = int(os.environ.get("JARVIS_PORT", "8765"))
UI_DIR = Path(os.environ.get("JARVIS_UI_DIR", Path(__file__).resolve().parents[2] / "ui"))

# Only the desktop app and the locally served UI may talk to the agent. This stops random
# web pages in your normal browser from driving your computer through localhost.
ALLOWED_ORIGINS = {
    "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
    f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}",
    # `tauri dev` serves frontendDist from the CLI's built-in dev server on port 1430.
    "http://127.0.0.1:1430", "http://localhost:1430",
}

clients: set[WebSocket] = set()
session: Session | None = None


async def broadcast(ev: dict):
    if session is not None and getattr(session, "phones", None) is not None:
        try:
            session.phones.forward(ev)
        except Exception:  # noqa: BLE001
            pass
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(ev)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session
    session = Session(broadcast)
    yield
    session.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=list(ALLOWED_ORIGINS), allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "origin not allowed"}, status_code=403)
    return await call_next(request)


# --------------------------------------------------------------------------- REST
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return {"settings": config.public(session.settings), "presets": config.LLM_PRESETS}


@app.post("/api/settings")
async def post_settings(body: dict):
    session.apply_settings(body)
    return {"settings": config.public(session.settings)}


@app.get("/api/models")
def get_models():
    return models.catalog()


@app.post("/api/models/download")
def download(body: dict):
    return {"id": models.start_download(body["kind"], body["name"])}


@app.get("/api/audio/devices")
def devices():
    return audio.list_devices()


@app.post("/api/tts/test")
async def tts_test(body: dict):
    text = body.get("text") or "Good evening. All systems are online and I'm at your service."
    try:
        samples, sr = await asyncio.to_thread(TTS(session.settings).synthesize, text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e))
    if body.get("play", True):
        session.speak(text)
    return {"wav_b64": base64.b64encode(to_wav_bytes(samples, sr)).decode(), "seconds": len(samples) / sr}


# --------------------------------------------------------------------------- v0.7: custom voice, smart home
@app.get("/api/voice/clone")
async def clone_status():
    return await asyncio.to_thread(voiceclone.status, session.settings)


@app.post("/api/voice/sample")
async def clone_sample(body: dict):
    """{audio_b64, text}: upload a voice sample (any common audio format) and/or what is said in it."""
    try:
        data = base64.b64decode(body["audio_b64"]) if body.get("audio_b64") else None
        return await asyncio.to_thread(voiceclone.save_sample, data, body.get("text"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.post("/api/voice/record")
async def clone_record(body: dict):
    try:
        return await asyncio.to_thread(voiceclone.record, float(body.get("seconds", 15)),
                                       session.settings["audio"].get("input_device"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))


@app.get("/api/voice/sample")
def clone_sample_get():
    if not voiceclone.SAMPLE.exists():
        raise HTTPException(404, "no sample")
    return {"wav_b64": base64.b64encode(voiceclone.SAMPLE.read_bytes()).decode(), **voiceclone.info()}


@app.delete("/api/voice/sample")
def clone_sample_delete():
    voiceclone.delete_sample()
    return voiceclone.info()


@app.post("/api/homeassistant/test")
async def ha_test(body: dict):
    """Test the URL/token typed in Settings (a masked token means "the saved one")."""
    saved = session.settings.get("home_assistant") or {}
    tok = body.get("token")
    cfg = {**saved, **{k: v for k, v in body.items() if v is not None}}
    if not tok or str(tok).startswith("••••"):
        cfg["token"] = saved.get("token", "")
    ha = homeassistant.HomeAssistant({"home_assistant": cfg})
    return await asyncio.to_thread(ha.test)


@app.get("/api/homeassistant/devices")
async def ha_devices():
    if not homeassistant.configured(session.settings):
        return {"devices": [], "configured": False}
    try:
        return {"devices": await asyncio.to_thread(session.home.devices), "configured": True}
    except Exception as e:  # noqa: BLE001
        return {"devices": [], "configured": True, "error": str(e)}


@app.post("/api/reset")
def reset():
    session.reset()
    return {"ok": True}


def _mem():
    if session.memory is None:
        raise HTTPException(503, "graph memory unavailable")
    return session.memory


@app.get("/api/graph")
def graph(include_invalid: bool = False, files: bool = True):
    g = _mem().graph(include_invalid)
    if files and session.files is not None:
        part = session.files.graph_part()
        g["nodes"] = g["nodes"] + part["nodes"]
        g["edges"] = g["edges"] + part["edges"]
    return g


@app.get("/api/graph/search")
def graph_search(q: str):
    return _mem().search(q)


@app.get("/api/graph/node/{node_id}")
def graph_node(node_id: str):
    m = _mem()
    ent = next((e for e in m.entities() if e["id"] == node_id), None)
    if not ent and node_id.startswith("doc-") and session.files is not None:
        doc = session.files.document(node_id)
        if doc:
            return {"entity": {"id": node_id, "name": doc["name"], "type": "Document", "summary": doc["summary"] or "",
                               "updated": doc["indexed"] or "", "mentions": len(doc["entities"]), "path": doc["path"],
                               "chunks": doc["chunks"]}, "facts": []}
    if not ent:
        raise HTTPException(404, "not found")
    return {"entity": ent, "facts": m.facts(valid_only=False, around=[node_id])}


@app.delete("/api/graph/node/{node_id}")
def graph_forget(node_id: str):
    _mem().forget(node_id)
    return {"ok": True}


@app.post("/api/graph/clear")
def graph_clear():
    _mem().clear()
    return {"ok": True}


# --------------------------------------------------------------------------- v0.8: memory clean-up
@app.get("/api/memory/consolidation")
def consolidation_status(days: int = 14):
    if session.consolidator is None:
        raise HTTPException(503, "graph memory unavailable")
    return {**session.consolidator.status(), "digests": _mem().digests(limit=days)}


@app.post("/api/memory/consolidate")
def consolidate_now():
    if session.consolidator is None:
        raise HTTPException(503, "graph memory unavailable")
    return {"started": session.consolidator.run_async(), **session.consolidator.status()}


# --------------------------------------------------------------------------- v0.8: your files
def _files():
    if session.files is None:
        raise HTTPException(503, "file index unavailable")
    return session.files


@app.get("/api/files")
def files_status():
    return _files().status()


def _set_folders(folders: list[str]):
    session.apply_settings({"files": {"folders": folders}})


@app.post("/api/files/folders")
def files_add(body: dict):
    from .files import norm
    path = norm(str(body.get("path") or "").strip().strip('"'))
    if not os.path.isdir(path):
        raise HTTPException(400, f"not a folder: {path}")
    current = _files().folders()
    if any(path == f or path.startswith(f.rstrip("\\/") + os.sep) for f in current):
        raise HTTPException(400, "that folder is already indexed")
    # adding a parent replaces the subfolders it contains
    _set_folders([f for f in current if not f.startswith(path.rstrip("\\/") + os.sep)] + [path])
    return _files().status()


@app.delete("/api/files/folders")
def files_remove(path: str):
    from .files import norm
    _set_folders([f for f in _files().folders() if f != norm(path)])
    return _files().status()


@app.post("/api/files/reindex")
def files_reindex(body: dict | None = None):
    _files().reindex((body or {}).get("path") or None)
    return _files().status()


@app.get("/api/files/search")
def files_search(q: str, limit: int = 8):
    fi = _files()
    return {"query": q, "hits": [fi.public_hit(h) | {"score": h["score"]} for h in fi.search(q, limit=limit)]}


@app.post("/api/files/open")
def files_open(body: dict):
    from .files import open_path
    path = str(body.get("path") or "")
    if not _files().is_indexed(path):      # only files Jarvis indexed, never arbitrary paths
        raise HTTPException(404, "not an indexed file")
    open_path(path)
    return {"ok": True}


@app.post("/api/files/pick")
async def files_pick():
    from .files import pick_folder
    try:
        return {"path": await asyncio.to_thread(pick_folder)}
    except Exception as e:  # noqa: BLE001
        return {"path": "", "error": str(e)}


# --------------------------------------------------------------------------- v0.4: agenda, skills, apps, PCs
@app.get("/api/agenda")
def agenda():
    return session.agenda()


@app.post("/api/voice_mode")
async def voice_mode(body: dict):
    await asyncio.to_thread(session.set_voice_mode, body.get("mode", "off"))
    return {"mode": session.voice_mode, "wake_ready": session.mic.wake is not None}


@app.delete("/api/schedules/{job_id}")
def cancel_schedule(job_id: str):
    ok = session.scheduler.cancel(job_id)
    session.emit({"type": "agenda"})
    return {"ok": ok}


@app.post("/api/background/{bid}/cancel")
def cancel_bg(bid: str):
    session.cancel_background(bid)
    return {"ok": True}


@app.get("/api/skills")
def list_skills():
    return {"skills": skills.list_skills()}


@app.get("/api/skills/{sid}")
def get_skill(sid: str):
    sk = skills.get(sid, count_use=False)
    if not sk:
        raise HTTPException(404, "not found")
    return sk


@app.delete("/api/skills/{sid}")
def delete_skill(sid: str):
    return {"ok": skills.delete(sid)}


@app.post("/api/teach")
async def teach(body: dict):
    if body.get("action") == "start":
        msg = session.teach_start(body.get("name", ""))
        session.announce(msg, "teach")
    else:
        msg = await asyncio.to_thread(session.teach_stop)
    return {"message": msg, "on": session.teacher is not None}


@app.get("/api/mcp")
def mcp_status():
    return {"servers": session.mcp.status()}


@app.get("/api/node")
def node_info():
    cfg = session.settings.get("node", {})
    return {"enabled": bool(cfg.get("enabled")), "running": session.node_server is not None,
            "port": int(cfg.get("port") or nodes.DEFAULT_PORT), "token": cfg.get("token", ""),
            "addresses": nodes.lan_addresses(), "name": cfg.get("name") or __import__("platform").node()}


@app.get("/api/nodes/status")
async def nodes_status():
    return {"pcs": await asyncio.gather(*[asyncio.to_thread(nodes.ping, p) for p in session.settings.get("nodes") or []])}


# --------------------------------------------------------------------------- phones
@app.get("/api/phones")
def phones_status():
    cfg = session.settings.get("phones") or {}
    return {"enabled": bool(cfg.get("enabled")), "running": session.phones.running(),
            "port": int(cfg.get("port") or 8767), "addresses": session.phones.addresses(),
            "name": session.phones.pc_name(), "devices": session.phones.status()}


@app.post("/api/phones/pair")
def phones_pair():
    if not (session.settings.get("phones") or {}).get("enabled"):
        session.apply_settings({"phones": {"enabled": True}})
    return session.phones.new_pairing()


@app.delete("/api/phones/{device_id}")
def phones_remove(device_id: str):
    ok = session.phones.remove(device_id)
    session.emit({"type": "phones"})
    return {"ok": ok}


# --------------------------------------------------------------------------- WebSocket
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        await ws.close(code=4403)
        return
    await ws.accept()
    clients.add(ws)
    await ws.send_json({"type": "state", "state": session.state})
    await ws.send_json({"type": "handsfree", "on": session.voice_mode != "off"})
    await ws.send_json({"type": "voice_mode", "mode": session.voice_mode, "wake_ready": session.mic.wake is not None})
    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")
            if t == "user_text":
                await session.handle_user(msg.get("text", "").strip())
            elif t == "ptt_start":
                await asyncio.to_thread(session.ptt_start)
            elif t == "ptt_stop":
                await asyncio.to_thread(session.ptt_stop)
            elif t == "handsfree":
                await asyncio.to_thread(session.set_handsfree, bool(msg.get("on")))
            elif t == "confirm":
                session.resolve_confirm(msg["id"], bool(msg.get("approve")))
            elif t == "stop":
                await session.interrupt()
                session.set_state(session._idle_state())
            elif t == "stop_speaking":
                session.stop_speaking()
            elif t == "voice_mode":
                await asyncio.to_thread(session.set_voice_mode, msg.get("mode", "off"))
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


def main():
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
