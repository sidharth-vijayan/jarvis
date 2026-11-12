"""JARVIS backend: chat API, conversation history, settings, and the web UI."""
import asyncio
import json
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, llm, voice

Mode = Literal["fast", "balanced", "advanced"]
# ponytail: naive "last N messages" context window; make it token-aware / summarized in Phase 4 (memory).
HISTORY_LIMIT = 40

SYSTEM_PROMPT = """You are JARVIS, a personal AI assistant running locally on the user's Windows laptop.
Be concise, direct and courteous, with occasional dry wit that is never at the user's expense.
Use Markdown only when it helps.
Current date and time: {now}.
You cannot yet open apps, read files, browse the web, read email or set reminders: those tools are not
connected in this version. If asked to do one of those, say so plainly. Never claim to have performed an action."""
SPOKEN = """
The user is speaking to you and will hear your reply read aloud: answer in one to three short
conversational sentences, with no Markdown, lists, code or emoji."""
MAX_AUDIO_BYTES = 25_000_000


@asynccontextmanager
async def lifespan(_app):
    db.init()
    yield


app = FastAPI(title="JARVIS", lifespan=lifespan)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: int | None = None
    mode: Mode | None = None
    voice: bool = False  # the request was spoken, so the reply will be read aloud


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    voice: str = Field(min_length=1, max_length=100)
    speed: float = Field(1.0, ge=0.5, le=2.0)


class SettingsIn(BaseModel):
    mode: Mode | None = None
    model_fast: str | None = Field(None, min_length=1, max_length=200)
    model_balanced: str | None = Field(None, min_length=1, max_length=200)
    model_advanced: str | None = Field(None, min_length=1, max_length=200)


def _line(event: dict) -> str:
    return json.dumps(event) + "\n"


@app.post("/api/chat")
async def chat(body: ChatIn):
    settings = db.get_settings()
    mode = body.mode or settings["mode"]
    model = settings[f"model_{mode}"]
    cid = body.conversation_id
    if cid is None:
        cid = db.create_conversation(body.message.strip()[:60])
    elif not db.conversation_exists(cid):
        raise HTTPException(404, "Conversation not found")
    db.add_message(cid, "user", body.message)
    system = SYSTEM_PROMPT.format(now=datetime.now().strftime("%A %Y-%m-%d %H:%M")) + (SPOKEN if body.voice else "")
    messages = [{"role": "system", "content": system}]
    messages += [{"role": m["role"], "content": m["content"]} for m in db.get_messages(cid)[-HISTORY_LIMIT:]]

    async def stream():
        yield _line({"type": "start", "conversation_id": cid, "model": model, "mode": mode})
        parts, thinking = [], False
        try:
            async for chunk in llm.chat_stream(model, messages, think=mode != "fast"):
                msg = chunk.get("message", {})
                if msg.get("thinking") and not thinking:
                    thinking = True
                    yield _line({"type": "thinking"})
                if msg.get("content"):
                    parts.append(msg["content"])
                    yield _line({"type": "token", "text": msg["content"]})
                if chunk.get("done"):
                    ns = chunk.get("eval_duration") or 0
                    tokens = chunk.get("eval_count", 0)
                    yield _line({"type": "done", "tokens": tokens,
                                 "tokens_per_s": round(tokens / (ns / 1e9), 1) if ns else None})
        except httpx.ConnectError:
            yield _line({"type": "error", "message": "Ollama is not running. Start it, then try again."})
        except Exception as e:
            yield _line({"type": "error", "message": str(e) or type(e).__name__})
        finally:
            # Also runs when the client presses Stop (the stream is cancelled): keep the partial answer.
            if parts:
                db.add_message(cid, "assistant", "".join(parts))

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/api/voice")
def voice_info():
    voice.warm()
    return {"voices": voice.voices()}


@app.post("/api/stt")
async def speech_to_text(request: Request):
    if int(request.headers.get("content-length") or 0) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Recording is too long")
    audio = await request.body()
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(400, "No audio received")
    try:
        return {"text": await run_in_threadpool(voice.transcribe, audio)}
    except Exception:
        raise HTTPException(400, "Could not read that recording")


@app.websocket("/api/wake")
async def wake_word(ws: WebSocket):
    """The app streams microphone audio here while the wake word is on. Audio is scored and dropped, never stored."""
    await ws.accept()
    detector = await run_in_threadpool(voice.WakeDetector)
    try:
        while True:
            frame = await ws.receive_bytes()
            if len(frame) != voice.WAKE_FRAME_BYTES:
                continue
            score = detector.score(frame)
            if score >= voice.WAKE_THRESHOLD:
                detector.reset()
                await ws.send_text("wake")
            elif score >= 0.15:  # near miss: lets the UI show how close it was, for tuning
                await ws.send_text(f"score:{score:.2f}")
    except WebSocketDisconnect:
        pass


