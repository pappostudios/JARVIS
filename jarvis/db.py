"""SQLite storage: projects, tasks, progress log, memories, chat log, settings, pending actions."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',      -- active | paused | done
    priority TEXT NOT NULL DEFAULT 'medium',    -- low | medium | high
    deadline TEXT,                               -- YYYY-MM-DD
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    notes TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',        -- todo | in_progress | done
    priority TEXT NOT NULL DEFAULT 'medium',
    due_date TEXT,
    estimate_minutes INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS progress_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,           -- user | jarvis
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | cancelled | failed
    created_turn INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    created_at TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conn = _connect()
_conn.executescript(SCHEMA)
_conn.commit()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def query(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | list = ()) -> int:
    with tx() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid or cur.rowcount


def now_iso() -> str:
    return config.now().isoformat(timespec="seconds")


# ── settings ──────────────────────────────────────────────────
def get_settings() -> dict[str, str]:
    s = dict(config.DEFAULT_SETTINGS)
    for row in query("SELECT key, value FROM settings"):
        s[row["key"]] = row["value"]
    return s


def get_setting(key: str) -> str:
    return get_settings().get(key, "")


def set_settings(values: dict[str, Any]) -> dict[str, str]:
    with tx() as c:
        for k, v in values.items():
            if k in config.DEFAULT_SETTINGS:
                c.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, str(v)),
                )
    return get_settings()


# ── chat log ──────────────────────────────────────────────────
def log_message(role: str, text: str) -> None:
    execute("INSERT INTO messages(role, text, created_at) VALUES(?,?,?)", (role, text, now_iso()))


def recent_messages(limit: int = 40) -> list[dict[str, Any]]:
    rows = query("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
    return list(reversed(rows))


def clear_messages() -> None:
    execute("DELETE FROM messages")


# ── pending actions ───────────────────────────────────────────
def add_pending(tool: str, args: dict, summary: str, turn: int) -> int:
    return execute(
        "INSERT INTO pending_actions(tool, args, summary, created_turn, created_at) VALUES(?,?,?,?,?)",
        (tool, json.dumps(args, default=str), summary, turn, now_iso()),
    )


def list_pending() -> list[dict[str, Any]]:
    rows = query("SELECT * FROM pending_actions WHERE status='pending' ORDER BY id")
    for r in rows:
        r["args"] = json.loads(r["args"])
    return rows


def get_pending(action_id: int) -> dict[str, Any] | None:
    r = query_one("SELECT * FROM pending_actions WHERE id=?", (action_id,))
    if r:
        r["args"] = json.loads(r["args"])
    return r


def set_pending_status(action_id: int, status: str, result: Any = None) -> None:
    execute(
        "UPDATE pending_actions SET status=?, result=? WHERE id=?",
        (status, json.dumps(result, default=str) if result is not None else None, action_id),
    )
