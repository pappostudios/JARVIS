"""Google Calendar + Gmail integration (OAuth, runs against *your* Google account).

Public functions (without leading underscore) are exposed to Gemini as tools.
Run `python -m jarvis.google_services` once to sign in from the desktop.
"""
from __future__ import annotations

import base64
import html
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any, Optional

from . import config, db

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",  # read, label, draft and send
]


class GoogleNotConnected(RuntimeError):
    pass


# ── accounts ──────────────────────────────────────────────────
# Several Google accounts can be connected (e.g. personal + work). Each has its own token file in
# data/google_accounts/<email>.json and an optional label. One account is the default for creating things.
ACCOUNTS_DIR = config.DATA_DIR / "google_accounts"
ACCOUNTS_META = config.DATA_DIR / "google_accounts.json"
ID_SEP = "~"  # composite ids "<email>~<google id>" tell JARVIS which account an email/event belongs to

_auth_state: dict[str, Any] = {"running": False, "error": None, "url": None, "attempt": 0, "added": None}
_creds_lock = threading.Lock()
_meta_lock = threading.RLock()


def _load_meta() -> dict:
    import json

    with _meta_lock:
        if ACCOUNTS_META.exists():
            try:
                return json.loads(ACCOUNTS_META.read_text())
            except Exception:  # noqa: BLE001
                pass
        return {"accounts": [], "default": None}


def _save_meta(meta: dict) -> None:
    import json

    with _meta_lock:
        ACCOUNTS_META.write_text(json.dumps(meta, indent=2))


def _token_path(email: str) -> Path:
    return ACCOUNTS_DIR / f"{email.lower()}.json"


def _migrate_legacy() -> None:
    """Older versions kept one account in data/token.json — move it into the accounts folder."""
    legacy = config.GOOGLE_TOKEN
    if not legacy.exists() or _load_meta()["accounts"]:
        return
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(legacy), SCOPES)
        if not creds.valid and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request

            creds.refresh(Request())
        email = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(
            userId="me").execute()["emailAddress"]
        _store_account(email, creds.to_json())
        legacy.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        print("[google] could not migrate the old sign-in:", e)


def _store_account(email: str, token_json: str) -> None:
    ACCOUNTS_DIR.mkdir(exist_ok=True)
    _token_path(email).write_text(token_json)
    with _meta_lock:
        meta = _load_meta()
        if not any(a["email"].lower() == email.lower() for a in meta["accounts"]):
            label = "personal" if not meta["accounts"] and email.lower().endswith(("@gmail.com", "@googlemail.com")) \
                else ("work" if meta["accounts"] and not email.lower().endswith("@gmail.com") else "")
            meta["accounts"].append({"email": email, "label": label})
        if not meta.get("default"):
            meta["default"] = email
        _save_meta(meta)


def accounts() -> list[dict]:
    """Connected accounts, default first: [{email, label, default}]."""
    _migrate_legacy()
    meta = _load_meta()
    out = [{"email": a["email"], "label": a.get("label") or "", "default": a["email"] == meta.get("default")}
           for a in meta["accounts"] if _token_path(a["email"]).exists()]
    out.sort(key=lambda a: not a["default"])
    return out


def _multi() -> bool:
    return len(accounts()) > 1


def _name(email: str) -> str:
    for a in accounts():
        if a["email"] == email:
            return a["label"] or a["email"]
    return email


def _resolve(account: Optional[str]) -> str:
    """Account email for an email address, a label ("work"), part of either, or None (= default)."""
    accs = accounts()
    if not accs:
        raise GoogleNotConnected(
            "Google account is not connected yet. Open Settings in the dashboard and press 'Sign in with Google'.")
    if not account or account.strip().lower() in ("default", "primary", "main"):
        return accs[0]["email"]
    q = account.strip().lower()
    for a in accs:
        if q in (a["email"].lower(), (a["label"] or "").lower()):
            return a["email"]
    for a in accs:
        if q in a["email"].lower() or (a["label"] and q in a["label"].lower()):
            return a["email"]
    raise ValueError(f"No connected Google account matches '{account}'. Connected: "
                     + ", ".join(f"{a['label'] or a['email']} ({a['email']})" for a in accs))


def _targets(account: Optional[str]) -> list[str]:
    """For reading: every account unless one is named."""
    if not account or account.strip().lower() in ("all", "both", "every", "any"):
        accs = accounts()
        if not accs:
            _resolve(None)  # raises GoogleNotConnected
        return [a["email"] for a in accs]
    return [_resolve(account)]


