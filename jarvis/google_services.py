"""Google Calendar + Gmail integration (OAuth, runs against *your* Google account).

Public functions (without leading underscore) are exposed to Gemini as tools.
Run `python -m jarvis.google_services` once to sign in from the desktop.
"""
from __future__ import annotations

import base64
import html
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from typing import Any, Optional

from . import config, db

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",  # read, label, draft and send
]


class GoogleNotConnected(RuntimeError):
    pass


# ── auth ──────────────────────────────────────────────────────
_auth_state: dict[str, Any] = {"running": False, "error": None}
_creds_lock = threading.Lock()


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    with _creds_lock:
        if not config.GOOGLE_TOKEN.exists():
            raise GoogleNotConnected(
                "Google account is not connected yet. Open Settings in the dashboard and press "
                "'Connect Google', or run: python -m jarvis.google_services"
            )
        creds = Credentials.from_authorized_user_file(str(config.GOOGLE_TOKEN), SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                config.GOOGLE_TOKEN.write_text(creds.to_json())
            else:
                raise GoogleNotConnected("Google sign-in expired. Please reconnect Google in Settings.")
        return creds


def _svc(name: str, version: str):
    from googleapiclient.discovery import build

    # A fresh client per call: httplib2 is not thread-safe and the dashboard + agent run concurrently.
    return build(name, version, credentials=_creds(), cache_discovery=False)


def google_status() -> dict:
    connected = False
    email = None
    if config.GOOGLE_TOKEN.exists():
        try:
            email = _svc("gmail", "v1").users().getProfile(userId="me").execute().get("emailAddress")
            connected = True
        except Exception as e:  # noqa: BLE001
            _auth_state["error"] = str(e)
    return {
        "connected": connected,
        "email": email,
        "client_secret_present": config.GOOGLE_CLIENT_SECRET.exists(),
        "auth_running": _auth_state["running"],
        "error": _auth_state["error"],
    }


def run_auth_flow(open_browser: bool = True) -> None:
    """Blocking OAuth flow: opens a browser on THIS computer for Google sign-in."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.GOOGLE_CLIENT_SECRET.exists():
        raise FileNotFoundError(
            f"Missing {config.GOOGLE_CLIENT_SECRET}. Download your OAuth 'Desktop app' client JSON from "
            "Google Cloud Console and save it there (see README)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(config.GOOGLE_CLIENT_SECRET), SCOPES)
    try:
        creds = flow.run_local_server(
            port=0, open_browser=open_browser, prompt="consent", timeout_seconds=300,
            authorization_prompt_message="Opening Google sign-in: {url}",
            success_message="JARVIS is now connected to your Google account. You can close this tab.",
        )
    except AttributeError as e:  # library raises this when the 5-minute wait expires
        raise TimeoutError("Google sign-in timed out. Press 'Sign in with Google' to try again.") from e
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "access_denied" in msg:
            raise PermissionError(
                "Google blocked the sign-in (access_denied). In Google Cloud → OAuth consent screen → "
                "Audience/Test users, add your Gmail address, then try again.") from e
        raise
    config.GOOGLE_TOKEN.write_text(creds.to_json())


def start_auth_in_background() -> dict:
    if _auth_state["running"]:
        return {"started": False, "message": "Sign-in already in progress."}

    _auth_state.update(running=True, error=None)  # set before the thread starts so status polls see it

    def _run():
        try:
            run_auth_flow()
        except Exception as e:  # noqa: BLE001
            _auth_state["error"] = str(e)
        finally:
            _auth_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "message": "Opening Google sign-in in your web browser…"}


def disconnect() -> None:
    config.GOOGLE_TOKEN.unlink(missing_ok=True)


# ── time helpers ──────────────────────────────────────────────
def _parse_dt(value: str) -> datetime:
    v = value.strip().replace("Z", "+00:00")
    if " " in v and "T" not in v:
        v = v.replace(" ", "T", 1)
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.local_tz())
    return dt


def _is_date_only(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()))


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, datetime.min.time(), tzinfo=config.local_tz())
    return start, start + timedelta(days=1)


def _fmt_event(e: dict) -> dict:
    start = e.get("start", {})
    end = e.get("end", {})
    out = {
        "id": e.get("id"),
        "title": e.get("summary", "(no title)"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": "date" in start,
    }
    if e.get("location"):
        out["location"] = e["location"]
    if e.get("description"):
        out["description"] = e["description"][:500]
    if e.get("attendees"):
        out["attendees"] = [
            {"email": a.get("email"), "response": a.get("responseStatus")} for a in e["attendees"][:20]
        ]
    if e.get("hangoutLink"):
        out["meet_link"] = e["hangoutLink"]
    if e.get("organizer", {}).get("email"):
        out["organizer"] = e["organizer"]["email"]
    return out


def _time_body(start: str, end: Optional[str], duration_minutes: int) -> tuple[dict, dict]:
    if _is_date_only(start):
        s = date.fromisoformat(start.strip())
        e = date.fromisoformat(end.strip()) if end and _is_date_only(end) else s + timedelta(days=1)
        return {"date": s.isoformat()}, {"date": e.isoformat()}
    s_dt = _parse_dt(start)
    e_dt = _parse_dt(end) if end else s_dt + timedelta(minutes=duration_minutes or 60)
    tz = config.tz_name() if "/" in config.tz_name() else None
    sb = {"dateTime": s_dt.isoformat()}
    eb = {"dateTime": e_dt.isoformat()}
    if tz:
        sb["timeZone"] = eb["timeZone"] = tz
    return sb, eb


# ── calendar tools ────────────────────────────────────────────
def list_events(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search: Optional[str] = None,
    max_results: int = 30,
) -> list[dict]:
    """Read the user's Google Calendar. Returns events with id, title, start, end, attendees, location.

    Args:
        start_date: First day to include (YYYY-MM-DD) or an ISO datetime. Defaults to today.
        end_date: Last day to include (YYYY-MM-DD, inclusive) or an ISO datetime. Defaults to start_date.
        search: Optional free-text search (title, attendees, description).
        max_results: Maximum number of events.
    """
    today = config.now().date()
    if start_date and not _is_date_only(start_date):
        t_min = _parse_dt(start_date)
    else:
        t_min = _day_bounds(date.fromisoformat(start_date) if start_date else today)[0]
    if end_date and not _is_date_only(end_date):
        t_max = _parse_dt(end_date)
    else:
        end_day = date.fromisoformat(end_date) if end_date else t_min.date()
        t_max = _day_bounds(end_day)[1]
    params = dict(
        calendarId="primary",
        timeMin=t_min.isoformat(),
        timeMax=t_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=max(1, min(int(max_results), 250)),
    )
    if search:
        params["q"] = search
    items = _svc("calendar", "v3").events().list(**params).execute().get("items", [])
    return [_fmt_event(e) for e in items if e.get("status") != "cancelled"]


def create_event(
    title: str,
    start: str,
    end: Optional[str] = None,
    duration_minutes: int = 60,
    description: str = "",
    location: str = "",
    attendees: Optional[list[str]] = None,
    add_google_meet: bool = False,
    reminder_minutes: Optional[int] = None,
) -> dict:
    """Create a calendar event or meeting. If attendees are given, Google sends them invitations.
    Use for meetings, appointments, reminders and focus/work blocks when planning the user's schedule.

    Args:
        title: Event title.
        start: Local start as ISO "YYYY-MM-DDTHH:MM", or "YYYY-MM-DD" for an all-day event.
        end: Local end (same format). If omitted, duration_minutes is used.
        duration_minutes: Length when no end is given.
        description: Notes / agenda.
        location: Place or address.
        attendees: Email addresses to invite.
        add_google_meet: Attach a Google Meet video link.
        reminder_minutes: Popup reminder this many minutes before.
    """
    s, e = _time_body(start, end, duration_minutes)
    body: dict[str, Any] = {"summary": title, "start": s, "end": e}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a.strip()} for a in attendees if a.strip()]
    if reminder_minutes is not None:
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": int(reminder_minutes)}]}
    kwargs: dict[str, Any] = {"calendarId": "primary", "body": body, "sendUpdates": "all" if attendees else "none"}
    if add_google_meet:
        body["conferenceData"] = {
            "createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}
        }
        kwargs["conferenceDataVersion"] = 1
    created = _svc("calendar", "v3").events().insert(**kwargs).execute()
    return {"created": True, **_fmt_event(created), "link": created.get("htmlLink")}


def update_event(
    event_id: str,
    title: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    add_attendees: Optional[list[str]] = None,
    remove_attendees: Optional[list[str]] = None,
) -> dict:
    """Edit / move / reschedule an existing calendar event. If only start is changed the duration is kept.

    Args:
        event_id: Event id from list_events.
        title: New title.
        start: New local start "YYYY-MM-DDTHH:MM" (or YYYY-MM-DD for all-day).
        end: New local end.
        description: New description.
        location: New location.
        add_attendees: Emails to invite.
        remove_attendees: Emails to remove.
    """
    svc = _svc("calendar", "v3")
    ev = svc.events().get(calendarId="primary", eventId=event_id).execute()
    if title is not None:
        ev["summary"] = title
    if description is not None:
        ev["description"] = description
    if location is not None:
        ev["location"] = location
    if start:
        if not end and "dateTime" in ev.get("start", {}) and not _is_date_only(start):
            old_s = _parse_dt(ev["start"]["dateTime"])
            old_e = _parse_dt(ev["end"]["dateTime"])
            dur = int((old_e - old_s).total_seconds() // 60)
            s, e = _time_body(start, None, dur)
        else:
            s, e = _time_body(start, end, 60)
        ev["start"], ev["end"] = s, e
    elif end:
        ev["end"] = _time_body(end, None, 0)[0] if not _is_date_only(end) else {"date": end}
    attendees = ev.get("attendees", [])
    if add_attendees:
        existing = {a.get("email", "").lower() for a in attendees}
        attendees += [{"email": a} for a in add_attendees if a.lower() not in existing]
    if remove_attendees:
        rm = {a.lower() for a in remove_attendees}
        attendees = [a for a in attendees if a.get("email", "").lower() not in rm]
    if add_attendees or remove_attendees:
        ev["attendees"] = attendees
    notify = "all" if ev.get("attendees") else "none"
    updated = svc.events().update(calendarId="primary", eventId=event_id, body=ev, sendUpdates=notify).execute()
    return {"updated": True, **_fmt_event(updated)}


def delete_event(event_id: str, notify_attendees: bool = True) -> dict:
    """Delete / cancel a calendar event.

    Args:
        event_id: Event id from list_events.
        notify_attendees: Email cancellation notices to attendees.
    """
    _svc("calendar", "v3").events().delete(
        calendarId="primary", eventId=event_id, sendUpdates="all" if notify_attendees else "none"
    ).execute()
    return {"deleted": True, "event_id": event_id}


def find_free_slots(
    day: Optional[str] = None,
    duration_minutes: int = 30,
    day_start: Optional[str] = None,
    day_end: Optional[str] = None,
) -> dict:
    """Find free time on a given day within working hours. Use before proposing meeting times or planning the day.

    Args:
        day: Day as YYYY-MM-DD (defaults to today).
        duration_minutes: Minimum length of a free slot.
        day_start: Start of working hours "HH:MM" (defaults to the user's setting).
        day_end: End of working hours "HH:MM" (defaults to the user's setting).
    """
    settings = db.get_settings()
    d = date.fromisoformat(day) if day else config.now().date()
    ws = datetime.strptime(day_start or settings["work_start"], "%H:%M").time()
    we = datetime.strptime(day_end or settings["work_end"], "%H:%M").time()
    tz = config.local_tz()
    win_start = datetime.combine(d, ws, tzinfo=tz)
    win_end = datetime.combine(d, we, tzinfo=tz)
    now = config.now()
    if win_start < now < win_end:
        # round up to next quarter hour
        mins = (now.minute // 15 + 1) * 15
        win_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=mins)
    if win_start >= win_end:
        return {"date": d.isoformat(), "free_slots": [], "note": "Working hours are over for that day."}

    fb = _svc("calendar", "v3").freebusy().query(
        body={"timeMin": win_start.isoformat(), "timeMax": win_end.isoformat(), "items": [{"id": "primary"}]}
    ).execute()
    busy = sorted(
        (_parse_dt(b["start"]).astimezone(tz), _parse_dt(b["end"]).astimezone(tz))
        for b in fb["calendars"]["primary"].get("busy", [])
    )
    slots, cursor = [], win_start
    for b_start, b_end in busy:
        if b_start > cursor and (b_start - cursor) >= timedelta(minutes=duration_minutes):
            slots.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if win_end > cursor and (win_end - cursor) >= timedelta(minutes=duration_minutes):
        slots.append((cursor, win_end))
    return {
        "date": d.isoformat(),
        "working_hours": f"{ws:%H:%M}-{we:%H:%M}",
        "free_slots": [
            {"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"), "minutes": int((e - s).total_seconds() // 60)}
            for s, e in slots
        ],
        "busy": [{"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M")} for s, e in busy],
    }


# ── gmail helpers ─────────────────────────────────────────────
def _headers(msg: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def _body_text(payload: dict) -> str:
    plain, rich = [], []

    def walk(p: dict) -> None:
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mt == "text/plain":
            plain.append(_decode(data))
        elif data and mt == "text/html":
            rich.append(_decode(data))
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if rich:
        return _html_to_text("\n".join(rich))
    return ""


def _summary(msg: dict) -> dict:
    h = _headers(msg)
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId"),
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "subject": h.get("subject", "(no subject)"),
        "date": h.get("date", ""),
        "snippet": html.unescape(msg.get("snippet", "")),
        "unread": "UNREAD" in msg.get("labelIds", []),
        "important": "IMPORTANT" in msg.get("labelIds", []),
    }


def _my_address() -> str:
    return _svc("gmail", "v1").users().getProfile(userId="me").execute().get("emailAddress", "")


def _send_raw(msg: EmailMessage, thread_id: Optional[str] = None, draft: bool = False) -> dict:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body: dict[str, Any] = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    users = _svc("gmail", "v1").users()
    if draft:
        r = users.drafts().create(userId="me", body={"message": body}).execute()
        return {"draft_created": True, "draft_id": r.get("id")}
    r = users.messages().send(userId="me", body=body).execute()
    return {"sent": True, "message_id": r.get("id")}


# ── gmail tools ───────────────────────────────────────────────
def list_emails(query: str = "in:inbox is:unread", max_results: int = 10) -> list[dict]:
    """Search / list emails in Gmail. Returns sender, subject, date, snippet and id (no full body).

    Args:
        query: Gmail search syntax, e.g. "in:inbox is:unread", "from:alice newer_than:7d", "subject:invoice",
            "is:important newer_than:2d". Use "in:inbox" for recent inbox mail.
        max_results: How many emails (max 50).
    """
    users = _svc("gmail", "v1").users()
    ids = users.messages().list(userId="me", q=query, maxResults=max(1, min(int(max_results), 50))).execute()
    out = []
    for m in ids.get("messages", []):
        msg = users.messages().get(
            userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "To", "Subject", "Date"]
        ).execute()
        out.append(_summary(msg))
    return out


def read_email(message_id: str, mark_as_read: bool = False) -> dict:
    """Read the full content of one email (to summarise it or prepare a reply).

    Args:
        message_id: Email id from list_emails.
        mark_as_read: Also mark it as read.
    """
    users = _svc("gmail", "v1").users()
    msg = users.messages().get(userId="me", id=message_id, format="full").execute()
    out = _summary(msg)
    h = _headers(msg)
    if h.get("cc"):
        out["cc"] = h["cc"]
    body = _body_text(msg.get("payload", {}))
    out["body"] = body[:12000] + ("\n…[truncated]" if len(body) > 12000 else "")
    attachments = []

    def walk(p: dict) -> None:
        if p.get("filename"):
            attachments.append(p["filename"])
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(msg.get("payload", {}))
    if attachments:
        out["attachments"] = attachments
    if mark_as_read and out["unread"]:
        users.messages().modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()
    return out


def send_email(
    to: list[str],
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
) -> dict:
    """Send a NEW email from the user's Gmail account (plain text). Sign off with the user's name.

    Args:
        to: Recipient email addresses.
        subject: Subject line.
        body: Plain-text body.
        cc: CC addresses.
        bcc: BCC addresses.
    """
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg.set_content(body)
    return _send_raw(msg)


def _build_reply(message_id: str, body: str, reply_all: bool) -> tuple[EmailMessage, str]:
    users = _svc("gmail", "v1").users()
    orig = users.messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID", "References", "Reply-To"],
    ).execute()
    h = _headers(orig)
    me = _my_address().lower()
    sender = h.get("reply-to") or h.get("from", "")
    to_addrs = [parseaddr(sender)[1]]
    cc_addrs: list[str] = []
    if reply_all:
        others = getaddresses([h.get("to", ""), h.get("cc", "")])
        cc_addrs = [a for _, a in others if a and a.lower() != me and a not in to_addrs]
    subj = h.get("subject", "")
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    msg = EmailMessage()
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subj
    if h.get("message-id"):
        msg["In-Reply-To"] = h["message-id"]
        msg["References"] = (h.get("references", "") + " " + h["message-id"]).strip()
    msg.set_content(body)
    return msg, orig.get("threadId")


def reply_to_email(message_id: str, body: str, reply_all: bool = False) -> dict:
    """Reply to an email in the same thread.

    Args:
        message_id: Id of the email being answered.
        body: Plain-text reply body (no need to quote the original).
        reply_all: Reply to everyone on the email instead of only the sender.
    """
    msg, thread_id = _build_reply(message_id, body, reply_all)
    return _send_raw(msg, thread_id)


def create_draft(
    body: str,
    to: Optional[list[str]] = None,
    subject: str = "",
    reply_to_message_id: Optional[str] = None,
    reply_all: bool = False,
) -> dict:
    """Save an email as a Gmail draft (not sent). Use when the user wants to review before sending.
    For a reply draft pass reply_to_message_id instead of to/subject.

    Args:
        body: Plain-text body.
        to: Recipients (new email only).
        subject: Subject (new email only).
        reply_to_message_id: Make it a reply draft to this email id.
        reply_all: For reply drafts, include everyone.
    """
    if reply_to_message_id:
        msg, thread_id = _build_reply(reply_to_message_id, body, reply_all)
        return _send_raw(msg, thread_id, draft=True)
    msg = EmailMessage()
    msg["To"] = ", ".join(to or [])
    msg["Subject"] = subject
    msg.set_content(body)
    return _send_raw(msg, draft=True)


def mark_emails_read(message_ids: list[str]) -> dict:
    """Mark one or more emails as read.

    Args:
        message_ids: Email ids.
    """
    _svc("gmail", "v1").users().messages().batchModify(
        userId="me", body={"ids": message_ids, "removeLabelIds": ["UNREAD"]}
    ).execute()
    return {"marked_read": len(message_ids)}


def archive_emails(message_ids: list[str]) -> dict:
    """Archive emails (remove from inbox, keep in All Mail).

    Args:
        message_ids: Email ids.
    """
    _svc("gmail", "v1").users().messages().batchModify(
        userId="me", body={"ids": message_ids, "removeLabelIds": ["INBOX"]}
    ).execute()
    return {"archived": len(message_ids)}


def unread_count() -> int:
    r = _svc("gmail", "v1").users().labels().get(userId="me", id="INBOX").execute()
    return int(r.get("messagesUnread", 0))


if __name__ == "__main__":
    print("Opening Google sign-in in your browser…")
    run_auth_flow()
    print("✓ Google connected. Token saved to", config.GOOGLE_TOKEN)
