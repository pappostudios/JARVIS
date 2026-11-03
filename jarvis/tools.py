"""Tool registry: which Python functions Gemini may call, plus the safety layer that holds
outward-facing actions (sending email, inviting people, deleting events) for the user's approval."""
from __future__ import annotations

import json
import traceback
from datetime import date, datetime
from typing import Any, Callable, Optional

from . import config, db, google_services as g, projects as p


# ── time tool ─────────────────────────────────────────────────
def get_current_time() -> dict:
    """Get the current local date, time, weekday and time zone."""
    n = config.now()
    return {"now": n.isoformat(timespec="minutes"), "weekday": n.strftime("%A"), "timezone": config.tz_name()}


# ── confirmation layer ────────────────────────────────────────
class _TurnCounter:
    value = 0


turns = _TurnCounter()


def _needs_confirmation(name: str, args: dict) -> bool:
    if db.get_setting("require_confirmation").lower() != "true":
        return False
    if name in ("send_email", "reply_to_email", "delete_event", "delete_project"):
        return True
    if name == "create_event" and args.get("attendees"):
        return True
    if name == "update_event" and (args.get("add_attendees") or args.get("remove_attendees")):
        return True
    return False


def _describe(name: str, a: dict) -> str:
    if name == "send_email":
        return f"Send email to {', '.join(a.get('to', []))} — “{a.get('subject', '')}”\n\n{a.get('body', '')}"
    if name == "reply_to_email":
        who = "everyone" if a.get("reply_all") else "the sender"
        return f"Reply to {who} (email {a.get('message_id')}):\n\n{a.get('body', '')}"
    if name == "create_event":
        att = ", ".join(a.get("attendees") or [])
        meet = " + Google Meet" if a.get("add_google_meet") else ""
        return f"Create “{a.get('title')}” at {a.get('start')}{meet} and invite {att}"
    if name == "update_event":
        return f"Update event {a.get('event_id')}: " + json.dumps({k: v for k, v in a.items() if k != "event_id"})
    if name == "delete_event":
        return f"Delete calendar event {a.get('event_id')}"
    if name == "delete_project":
        return f"Delete project “{a.get('project')}”"
    return f"{name}({json.dumps(a)})"


def _enrich_summary(name: str, a: dict, summary: str) -> str:
    """Add human context (event title / email subject) where cheap to fetch."""
    try:
        if name == "delete_event":
            ev = g._svc("calendar", "v3").events().get(calendarId="primary", eventId=a["event_id"]).execute()
            st = ev.get("start", {})
            return f"Delete “{ev.get('summary', '(no title)')}” on {st.get('dateTime') or st.get('date')}"
        if name == "reply_to_email":
            m = g.read_email(a["message_id"])
            return f"Reply to {m['from']} — “{m['subject']}”\n\n{a.get('body', '')}"
    except Exception:  # noqa: BLE001
        pass
    return summary


def confirm_action(action_id: int) -> dict:
    """Execute a pending action AFTER the user has explicitly approved it in this conversation
    (e.g. they said "yes, send it"). Never call this in the same turn the action was proposed.

    Args:
        action_id: The pending action id.
    """
    act = db.get_pending(int(action_id))
    if not act or act["status"] != "pending":
        return {"error": f"No pending action #{action_id}."}
    if turns.value <= act["created_turn"]:
        return {"error": "The user has not approved this yet. Read the draft to them and ask for confirmation."}
    return execute_pending(int(action_id))


def cancel_action(action_id: int) -> dict:
    """Cancel / discard a pending action the user does not want.

    Args:
        action_id: The pending action id.
    """
    db.set_pending_status(int(action_id), "cancelled")
    return {"cancelled": action_id}


def execute_pending(action_id: int) -> dict:
    act = db.get_pending(action_id)
    if not act or act["status"] != "pending":
        return {"error": f"No pending action #{action_id}."}
    fn = TOOLS[act["tool"]]
    try:
        result = fn(**act["args"])
        db.set_pending_status(action_id, "done", result)
        return {"done": True, "action_id": action_id, "result": result}
    except Exception as e:  # noqa: BLE001
        db.set_pending_status(action_id, "failed", str(e))
        return {"error": str(e)}


# ── registry ──────────────────────────────────────────────────
TOOL_FUNCS: list[Callable[..., Any]] = [
    get_current_time,
    # calendar
    g.list_events, g.create_event, g.update_event, g.delete_event, g.find_free_slots,
    # email
    g.list_emails, g.read_email, g.send_email, g.reply_to_email, g.create_draft,
    g.mark_emails_read, g.archive_emails,
    # projects & tasks
    p.list_projects, p.get_project, p.create_project, p.update_project, p.delete_project, p.log_progress,
    p.add_task, p.list_tasks, p.prioritized_tasks, p.update_task, p.complete_task, p.delete_task,
    # memory
    p.remember, p.list_memories, p.forget,
    # approvals
    confirm_action, cancel_action,
]
TOOLS: dict[str, Callable[..., Any]] = {f.__name__: f for f in TOOL_FUNCS}

# Friendly labels for the dashboard's live status line.
STATUS_LABELS = {
    "list_events": "Reading calendar", "create_event": "Scheduling", "update_event": "Updating calendar",
    "delete_event": "Updating calendar", "find_free_slots": "Checking availability",
    "list_emails": "Scanning inbox", "read_email": "Reading email", "send_email": "Composing email",
    "reply_to_email": "Composing reply", "create_draft": "Drafting email", "mark_emails_read": "Tidying inbox",
    "archive_emails": "Archiving", "remember": "Committing to memory",
    "list_projects": "Reviewing projects", "get_project": "Reviewing project", "create_project": "Creating project",
    "update_project": "Updating project", "log_progress": "Logging progress", "add_task": "Updating to-do list",
    "list_tasks": "Checking to-do list", "prioritized_tasks": "Prioritising", "update_task": "Updating to-do list", "complete_task": "Ticking it off",
    "confirm_action": "Executing", "get_current_time": "Checking the time",
}


def _jsonable(x: Any) -> Any:
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def call_tool(name: str, args: Optional[dict]) -> dict:
    """Run a tool requested by the model and always return a JSON-able dict."""
    args = dict(args or {})
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"Unknown tool {name}"}
    try:
        if _needs_confirmation(name, args):
            summary = _enrich_summary(name, args, _describe(name, args))
            aid = db.add_pending(name, args, summary, turns.value)
            return {
                "status": "awaiting_user_approval",
                "action_id": aid,
                "instruction": "Nothing has been sent yet. Briefly tell the user what you prepared and ask them "
                               "to confirm (they can also press Approve on the dashboard). When they confirm in a "
                               "later message, call confirm_action with this action_id.",
            }
        result = fn(**args)
        return {"result": _jsonable(result)}
    except g.GoogleNotConnected as e:
        return {"error": str(e)}
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return {"error": f"{type(e).__name__}: {e}"}