@app.post("/api/tts")
def text_to_speech(body: SpeakIn):
    if body.voice not in voice.voices():
        raise HTTPException(404, "Voice not installed")
    return Response(voice.speak(body.text, body.voice, body.speed), media_type="audio/wav")


_preloaded = False


@app.get("/api/status")
async def status():
    global _preloaded
    try:
        models = await llm.list_models()
        if not _preloaded:  # first page load: get the everyday model into memory in the background
            _preloaded = True
            app.state.preload = asyncio.create_task(llm.preload(db.get_settings()["model_fast"]))
        return {"ollama": True, "models": models, "loaded": await llm.loaded_models()}
    except (httpx.HTTPError, KeyError, ValueError):
        return {"ollama": False, "models": [], "loaded": []}


_gpu_cache = (0.0, None)


def _gpu():
    """GPU load and memory from nvidia-smi, cached for 5 s. None when there is no NVIDIA GPU."""
    global _gpu_cache
    if time.monotonic() - _gpu_cache[0] > 5:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW).stdout
            load, used, total = (float(x) for x in out.strip().splitlines()[0].split(","))
            gpu = {"load": load, "vram": round(100 * used / total, 1)}
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            gpu = None
        _gpu_cache = (time.monotonic(), gpu)
    return _gpu_cache[1]


@app.get("/api/system")
def system():
    battery = psutil.sensors_battery()
    return {
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage(Path.home().anchor).percent,
        "battery": {"percent": battery.percent, "plugged": battery.power_plugged} if battery else None,
        "gpu": _gpu(),
    }


@app.get("/api/graph")
async def graph():
    """Everything JARVIS currently knows about, as nodes and links for the 3D map."""
    settings = db.get_settings()
    try:
        models = await llm.list_models()
    except (httpx.HTTPError, KeyError, ValueError):
        models = []
    nodes = [{"id": "core", "label": "JARVIS", "type": "core", "detail": "The assistant itself."}]
    links = []

    def add(node_id, label, kind, detail, parent):
        nodes.append({"id": node_id, "label": label, "type": kind, "detail": detail})
        links.append({"source": parent, "target": node_id})

    add("hub:conversations", "Conversations", "hub", "Every saved conversation.", "core")
    add("hub:models", "Models", "hub", "Language models installed in Ollama.", "core")
    add("hub:modes", "Modes", "hub", "Reasoning modes and the model each one uses.", "core")
    add("hub:voice", "Voice", "hub", "Speech recognition, spoken voices and the wake word.", "core")
    for c in db.list_conversations():
        add(f"conversation:{c['id']}", c["title"], "conversation", f"Last updated {c['updated_at']} UTC", "hub:conversations")
    for m in sorted(set(models) | {settings[f"model_{mode}"] for mode in ("fast", "balanced", "advanced")}):
        add(f"model:{m}", m, "model", "Installed" if m in models else "Not installed", "hub:models")
    for mode in ("fast", "balanced", "advanced"):
        add(f"mode:{mode}", mode.capitalize(), "mode", f"Uses {settings[f'model_{mode}']}", "hub:modes")
        links.append({"source": f"mode:{mode}", "target": f"model:{settings[f'model_{mode}']}"})
    for v in voice.voices():
        add(f"voice:{v}", v, "voice", "Piper voice for spoken replies.", "hub:voice")
    add("speech:whisper", f"Whisper {voice.WHISPER_MODEL}", "speech", "Turns your speech into text, on this laptop.", "hub:voice")
    add("speech:wake", "Wake word", "speech", 'Listens for "Hey JARVIS".', "hub:voice")
    return {"nodes": nodes, "links": links}


@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    db.set_settings(body.model_dump(exclude_none=True))
    return db.get_settings()


@app.get("/api/conversations")
def conversations():
    return db.list_conversations()


@app.get("/api/conversations/{cid}/messages")
def conversation_messages(cid: int):
    if not db.conversation_exists(cid):
        raise HTTPException(404, "Conversation not found")
    return db.get_messages(cid)


@app.delete("/api/conversations/{cid}", status_code=204)
def delete_conversation(cid: int):
    db.delete_conversation(cid)


class FreshStatic(StaticFiles):
    """Makes the browser re-check files on every load, so an updated interface is never shown from cache."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", FreshStatic(directory=Path(__file__).parent.parent / "web", html=True), name="web")
