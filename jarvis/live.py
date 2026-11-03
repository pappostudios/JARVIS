"""Live voice mode: a phone-call style connection.

    browser mic ──16 kHz PCM──▶ JARVIS ──▶ Gemini Live ──24 kHz voice──▶ JARVIS ──▶ browser speaker
                                   ▲                      │
                                   └── calendar / email / tasks tools (same ones as the text chat)

One continuous session per call: Gemini hears you while you speak, answers in its own voice, and can be
interrupted. It uses the Live API's quota, which is separate from the text models' quota.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import anyio
from fastapi import WebSocket
from google.genai import types

from . import agent, db, tools, usage

log = logging.getLogger("jarvis.live")

IN_RATE = 16000    # what the browser sends
OUT_RATE = 24000   # what Gemini Live speaks
# Known-good Live models, used if the model list can't be read. Newest first.
FALLBACK_LIVE_MODELS = [
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-live-2.5-flash-preview",
]
_NOT_ASSISTANTS = ("transcribe", "translate", "robotics", "tts", "image")
_bad_until: dict[str, float] = {}
_cache: dict[str, Any] = {"t": 0.0, "models": []}

LIVE_ADDENDUM = """

LIVE VOICE CALL — you are talking to the user in real time, out loud.
- Speak with a refined, received-pronunciation British English accent: a composed, warm, slightly wry English
  butler. Natural pace, never theatrical.