def _cid(email: str, raw: str) -> str:
    return f"{email}{ID_SEP}{raw}" if _multi() else raw


def _split(item_id: str, account: Optional[str] = None) -> tuple[str, str]:
    if ID_SEP in item_id:
        email, raw = item_id.split(ID_SEP, 1)
        return _resolve(email), raw
    return _resolve(account), item_id


def _creds(email: Optional[str] = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    email = email or _resolve(None)
    path = _token_path(email)
    with _creds_lock:
        if not path.exists():
            raise GoogleNotConnected(f"{email} is not connected. Reconnect it in Settings → Google accounts.")
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                path.write_text(creds.to_json())
            else:
                raise GoogleNotConnected(f"Google sign-in for {email} expired. Reconnect it in Settings.")
        return creds


def _svc(name: str, version: str, email: Optional[str] = None):
    from googleapiclient.discovery import build

    # A fresh client per call: httplib2 is not thread-safe and the dashboard + agent run concurrently.
    return build(name, version, credentials=_creds(email), cache_discovery=False)


def google_status() -> dict:
    accs = []
    for a in accounts():
        ok, err = True, None
        try:
            _svc("gmail", "v1", a["email"]).users().getProfile(userId="me").execute()
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)[:200]
        accs.append({**a, "ok": ok, "error": err})
    default = next((a for a in accs if a["default"]), None)
    return {
        "connected": any(a["ok"] for a in accs),
        "email": default["email"] if default else None,
        "accounts": accs,
        "client_secret_present": config.GOOGLE_CLIENT_SECRET.exists(),
        "auth_running": _auth_state["running"],
        "auth_url": _auth_state["url"] if _auth_state["running"] else None,
        "added": _auth_state.get("added"),
        "error": _auth_state["error"],
    }


def update_account(email: str, label: Optional[str] = None, make_default: bool = False) -> None:
    with _meta_lock:
        meta = _load_meta()
        for a in meta["accounts"]:
            if a["email"].lower() == email.lower():
                if label is not None:
                    a["label"] = label.strip()[:30]
                if make_default:
                    meta["default"] = a["email"]
        _save_meta(meta)


_SUCCESS_PAGE = """<!doctype html><meta charset=utf-8><title>JARVIS</title>
<body style="background:#03070d;color:#dcefff;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><h2 style="color:#46d9ff;letter-spacing:.2em">J.A.R.V.I.S.</h2>
<p>%s</p><p style="color:#7f97ad">You can close this tab and return to JARVIS.</p></div>"""


def _friendly_oauth_error(code: str) -> str:
    if code == "access_denied":
        return ("Google refused the sign-in (access_denied). Either you pressed Cancel, or your Gmail address is "
                "not on the Test users list. Fix: Google Cloud → Google Auth Platform → Audience → Test users → "
                "Add users → your Gmail → Save. Then press 'Sign in with Google' again.")
    return f"Google returned an error: {code}. Press 'Sign in with Google' to try again."


def _granted_scopes(token: dict) -> set[str]:
    """Scopes Google actually granted. oauthlib gives a list; raw responses give a space-separated string."""
    sc = token.get("scope")
    if not sc:
        return set(SCOPES)  # Google omitted it: everything requested was granted
    if isinstance(sc, str):
        return set(sc.split())
    return {str(x) for x in sc}


