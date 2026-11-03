"""The JARVIS brain: Gemini + function calling loop with conversation memory."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterator

from google import genai
from google.genai import errors, types

from . import config, db, projects, tools

log = logging.getLogger("jarvis.agent")

MAX_TOOL_ROUNDS = 10
REQUEST_TIMEOUT_MS = 75_000

# "Thinking" = how long Gemini reasons before answering. Newer models use levels, older ones a token budget.
_THINKING = {
    "fast": [types.ThinkingConfig(thinking_level="LOW"), types.ThinkingConfig(thinking_budget=0),
             types.ThinkingConfig(thinking_budget=512)],
    "balanced": [types.ThinkingConfig(thinking_level="MEDIUM"), types.ThinkingConfig(thinking_budget=2048)],
    "deep": [types.ThinkingConfig(thinking_level="HIGH"), types.ThinkingConfig(thinking_budget=-1)],
}
MAX_HISTORY_CONTENTS = 40
FALLBACK_MODELS = ["gemini-flash-latest", "gemini-2.5-flash"]

PERSONA = """You are J.A.R.V.I.S. — "Just A Rather Very Intelligent System" — a refined British AI personal
assistant and chief of staff. You speak like a well-educated Englishman: composed, warm, quietly witty, impeccably
polite, using British spelling and idiom ("certainly", "right away", "I've taken the liberty of…"). You address the
user as "{address}". Dry humour is welcome; waffle is not.

Your job: run the user's day. You manage their Google Calendar (read, create, move, cancel meetings, find free time),
their Gmail (search, read, summarise, draft and reply), their projects and to-do list (track tasks, progress and
deadlines), and you help them plan their schedule realistically.

How you work:
- Your replies are usually SPOKEN ALOUD. Keep them short and natural: 1–4 sentences unless asked for detail. No
  markdown tables, no headings, no emojis. Use plain sentences; short lists only when genuinely clearer.
- Act, don't ask, for safe things: reading calendar/email, managing tasks/projects, creating personal events or focus
  blocks. Use tools rather than guessing. Resolve relative dates ("tomorrow", "next Tuesday") from the current time
  below. When times are vague, pick a sensible slot using find_free_slots.
- Outward-facing actions (sending/replying to email, inviting other people, cancelling events) go through approval:
  the tool returns "awaiting_user_approval". Then tell the user what you prepared — read out the recipient, subject and
  gist — and ask "Shall I send it?". Only when they confirm in their next message, call confirm_action.
- Emails you write: match the user's tone, concise, signed with the user's name if known. Never invent facts,
  commitments or prices.
- Summarising email: lead with what needs action, then FYIs; group by importance; mention senders by name.
- Planning: combine calendar free slots, task priorities, deadlines and estimates. Propose a timed plan, protect
  breaks, don't overbook. Once the user agrees, put focus blocks in the calendar.
- Projects: when the user reports progress, log it (log_progress) and update/complete tasks. Proactively notice
  overdue tasks and approaching deadlines.