- Answer in the language the user is speaking.
- Keep turns short and conversational (usually one to three sentences); they can always ask for more.
- Before a tool that takes a moment (reading email, checking the calendar), you may say a brief "One moment, sir."
- Never read out ids, links or long lists verbatim; summarise naturally.
- Everything else in your instructions still applies, including asking before sending emails."""


def _version(name: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def live_models() -> list[str]:
    """Live-capable assistant models this key can use, newest first (cached 10 min)."""
    if time.time() - _cache["t"] > 600:
        names: list[str] = []
        try:
            for m in agent.jarvis._ensure_client().models.list():
                n = (m.name or "").removeprefix("models/")
                acts = m.supported_actions or []
                if "bidiGenerateContent" in acts and ("live" in n or "native-audio" in n) \
                        and not any(x in n for x in _NOT_ASSISTANTS):
                    names.append(n)
        except Exception as e:  # noqa: BLE001
            log.warning("could not list live models: %r", e)
        names.sort(key=_version, reverse=True)
        _cache.update(t=time.time(), models=names)
    found = list(_cache["models"])
    order = found + [m for m in FALLBACK_LIVE_MODELS if m not in found]
    now = time.time()
    return [m for m in order if _bad_until.get(m, 0) < now] or order


_live_decls: list[types.FunctionDeclaration] | None = None


def _declarations() -> list[types.FunctionDeclaration]:
    """Same tools as the text chat, in the classic schema form the Live API accepts everywhere."""
    global _live_decls
    if _live_decls is None:
        client = agent.jarvis._ensure_client()
        _live_decls = [types.FunctionDeclaration.from_callable(client=client._api_client, callable=f)
                       for f in tools.TOOL_FUNCS]
    return _live_decls


def _recent_conversation() -> str:
    msgs = db.recent_messages(10)
    if not msgs:
        return ""
    lines = [f"{'User' if m['role'] == 'user' else 'You'}: {m['text'][:300]}" for m in msgs]
    return "\n\nRecent conversation (for context):\n" + "\n".join(lines)


def build_config(handle: str | None, minimal: bool = False) -> types.LiveConnectConfig:
    s = db.get_settings()
    cfg: dict[str, Any] = dict(
        response_modalities=["AUDIO"],
        system_instruction=agent._system_prompt() + LIVE_ADDENDUM + _recent_conversation(),
        tools=[types.Tool(function_declarations=_declarations())],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=s.get("gemini_voice") or "Charon"))),
    )
    if not minimal:
        # keep calls going for hours, and survive the server's periodic connection resets
        cfg["context_window_compression"] = types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow())
        cfg["session_resumption"] = types.SessionResumptionConfig(handle=handle)
    return types.LiveConnectConfig(**cfg)


class _Call:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.handle: str | None = None
        self.user_buf = ""
        self.jarvis_buf = ""
        self.new_turn = True
        self.hung_up = False
        self.model = ""

    async def send(self, **event: Any) -> None:
        try:
            await self.ws.send_text(json.dumps(event))
        except Exception:  # noqa: BLE001
            self.hung_up = True

    # ── browser → Gemini ──
    async def pump_in(self, session) -> None:
        while True:
            msg = await self.ws.receive()
            if msg.get("type") == "websocket.disconnect":
                self.hung_up = True
                return
            if msg.get("bytes"):
                await session.send_realtime_input(
                    audio=types.Blob(data=msg["bytes"], mime_type=f"audio/pcm;rate={IN_RATE}"))
            elif msg.get("text"):
                data = json.loads(msg["text"])
                if data.get("type") == "hangup":
                    self.hung_up = True
                    return
                if data.get("type") == "text" and data.get("text", "").strip():
                    self.user_buf += data["text"]
                    await self._begin_turn()
                    await session.send_realtime_input(text=data["text"])
                if data.get("type") == "mic_paused":
                    await session.send_realtime_input(audio_stream_end=True)

    async def _begin_turn(self) -> None:
        if self.new_turn:
            tools.turns.value += 1  # lets confirm_action see that the user spoke after a proposal
            self.new_turn = False

    # ── Gemini → browser ──
    async def pump_out(self, session) -> str:
        while True:
            async for r in session.receive():
                if r.usage_metadata:
                    usage.record_tokens(self.model, "live", r.usage_metadata)
                if r.session_resumption_update and r.session_resumption_update.resumable \
                        and r.session_resumption_update.new_handle:
                    self.handle = r.session_resumption_update.new_handle
                if r.go_away is not None:
                    log.info("live: server asked to reconnect (time left %s)", r.go_away.time_left)
                    return "reconnect"
                if r.tool_call:
                    await self._run_tools(session, r.tool_call.function_calls or [])
                sc = r.server_content
                if not sc:
                    continue
                if sc.input_transcription and sc.input_transcription.text:
                    await self._begin_turn()
                    self.user_buf += sc.input_transcription.text
                    await self.send(type="user_text", text=sc.input_transcription.text)
                if sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            await self.ws.send_bytes(part.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    self.jarvis_buf += sc.output_transcription.text
                    await self.send(type="jarvis_text", text=sc.output_transcription.text)
                if sc.interrupted:
                    await self.send(type="interrupted")
                if sc.turn_complete:
                    self._save_turn()
                    await self.send(type="turn_complete")

    async def _run_tools(self, session, calls) -> None:
        responses = []
        for fc in calls:
            await self.send(type="status", text=tools.STATUS_LABELS.get(fc.name, "Working"), tool=fc.name)
            result = await anyio.to_thread.run_sync(tools.call_tool, fc.name, dict(fc.args or {}))
            responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
            log.info("live tool %s -> %s", fc.name, "error" if "error" in result else "ok")
        await session.send_tool_response(function_responses=responses)
        await self.send(type="refresh")

    def _save_turn(self) -> None:
        user, said = self.user_buf.strip(), self.jarvis_buf.strip()
        if user:
            db.log_message("user", user)
        if said:
            db.log_message("jarvis", said)
        if user or said:  # keep the text chat's memory in step with the call
            h = agent.jarvis.history
            if user:
                h.append(types.Content(role="user", parts=[types.Part(text=user)]))
            if said:
                h.append(types.Content(role="model", parts=[types.Part(text=said)]))
        self.user_buf = self.jarvis_buf = ""
        self.new_turn = True


async def handle(ws: WebSocket) -> None:
    call = _Call(ws)
    try:
        agent.jarvis._ensure_client()
    except Exception as e:  # noqa: BLE001
        await call.send(type="error", text=str(e))
        await ws.close()
        return

    models = await anyio.to_thread.run_sync(live_models)
    client = agent.jarvis._ensure_client()
    idx, minimal, reconnects, last_err = 0, False, 0, ""
    connected_at: float | None = None
    while not call.hung_up and idx < len(models) and reconnects < 50:
        model = models[idx]
        try:
            cfg = await anyio.to_thread.run_sync(build_config, call.handle, minimal)
            async with client.aio.live.connect(model=model, config=cfg) as session:
                call.model = model
                connected_at = time.time()
                await call.send(type="ready", model=model, resumed=bool(call.handle))
                log.info("live call connected: %s%s", model, " (resumed)" if call.handle else "")
                t_in = asyncio.create_task(call.pump_in(session))
                t_out = asyncio.create_task(call.pump_out(session))
                done, pending = await asyncio.wait({t_in, t_out}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                usage.record_ok(model, "live", seconds=time.time() - connected_at)
                connected_at = None
                for t in done:
                    if t.exception() and not call.hung_up:
                        raise t.exception()  # type: ignore[misc]
                if call.hung_up:
                    break
                reconnects += 1  # server asked us to reconnect: resume the same conversation
                await call.send(type="status", text="Reconnecting")
        except Exception as e:  # noqa: BLE001
            if call.hung_up:
                break
            last_err = str(e)
            log.warning("live session on %s ended: %r", model, e)
            if call.model != model:
                usage.record_error(model, "live", e)
            elif connected_at:
                usage.record_ok(model, "live", seconds=time.time() - connected_at)
                connected_at = None
            low = last_err.lower()
            if call.model == model and "1011" not in low and "closed" in low and reconnects < 50:
                reconnects += 1  # dropped mid-call: resume with the handle we have
                await call.send(type="status", text="Reconnecting")
                continue
            if not minimal and any(k in low for k in ("1007", "invalid", "not supported", "unknown name")):
                minimal = True  # this model rejects an optional setting: retry it with the basics
                continue
            quota = "429" in low or "quota" in low or "resource_exhausted" in low
            _bad_until[model] = time.time() + (600 if quota else 1800)
            idx, minimal, call.handle = idx + 1, False, None
            if idx < len(models):
                await call.send(type="status", text="Trying another voice model")
    if not call.hung_up:
        hint = (" Your Live quota may be used up for today." if "429" in last_err or "quota" in last_err.lower()
                else "")
        await call.send(type="error", text=f"Live voice couldn't connect.{hint} ({last_err[:200]})")
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass
    log.info("live call ended")