def run_auth_flow(open_browser: bool = True, attempt: int | None = None) -> None:
    """OAuth sign-in on THIS computer: opens Google in the browser, waits for the redirect back.
    A newer attempt (pressing the button again) silently supersedes this one."""
    import http.server
    import os
    import webbrowser
    from urllib.parse import parse_qs, urlparse

    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.GOOGLE_CLIENT_SECRET.exists():
        raise FileNotFoundError("Upload your Google credentials file first (Settings → Google account).")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 5  # don't let an idle browser pre-connection block the wait loop

        def do_GET(self):  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs or "error" in qs:
                result["path"] = self.path
                ok = "code" in qs
                msg = "Google account connected." if ok else _friendly_oauth_error(qs["error"][0])
                body = (_SUCCESS_PAGE % msg).encode()
                self.send_response(200)
            else:
                body, _ = b"", self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the log quiet
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(config.GOOGLE_CLIENT_SECRET), SCOPES, redirect_uri=f"http://127.0.0.1:{server.server_address[1]}/"
        )
        url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        if attempt is not None:
            _auth_state["url"] = url
        print("Google sign-in URL:", url)
        if open_browser:
            webbrowser.open(url)
        deadline = time.time() + 15 * 60
        while "path" not in result:
            if attempt is not None and attempt != _auth_state["attempt"]:
                return  # superseded by a new attempt or cancelled
            if time.time() > deadline:
                raise TimeoutError("Google sign-in timed out. Press 'Sign in with Google' to try again.")
            server.handle_request()
    finally:
        server.server_close()

    qs = parse_qs(urlparse(result["path"]).query)
    if "error" in qs:
        raise PermissionError(_friendly_oauth_error(qs["error"][0]))
    flow.fetch_token(code=qs["code"][0])
    missing = [s_ for s_ in SCOPES if s_ not in _granted_scopes(flow.oauth2session.token)]
    if missing:
        raise PermissionError("Google sign-in worked, but not every permission box was ticked. Press 'Sign in with "
                              "Google' again and tick ALL the boxes (Calendar and Gmail) so JARVIS can help you.")
    from googleapiclient.discovery import build

    email = build("gmail", "v1", credentials=flow.credentials, cache_discovery=False).users().getProfile(
        userId="me").execute()["emailAddress"]
    _store_account(email, flow.credentials.to_json())
    _auth_state["added"] = email


def start_auth_in_background() -> dict:
    """Start (or restart) Google sign-in. Pressing the button again always works."""
    _auth_state["attempt"] += 1
    attempt = _auth_state["attempt"]
    _auth_state.update(running=True, error=None, url=None, added=None)  # set before the thread starts so status polls see it

    def _run():
        try:
            run_auth_flow(attempt=attempt)
        except Exception as e:  # noqa: BLE001
            if attempt == _auth_state["attempt"]:
                _auth_state["error"] = str(e)
        finally:
            if attempt == _auth_state["attempt"]:
                _auth_state.update(running=False, url=None)

    threading.Thread(target=_run, daemon=True, name="google-signin").start()
    return {"started": True, "message": "Opening Google sign-in in your web browser…"}


def cancel_auth() -> None:
    _auth_state["attempt"] += 1
    _auth_state.update(running=False, url=None, error=None)


def disconnect(email: Optional[str] = None) -> None:
    """Forget one account (or all of them)."""
    with _meta_lock:
        meta = _load_meta()
        targets = [a["email"] for a in meta["accounts"] if not email or a["email"].lower() == email.lower()]
        for e in targets:
            _token_path(e).unlink(missing_ok=True)
        meta["accounts"] = [a for a in meta["accounts"] if a["email"] not in targets]
        if meta.get("default") in targets:
            meta["default"] = meta["accounts"][0]["email"] if meta["accounts"] else None
        _save_meta(meta)
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
    account: Optional[str] = None,
) -> list[dict]:
    """Read the user's Google Calendar(s). Returns events with id, title, start, end, attendees, location and
    (when several accounts are connected) which account's calendar each event is on.

    Args:
        start_date: First day to include (YYYY-MM-DD) or an ISO datetime. Defaults to today.
        end_date: Last day to include (YYYY-MM-DD, inclusive) or an ISO datetime. Defaults to start_date.
        search: Optional free-text search (title, attendees, description).
        max_results: Maximum number of events.
        account: Which Google account: "personal", "work", an email address, or omit for ALL connected accounts.
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
    out, failures = [], []
    targets = _targets(account)
    for email in targets:
        try:
            items = _svc("calendar", "v3", email).events().list(**params).execute().get("items", [])
        except Exception as e:  # noqa: BLE001
            if len(targets) == 1:
                raise
            failures.append(f"{_name(email)}: {e}")
            continue
        for e in items:
            if e.get("status") != "cancelled":
                ev = _fmt_event(e)
                ev["id"] = _cid(email, ev["id"])
                if _multi():
                    ev["account"] = _name(email)
                out.append(ev)
    if failures and not out and len(failures) == len(targets):
        raise GoogleNotConnected("Couldn't read any calendar: " + "; ".join(failures))
    out.sort(key=lambda ev: _sort_key(ev.get("start")))
    out = out[: max(1, int(max_results))]
    if failures:
        out.append({"note": "Some calendars could not be read", "errors": failures})
    return out


def _sort_key(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return _parse_dt(value if "T" in value else value + "T00:00").astimezone(config.local_tz()).isoformat()
    except ValueError:
        return value


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
    account: Optional[str] = None,
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
        account: Which Google account to use: "personal", "work" or an email address. Omit for the default account.
    """
    email = _resolve(account)
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
    created = _svc("calendar", "v3", email).events().insert(**kwargs).execute()
    ev = _fmt_event(created)
    ev["id"] = _cid(email, ev["id"])
    return {"created": True, **ev, "account": _name(email), "link": created.get("htmlLink")}


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
    email, event_id = _split(event_id)
    svc = _svc("calendar", "v3", email)
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
    out = _fmt_event(updated)
    out["id"] = _cid(email, out["id"])
    return {"updated": True, **out}


