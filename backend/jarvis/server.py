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

from . import audio, config, models
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


@app.post("/api/reset")
def reset():
    session.reset()
    return {"ok": True}


@app.get("/api/memory")
def memory():
    return {"text": config.MEMORY_FILE.read_text("utf-8") if config.MEMORY_FILE.exists() else ""}


@app.post("/api/memory")
def set_memory(body: dict):
    config.MEMORY_FILE.write_text(body.get("text", ""), "utf-8")
    return {"ok": True}


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
    await ws.send_json({"type": "handsfree", "on": session.mic.mode == "handsfree"})
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
                session.set_state("listening" if session.mic.mode == "handsfree" else "idle")
            elif t == "stop_speaking":
                session.stop_speaking()
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
