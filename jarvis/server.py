"""FastAPI server: REST + streaming chat API and the dashboard (desktop & Android PWA)."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, google_services as g, projects, voice
from .agent import jarvis
from .tools import execute_pending

app = FastAPI(title="J.A.R.V.I.S.", docs_url=None, redoc_url=None)

# ── auth ──────────────────────────────────────────────────────
_SECRET_FILE = config.DATA_DIR / "secret.key"
if not _SECRET_FILE.exists():
    _SECRET_FILE.write_text(secrets.token_hex(32))
_SECRET = _SECRET_FILE.read_text().strip().encode()
COOKIE = "jarvis_auth"
OPEN_PATHS = {"/api/login", "/api/session", "/api/health"}


def _token() -> str:
    return hmac.new(_SECRET, config.ACCESS_CODE.encode(), hashlib.sha256).hexdigest()


def _authed(request: Request) -> bool:
    if not config.ACCESS_CODE:
        return True
    tok = request.cookies.get(COOKIE) or request.headers.get("x-jarvis-token", "")
    return hmac.compare_digest(tok, _token())


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in OPEN_PATHS and not _authed(request):
        return JSONResponse({"detail": "Access code required"}, status_code=401)
    resp = await call_next(request)
    if path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


class LoginBody(BaseModel):
    code: str


_failed: dict[str, list[float]] = {}


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _failed.get(ip, []) if time.time() - t < 300]
    if len(recent) >= 8:
        raise HTTPException(429, "Too many attempts. Wait five minutes.")
    if not config.ACCESS_CODE or hmac.compare_digest(body.code.strip(), config.ACCESS_CODE):
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, _token(), max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
        return resp
    _failed[ip] = recent + [time.time()]
    raise HTTPException(401, "Incorrect access code")


@app.get("/api/session")
def session(request: Request):
    return {"authenticated": _authed(request), "auth_required": bool(config.ACCESS_CODE)}


@app.get("/api/health")
def health():
    return {"ok": True, "gemini_key": bool(config.GEMINI_API_KEY), "model": jarvis.model}


# ── chat ──────────────────────────────────────────────────────
class ChatBody(BaseModel):
    text: str


@app.post("/api/chat")
def chat(body: ChatBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Empty message")

    def stream():
        for event in jarvis.chat(text):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.get("/api/history")
def history():
    return db.recent_messages(60)


@app.post("/api/reset")
def reset():
    jarvis.reset()
    return {"ok": True}


# ── voice ─────────────────────────────────────────────────────
@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    import anyio

    data = await audio.read()
    if len(data) < 2000:
        return {"text": ""}
    mime = (audio.content_type or "audio/wav").split(";")[0]
    try:
        text = await anyio.to_thread.run_sync(voice.transcribe, data, mime)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Transcription failed: {e}") from e
    return {"text": text}


class TTSBody(BaseModel):
    text: str


@app.post("/api/tts")
async def tts(body: TTSBody):
    try:
        out = await voice.synthesize(body.text[:3000])
    except Exception as e:  # noqa: BLE001
        print("[voice] TTS failed:", e)
        return Response(status_code=204, headers={"X-TTS-Error": str(e)[:200].replace("\n", " ")})
    if out is None:
        return Response(status_code=204)
    audio, mime = out
    return Response(audio, media_type=mime)


# ── dashboard data ────────────────────────────────────────────
_overview_cache: dict[str, Any] = {"t": 0.0, "google": None}


def _google_overview() -> dict:
    out: dict[str, Any] = {"connected": False}
    try:
        today = config.now().date()
        out["events"] = g.list_events(start_date=today.isoformat(), end_date=today.isoformat(), max_results=20)
        from datetime import timedelta
        tmr = today + timedelta(days=1)
        out["tomorrow"] = g.list_events(start_date=tmr.isoformat(), end_date=tmr.isoformat(), max_results=10)
        out["emails"] = g.list_emails("in:inbox is:unread", max_results=8)
        out["unread"] = g.unread_count()
        out["connected"] = True
    except g.GoogleNotConnected as e:
        out["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


@app.get("/api/overview")
def overview(refresh: bool = False):
    if refresh or time.time() - _overview_cache["t"] > 45 or _overview_cache["google"] is None:
        _overview_cache["google"] = _google_overview()
        _overview_cache["t"] = time.time()
    n = config.now()
    return {
        "now": n.isoformat(),
        "timezone": config.tz_name(),
        "google": _overview_cache["google"],
        "projects": projects.list_projects(),
        "tasks": projects.list_tasks(limit=40),
        "done_today": len([t for t in projects.list_tasks(status="done", limit=200)
                           if (t.get("completed_at") or "").startswith(n.date().isoformat())]),
        "pending": db.list_pending(),
        "settings": db.get_settings(),
        "model": jarvis.model,
    }


# ── quick edits from the dashboard (no AI round-trip) ─────────
class TaskBody(BaseModel):
    title: str
    project: str | None = None
    due_date: str | None = None
    priority: str = "medium"


@app.post("/api/tasks")
def add_task(body: TaskBody):
    return projects.add_task(body.title, body.project or None, body.due_date or None, body.priority)


@app.post("/api/tasks/{task_id}/toggle")
def toggle_task(task_id: int):
    t = projects._task(task_id)
    return projects.update_task(task_id, status="todo" if t["status"] == "done" else "done")


@app.delete("/api/tasks/{task_id}")
def remove_task(task_id: int):
    return projects.delete_task(task_id)


class ProjectBody(BaseModel):
    name: str
    description: str = ""
    priority: str = "medium"
    deadline: str | None = None


@app.post("/api/projects")
def add_project(body: ProjectBody):
    return projects.create_project(body.name, body.description, body.priority, body.deadline or None)


@app.get("/api/projects/{project_id}")
def project_detail(project_id: int):
    return projects.get_project(str(project_id))


@app.post("/api/actions/{action_id}/approve")
def approve(action_id: int):
    r = execute_pending(action_id)
    _overview_cache["t"] = 0
    if r.get("error"):
        raise HTTPException(400, r["error"])
    db.log_message("jarvis", f"Done — {db.get_pending(action_id)['summary'].splitlines()[0]}")
    return r


@app.post("/api/actions/{action_id}/reject")
def reject(action_id: int):
    db.set_pending_status(action_id, "cancelled")
    return {"ok": True}


# ── settings & google ─────────────────────────────────────────
@app.get("/api/settings")
def get_settings():
    return {
        "settings": db.get_settings(),
        "gemini_voices": voice.GEMINI_VOICES,
        "edge_voices": voice.EDGE_BRITISH_VOICES,
        "model": jarvis.model,
        "memories": projects.list_memories(),
    }


@app.post("/api/settings")
def save_settings(values: dict[str, Any]):
    return db.set_settings(values)


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int):
    return projects.forget(memory_id)


@app.get("/api/models")
def models():
    names = voice.available_models()
    return {"current": jarvis.model, "tts": voice.tts_model() if names else None,
            "available": [n for n in names if "gemini" in n]}


@app.get("/api/google/status")
def google_status():
    return g.google_status()


@app.post("/api/google/connect")
def google_connect():
    _overview_cache["t"] = 0
    return g.start_auth_in_background()


@app.post("/api/google/disconnect")
def google_disconnect():
    g.disconnect()
    _overview_cache["t"] = 0
    return {"ok": True}


# ── static dashboard ──────────────────────────────────────────
@app.get("/sw.js")
def service_worker():
    return FileResponse(config.WEB_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