def delete_event(event_id: str, notify_attendees: bool = True) -> dict:
    """Delete / cancel a calendar event.

    Args:
        event_id: Event id from list_events.
        notify_attendees: Email cancellation notices to attendees.
    """
    email, raw = _split(event_id)
    _svc("calendar", "v3", email).events().delete(
        calendarId="primary", eventId=raw, sendUpdates="all" if notify_attendees else "none"
    ).execute()
    return {"deleted": True, "event_id": event_id}


def find_free_slots(
    day: Optional[str] = None,
    duration_minutes: int = 30,
    day_start: Optional[str] = None,
    day_end: Optional[str] = None,
    account: Optional[str] = None,
) -> dict:
    """Find free time on a given day within working hours. Use before proposing meeting times or planning the day.
    Checks every connected calendar (personal and work) unless one account is named.

    Args:
        day: Day as YYYY-MM-DD (defaults to today).
        duration_minutes: Minimum length of a free slot.
        day_start: Start of working hours "HH:MM" (defaults to the user's setting).
        day_end: End of working hours "HH:MM" (defaults to the user's setting).
        account: Which Google account: "personal", "work", an email address, or omit for ALL connected accounts.
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

    busy_raw = []
    for email in _targets(account):
        fb = _svc("calendar", "v3", email).freebusy().query(
            body={"timeMin": win_start.isoformat(), "timeMax": win_end.isoformat(), "items": [{"id": "primary"}]}
        ).execute()
        busy_raw += fb["calendars"]["primary"].get("busy", [])
    busy = sorted((_parse_dt(b["start"]).astimezone(tz), _parse_dt(b["end"]).astimezone(tz)) for b in busy_raw)
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
        "received_ms": int(msg.get("internalDate") or 0),
    }


def _send_raw(msg: EmailMessage, email: str, thread_id: Optional[str] = None, draft: bool = False) -> dict:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body: dict[str, Any] = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    users = _svc("gmail", "v1", email).users()
    if draft:
        r = users.drafts().create(userId="me", body={"message": body}).execute()
        return {"draft_created": True, "draft_id": r.get("id"), "from_account": email}
    r = users.messages().send(userId="me", body=body).execute()
    return {"sent": True, "message_id": _cid(email, r.get("id")), "from_account": email}


# ── gmail tools ───────────────────────────────────────────────
def list_emails(query: str = "in:inbox is:unread", max_results: int = 10, account: Optional[str] = None) -> list[dict]:
    """Search / list emails in Gmail (all connected inboxes unless one account is named). Returns sender, subject,
    date, snippet, id and — with several accounts — which inbox each email is in. No full body.

    Args:
        query: Gmail search syntax, e.g. "in:inbox is:unread", "from:alice newer_than:7d", "subject:invoice",
            "is:important newer_than:2d". Use "in:inbox" for recent inbox mail.
        max_results: How many emails (max 50).
        account: Which Google account: "personal", "work", an email address, or omit for ALL connected accounts.
    """
    n = max(1, min(int(max_results), 50))
    out, failures = [], []
    targets = _targets(account)
    for email in targets:
        users = _svc("gmail", "v1", email).users()
        try:
            ids = users.messages().list(userId="me", q=query, maxResults=n).execute()
        except Exception as e:  # noqa: BLE001
            if len(targets) == 1:
                raise
            failures.append(f"{_name(email)}: {e}")
            continue
        for m in ids.get("messages", []):
            msg = users.messages().get(
                userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "To", "Subject", "Date"]
            ).execute()
            item = _summary(msg)
            item["id"] = _cid(email, item["id"])
            if _multi():
                item["account"] = _name(email)
            out.append(item)
    if failures and not out and len(failures) == len(targets):
        raise GoogleNotConnected("Couldn't read any inbox: " + "; ".join(failures))
    out.sort(key=lambda m: -m["received_ms"])
    out = out[:n]
    if failures:
        out.append({"note": "Some inboxes could not be read", "errors": failures})
    return out


def read_email(message_id: str, mark_as_read: bool = False) -> dict:
    """Read the full content of one email (to summarise it or prepare a reply).

    Args:
        message_id: Email id from list_emails.
        mark_as_read: Also mark it as read.
    """
    email, raw_id = _split(message_id)
    users = _svc("gmail", "v1", email).users()
    msg = users.messages().get(userId="me", id=raw_id, format="full").execute()
    out = _summary(msg)
    out["id"] = message_id
    if _multi():
        out["account"] = _name(email)
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
        users.messages().modify(userId="me", id=raw_id, body={"removeLabelIds": ["UNREAD"]}).execute()
    return out


def send_email(
    to: list[str],
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    account: Optional[str] = None,
) -> dict:
    """Send a NEW email from the user's Gmail (plain text). Sign off with the user's name. With several accounts,
    send work matters from the work account and personal matters from the personal one.

    Args:
        to: Recipient email addresses.
        subject: Subject line.
        body: Plain-text body.
        cc: CC addresses.
        bcc: BCC addresses.
        account: Which Google account to use: "personal", "work" or an email address. Omit for the default account.
    """
    email = _resolve(account)
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg.set_content(body)
    return _send_raw(msg, email)


def _build_reply(message_id: str, body: str, reply_all: bool) -> tuple[EmailMessage, str, str]:
    email, message_id = _split(message_id)
    users = _svc("gmail", "v1", email).users()
    orig = users.messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID", "References", "Reply-To"],
    ).execute()
    h = _headers(orig)
    me = email.lower()
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
    return msg, orig.get("threadId"), email


def reply_to_email(message_id: str, body: str, reply_all: bool = False) -> dict:
    """Reply to an email in the same thread.

    Args:
        message_id: Id of the email being answered.
        body: Plain-text reply body (no need to quote the original).
        reply_all: Reply to everyone on the email instead of only the sender.
    """
    msg, thread_id, email = _build_reply(message_id, body, reply_all)
    return _send_raw(msg, email, thread_id)  # replies always go out from the account that received the email


def create_draft(
    body: str,
    to: Optional[list[str]] = None,
    subject: str = "",
    reply_to_message_id: Optional[str] = None,
    reply_all: bool = False,
    account: Optional[str] = None,
) -> dict:
    """Save an email as a Gmail draft (not sent). Use when the user wants to review before sending.
    For a reply draft pass reply_to_message_id instead of to/subject.

    Args:
        body: Plain-text body.
        to: Recipients (new email only).
        subject: Subject (new email only).
        reply_to_message_id: Make it a reply draft to this email id.
        reply_all: For reply drafts, include everyone.
        account: Which Google account to use: "personal", "work" or an email address. Omit for the default account.
    """
    if reply_to_message_id:
        msg, thread_id, email = _build_reply(reply_to_message_id, body, reply_all)
        return _send_raw(msg, email, thread_id, draft=True)
    msg = EmailMessage()
    msg["To"] = ", ".join(to or [])
    msg["Subject"] = subject
    msg.set_content(body)
    return _send_raw(msg, _resolve(account), draft=True)


def mark_emails_read(message_ids: list[str]) -> dict:
    """Mark one or more emails as read.

    Args:
        message_ids: Email ids.
    """
    for email, ids in _group_ids(message_ids).items():
        _svc("gmail", "v1", email).users().messages().batchModify(
            userId="me", body={"ids": ids, "removeLabelIds": ["UNREAD"]}).execute()
    return {"marked_read": len(message_ids)}


def _group_ids(message_ids: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for mid in message_ids:
        email, raw = _split(mid)
        groups.setdefault(email, []).append(raw)
    return groups


def archive_emails(message_ids: list[str]) -> dict:
    """Archive emails (remove from inbox, keep in All Mail).

    Args:
        message_ids: Email ids.
    """
    for email, ids in _group_ids(message_ids).items():
        _svc("gmail", "v1", email).users().messages().batchModify(
            userId="me", body={"ids": ids, "removeLabelIds": ["INBOX"]}).execute()
    return {"archived": len(message_ids)}


def unread_count() -> int:
    total = 0
    for email in _targets(None):
        try:
            r = _svc("gmail", "v1", email).users().labels().get(userId="me", id="INBOX").execute()
            total += int(r.get("messagesUnread", 0))
        except Exception:  # noqa: BLE001
            pass
    return total


if __name__ == "__main__":
    print("Opening Google sign-in in your browser…")
    run_auth_flow(attempt=None)
    print("✓ Google connected:", ", ".join(a["email"] for a in accounts()))
