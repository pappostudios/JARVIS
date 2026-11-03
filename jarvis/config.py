"""Central configuration: .env values + paths + user-editable settings."""
from __future__ import annotations

import os
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ACCESS_CODE = os.getenv("JARVIS_ACCESS_CODE", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()
GEMINI_FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-flash-lite-latest").strip()
GEMINI_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "auto").strip()
HOST = os.getenv("HOST", "0.0.0.0").strip()
PORT = int(os.getenv("PORT", "8765"))
SSL_CERTFILE = os.getenv("SSL_CERTFILE", "").strip() or None
SSL_KEYFILE = os.getenv("SSL_KEYFILE", "").strip() or None

DB_PATH = DATA_DIR / "jarvis.db"
GOOGLE_CLIENT_SECRET = DATA_DIR / "credentials.json"
GOOGLE_TOKEN = DATA_DIR / "token.json"

_tz_name = os.getenv("TIMEZONE", "").strip()


def local_tz() -> tzinfo:
    if _tz_name:
        try:
            return ZoneInfo(_tz_name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def tz_name() -> str:
    """IANA name if configured, otherwise best-effort label."""
    if _tz_name:
        return _tz_name
    return datetime.now().astimezone().tzname() or "local"


def now() -> datetime:
    return datetime.now(local_tz())


# Defaults for settings editable from the dashboard (stored in SQLite).
DEFAULT_SETTINGS = {
    "user_name": "",              # e.g. "Tony"
    "address_as": "sir",          # how JARVIS addresses you: sir / ma'am / your name
    "voice_engine": "gemini",     # gemini | edge | browser
    "gemini_voice": "Charon",     # Gemini prebuilt voice
    "edge_voice": "en-GB-RyanNeural",
    "auto_speak": "true",
    "live_interrupt": "false",    # live calls: let your voice interrupt JARVIS (best with headphones)
    "work_start": "09:00",
    "work_end": "18:00",
    "require_confirmation": "true",  # ask before sending email / inviting people / deleting events
    "model": "auto",              # "auto" or a specific Gemini model id
    "daily_request_limit": "0",   # max Gemini requests per day (0 = no limit); Live calls not counted
    "thinking": "fast",           # fast | balanced | deep — how long Gemini reasons before answering
    "about_me": "",               # free text: role, company, preferences
}