- When the user tells you a lasting fact or preference, save it with remember.
- If a tool errors (e.g. Google not connected), explain briefly and how to fix it.
"""


def _system_prompt() -> str:
    s = db.get_settings()
    address = s.get("address_as") or "sir"
    now = config.now()
    parts = [PERSONA.format(address=address)]
    parts.append(
        f"\nCurrent local time: {now:%A %d %B %Y, %H:%M} ({config.tz_name()}). "
        f"Working hours: {s['work_start']}–{s['work_end']}."
    )
    if s.get("user_name"):
        parts.append(f"The user's name is {s['user_name']}.")
    if s.get("about_me"):
        parts.append(f"About the user: {s['about_me']}")
    mems = projects.list_memories()
    if mems:
        parts.append("Things you remember about the user:\n" + "\n".join(f"- [{m['id']}] {m['fact']}" for m in mems[-60:]))
    try:
        projs = projects.list_projects()
        if projs:
            lines = [
                f"- {p['name']} ({p['status']}, {p['priority']} priority, {p['progress_percent']}% done"
                + (f", deadline {p['deadline']}" if p.get("deadline") else "") + ")"
                for p in projs[:20]
            ]
            parts.append("Active projects:\n" + "\n".join(lines))
        overdue = projects.list_tasks(due_before=(now.date().isoformat()), limit=10)
        if overdue:
            parts.append("Open tasks due today or overdue:\n" + "\n".join(
                f"- #{t['id']} {t['title']} (due {t['due_date']}{', ' + t['project'] if t.get('project') else ''})"
                for t in overdue))
    except Exception:  # noqa: BLE001
        pass
    pend = db.list_pending()
    if pend:
        parts.append("Actions awaiting the user's approval:\n" + "\n".join(
            f"- action_id {a['id']}: {a['summary'][:300]}" for a in pend))
    return "\n".join(parts)


class Jarvis:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.client: genai.Client | None = None
        self.model = config.GEMINI_MODEL
        self.history: list[types.Content] = []
        self._decls: list[types.FunctionDeclaration] | None = None
        self._thinking_ok: dict[tuple[str, str], int] = {}
        self._restore_history()

    # ── setup ──
    def _ensure_client(self) -> genai.Client:
        if self.client is None:
            if not config.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is missing. Add it to the .env file and restart JARVIS.")
            self.client = genai.Client(
                api_key=config.GEMINI_API_KEY,
                http_options=types.HttpOptions(
                    timeout=REQUEST_TIMEOUT_MS,
                    retry_options=types.HttpRetryOptions(attempts=2, initial_delay=1.0, max_delay=4.0),
                ),
            )
        return self.client

    def _declarations(self) -> list[types.FunctionDeclaration]:
        if self._decls is None:
            client = self._ensure_client()
            self._decls = [
                types.FunctionDeclaration.from_callable(client=client._api_client, callable=f, use_json_schema=True)
                for f in tools.TOOL_FUNCS
            ]
        return self._decls

    def _restore_history(self) -> None:
        for m in db.recent_messages(16):
            role = "user" if m["role"] == "user" else "model"
            self.history.append(types.Content(role=role, parts=[types.Part(text=m["text"])]))
        # Gemini requires the conversation to start with a user turn.
        while self.history and self.history[0].role != "user":
            self.history.pop(0)

    def reset(self) -> None:
        with self._lock:
            self.history = []
            db.clear_messages()

    # ── helpers ──
    def _trim_history(self) -> None:
        if len(self.history) <= MAX_HISTORY_CONTENTS:
            return
        start = len(self.history) - MAX_HISTORY_CONTENTS
        # cut only at a plain user text turn so function call/response pairs stay intact
        for i in range(start, len(self.history)):
            c = self.history[i]
            if c.role == "user" and c.parts and all(p.function_response is None for p in c.parts):
                self.history = self.history[i:]
                return

    def _generate(self, config_: types.GenerateContentConfig) -> types.GenerateContentResponse:
        client = self._ensure_client()
        models = [self.model] + [m for m in FALLBACK_MODELS if m != self.model]
        last_err: Exception | None = None
        level = db.get_setting("thinking") or "fast"
        for model in models:
            thinking: list[types.ThinkingConfig | None] = list(_THINKING.get(level, _THINKING["fast"])) + [None]
            t_idx, attempt = self._thinking_ok.get((model, level), 0), 0
            while t_idx < len(thinking):
                config_.thinking_config = thinking[t_idx]
                t0 = time.time()
                try:
                    resp = client.models.generate_content(model=model, contents=self.history, config=config_)
                    log.info("gemini %s ok in %.1fs (thinking=%s)", model, time.time() - t0,
                             thinking[t_idx].model_dump(exclude_none=True) if thinking[t_idx] else "default")
                    self._thinking_ok[(model, level)] = t_idx  # remember the form this model accepts
                    if model != self.model:
                        log.warning("model '%s' unavailable, switched to '%s'", self.model, model)
                        self.model = model
                    return resp
                except errors.ClientError as e:
                    last_err = e
                    log.warning("gemini %s error %s after %.1fs: %s", model, e.code, time.time() - t0, str(e)[:300])
                    if e.code == 400 and "think" in str(e).lower():
                        t_idx += 1  # this model doesn't accept that thinking setting; try the next form
                        continue
                    if e.code == 404:
                        break  # model not available to this key: try the next model
                    if e.code == 429:
                        raise RuntimeError(
                            "Gemini says the usage limit was reached (429). On the free tier wait a minute and try "
                            "again, or enable billing for your key at aistudio.google.com.") from e
                    raise
                except errors.ServerError as e:
                    last_err = e
                    log.warning("gemini %s server error %s: %s", model, e.code, str(e)[:200])
                    attempt += 1
                    if attempt < 2:
                        time.sleep(1.5)
                        continue
                    raise RuntimeError("Gemini is temporarily overloaded. Please try again in a moment.") from e
                except Exception as e:  # noqa: BLE001  (timeouts, network)
                    log.warning("gemini %s failed after %.1fs: %r", model, time.time() - t0, e)
                    if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
                        raise RuntimeError("Gemini took too long to answer. Please try again "
                                           "(if this keeps happening, set Settings → Response speed to Fast).") from e
                    raise RuntimeError(f"Couldn't reach Gemini: {e}") from e
        raise last_err or RuntimeError("No Gemini model available")

    # ── main entry ──
    def chat(self, user_text: str) -> Iterator[dict[str, Any]]:
        """Yields events: {"type": "status", "text"} … then {"type": "reply", "text", "tools"} or {"type": "error"}."""
        with self._lock:
            tools.turns.value += 1
            db.log_message("user", user_text)
            self.history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
            used: list[str] = []
            try:
                cfg = types.GenerateContentConfig(
                    system_instruction=_system_prompt(),
                    tools=[types.Tool(function_declarations=self._declarations())],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    temperature=0.6,
                )
                t_start = time.time()
                reply = ""
                for _ in range(MAX_TOOL_ROUNDS):
                    yield {"type": "status", "text": "Thinking"}
                    resp = self._generate(cfg)
                    cand = resp.candidates[0] if resp.candidates else None
                    if cand is None or cand.content is None:
                        reply = "My apologies — I couldn't produce a response to that."
                        break
                    self.history.append(cand.content)
                    calls = [p.function_call for p in (cand.content.parts or []) if p.function_call]
                    if not calls:
                        reply = "".join(p.text or "" for p in cand.content.parts or [] if not p.thought).strip()
                        break
                    responses = []
                    for call in calls:
                        used.append(call.name)
                        yield {"type": "status", "text": tools.STATUS_LABELS.get(call.name, "Working"), "tool": call.name}
                        result = tools.call_tool(call.name, call.args)
                        responses.append(types.Part.from_function_response(name=call.name, response=result))
                    self.history.append(types.Content(role="user", parts=responses))
                else:
                    reply = "I've been at that rather a while — shall I keep going?"
                if not reply:
                    reply = "Done."
                db.log_message("jarvis", reply)
                log.info("turn done in %.1fs, tools=%s", time.time() - t_start, used)
                self._trim_history()
                yield {"type": "reply", "text": reply, "tools": used}
            except Exception as e:  # noqa: BLE001
                # drop the dangling turn so the conversation stays valid
                while self.history and not (self.history[-1].role == "user" and self.history[-1].parts
                                            and self.history[-1].parts[0].text == user_text):
                    self.history.pop()
                if self.history:
                    self.history.pop()
                log.exception("chat turn failed")
                msg = str(e)
                if "API key" in msg or "API_KEY" in msg:
                    msg = "Gemini rejected the API key. Check GEMINI_API_KEY in the .env file."
                yield {"type": "error", "text": msg}

    def one_shot(self, prompt: str, parts: list[types.Part] | None = None, model: str | None = None) -> str:
        """Single stateless call (used for transcription etc.)."""
        client = self._ensure_client()
        contents: list[Any] = list(parts or []) + [prompt]
        try:
            resp = client.models.generate_content(model=model or config.GEMINI_FAST_MODEL, contents=contents)
        except errors.ClientError as e:
            if e.code != 404:
                raise
            resp = client.models.generate_content(model=self.model, contents=contents)
        return (resp.text or "").strip()


jarvis = Jarvis()
