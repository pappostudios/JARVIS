"""Routines: your own trigger words that run a list of actions instantly — e.g. say "home" → minimize all windows.

A routine runs directly on the PC, WITHOUT asking Gemini (instant, no requests used), when a message is exactly one
of its trigger phrases ("home", "Jarvis, home", "home please"). In Live calls JARVIS runs it via run_routine.
Also here: end-call phrases ("shut up", "goodbye JARVIS") that end a Live call.
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

from . import db

db.execute("""CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    triggers TEXT NOT NULL,
    actions TEXT NOT NULL,
    created_at TEXT NOT NULL)""")

ACTION_HELP = [
    "minimize all", "minimize <app or window>", "close <app or window>", "close all",
    "maximize <app or window>", "switch to <app or window>",
    "open app <name>", "open website <address>", "open folder <name>", "open file <name>",
    "play/pause music", "next track", "previous track", "volume up", "volume down", "mute", "volume <0-100>",
    "say <text>", "wait <seconds>",
]

_FILLER = re.compile(r"^(hey |hi |ok |okay )?(jarvis[,]? )?|( please| now| jarvis)+$")


def _norm(text: str) -> str:
    t = re.sub(r"[^\w\s']", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    for _ in range(2):
        t = _FILLER.sub("", t).strip()
    return t


# ── action language ───────────────────────────────────────────
def _media(action: str, pct: int | None = None) -> tuple[str, dict]:
    return "media_control", ({"action": action} if pct is None else {"action": "set_volume", "volume_percent": pct})


_RULES: list[tuple[str, Callable[[re.Match], tuple[str, dict]]]] = [
    (r"^minimi[sz]e (all|everything|all windows|all tabs|every window)$", lambda m: ("minimize_windows", {"target": "all"})),
    (r"^(show (the )?desktop)$", lambda m: ("minimize_windows", {"target": "all"})),
    (r"^minimi[sz]e (.+)$", lambda m: ("minimize_windows", {"target": m.group(1)})),
    (r"^close (all|everything|all windows)$", lambda m: ("close_windows", {"target": "all"})),
    (r"^close (.+)$", lambda m: ("close_windows", {"target": m.group(1)})),
    (r"^maximi[sz]e (.+)$", lambda m: ("maximize_window", {"target": m.group(1)})),
    (r"^(switch to|focus|show|bring up) (.+)$", lambda m: ("focus_window", {"target": m.group(2)})),
    (r"^open app (.+)$", lambda m: ("open_app", {"name": m.group(1)})),
    (r"^(open|launch|start) (website|site|url) (.+)$", lambda m: ("open_website", {"url": m.group(3)})),
    (r"^open folder (.+)$", lambda m: ("open_folder", {"name_or_path": m.group(1)})),
    (r"^open file (.+)$", lambda m: ("open_file", {"name_or_path": m.group(1)})),
    (r"^(play|pause|play ?/ ?pause|resume)( the)?( music)?$", lambda m: _media("play_pause")),
    (r"^play/pause music$", lambda m: _media("play_pause")),
    (r"^(next|skip)( track| song)?$", lambda m: _media("next")),
    (r"^previous( track| song)?$", lambda m: _media("previous")),
    (r"^(stop)( music)?$", lambda m: _media("stop")),
    (r"^volume up$", lambda m: _media("volume_up")),
    (r"^volume down$", lambda m: _media("volume_down")),
    (r"^(mute|unmute)$", lambda m: _media("mute")),
    (r"^(set )?volume( to)? (\d{1,3})%?$", lambda m: _media("set_volume", int(m.group(3)))),
    (r"^(open|launch|start) (.+)$", lambda m: ("open_app", {"name": m.group(2)})),   # "open spotify"
    (r"^say (.+)$", lambda m: ("say", {"text": m.group(1)})),
    (r"^wait (\d+(\.\d+)?) ?(s|sec|secs|seconds)?$", lambda m: ("wait", {"seconds": float(m.group(1))})),
]


def parse_action(action: str) -> tuple[str, dict]:
    a = action.strip().rstrip(".")
    low = re.sub(r"\s+", " ", a.lower())
    for pattern, build in _RULES:
        m = re.match(pattern, low)
        if m:
            tool, args = build(m)
            # keep the user's original capitalisation for names/paths/text
            orig = re.match(pattern, re.sub(r"\s+", " ", a), re.I)
            if orig and tool not in ("media_control", "wait"):
                tool, args = build(orig)
            return tool, args
    raise ValueError(f"I don't understand the routine step '{action}'. Steps can be: " + "; ".join(ACTION_HELP))


# ── storage ───────────────────────────────────────────────────
def _row(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "triggers": json.loads(r["triggers"]), "actions": json.loads(r["actions"])}


def all_routines() -> list[dict]:
    return [_row(r) for r in db.query("SELECT * FROM routines ORDER BY name")]


def create_routine(name: str, triggers: list[str], actions: list[str], old_name: str = "") -> dict:
    """Create a routine, or EDIT one: to edit, call list_routines first, then call this with the same name and the
    complete new trigger and step lists (to rename, pass the current name as old_name and the new one as name).
    A routine is trigger phrases the user will say, and steps to run on the PC instantly.
    Write each step in this simple form (one per item): "minimize all", "minimize <app>", "close <app>",
    "maximize <app>", "switch to <app>", "open app <name>", "open website <address>", "open folder <name>",
    "open file <name>", "play/pause music", "next track", "volume <0-100>", "mute", "say <text>", "wait <seconds>".
    Example: name "Home", triggers ["home", "I'm home"], actions ["minimize all", "open app Spotify", "say Welcome home"].

    Args:
        name: Short name of the routine.
        triggers: Phrases that start it (short; the whole message must be the phrase).
        actions: Steps in the form above, in order.
        old_name: When renaming an existing routine: its current name.
    """
    if not name.strip():
        raise ValueError("A routine needs a name.")
    trig = [t.strip() for t in triggers if t and t.strip()]
    if not trig:
        raise ValueError("A routine needs at least one trigger phrase.")
    steps = [a.strip() for a in actions if a and a.strip()]
    if not steps:
        raise ValueError("A routine needs at least one step.")
    for s in steps:
        parse_action(s)   # validate now, so mistakes surface while the user is still talking about it
    replacing = {name.strip().lower(), old_name.strip().lower()} - {""}
    if old_name.strip() and old_name.strip().lower() != name.strip().lower() and \
            any(r["name"].lower() == name.strip().lower() for r in all_routines()):
        raise ValueError(f"There's already a routine called '{name.strip()}'.")
    taken = {t: r["name"] for r in all_routines() if r["name"].lower() not in replacing
             for t in map(_norm, r["triggers"])}
    clash = [t for t in trig if _norm(t) in taken]
    if clash:
        raise ValueError(f"'{clash[0]}' already starts the routine '{taken[_norm(clash[0])]}'.")
    edited = any(r["name"].lower() in replacing for r in all_routines())
    for n in replacing:
        db.execute("DELETE FROM routines WHERE name = ? COLLATE NOCASE", (n,))
    db.execute("INSERT INTO routines(name, triggers, actions, created_at) VALUES(?,?,?,?)",
               (name.strip(), json.dumps(trig), json.dumps(steps), db.now_iso()))
    return {"updated" if edited else "created": name.strip(), "triggers": trig, "actions": steps}


def list_routines() -> list[dict]:
    """List the user's routines (name, trigger phrases, steps)."""
    return [{k: v for k, v in r.items() if k != "id"} for r in all_routines()]


def delete_routine(name: str) -> dict:
    """Delete a routine by name.

    Args:
        name: The routine's name.
    """
    n = db.execute("DELETE FROM routines WHERE name = ?", (name.strip(),))
    return {"deleted": name} if n else {"error": f"No routine called '{name}'."}


def find(name_or_phrase: str) -> dict | None:
    q = _norm(name_or_phrase)
    for r in all_routines():
        if q == _norm(r["name"]) or q in (_norm(t) for t in r["triggers"]):
            return r
    return None


def match(text: str) -> dict | None:
    """The routine whose trigger phrase IS this message (ignoring 'hey jarvis', 'please', punctuation)."""
    q = _norm(text)
    if not q or len(q.split()) > 6:
        return None
    for r in all_routines():
        if q in (_norm(t) for t in r["triggers"]):
            return r
    return None


def run(routine: dict) -> dict:
    """Run every step directly (no Gemini). User-made routines don't ask for approval."""
    from . import tools

    done, said, problems = [], [], []
    for step in routine["actions"]:
        try:
            tool, args = parse_action(step)
            if tool == "say":
                said.append(args["text"])
            elif tool == "wait":
                time.sleep(max(0.0, min(float(args["seconds"]), 30)))
            else:
                res = tools.call_tool(tool, args, approved=True)
                if "error" in res or "error" in (res.get("result") or {}):
                    problems.append(f"{step}: {res.get('error') or res['result'].get('error')}")
                else:
                    done.append(step)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{step}: {e}")
    return {"routine": routine["name"], "done": done, "said": said, "problems": problems}


def summary(result: dict) -> str:
    parts = []
    if result["said"]:
        parts.append(" ".join(result["said"]))
    if not result["said"]:
        parts.append(f"{result['routine']} routine done." if not result["problems"] else
                     f"{result['routine']} routine ran with problems.")
    if result["problems"]:
        parts.append("Couldn't: " + "; ".join(result["problems"]))
    return " ".join(parts)


def run_routine(name: str) -> dict:
    """Run one of the user's routines now (use when the user says its trigger phrase or asks for it by name).

    Args:
        name: The routine's name or one of its trigger phrases.
    """
    r = find(name)
    if not r:
        return {"error": f"No routine called '{name}'.", "routines": [x["name"] for x in all_routines()]}
    return run(r)


# ── ending a call by voice ────────────────────────────────────
DEFAULT_END_PHRASES = "shut up, goodbye jarvis, bye jarvis, that's all jarvis, thank you jarvis, stop listening, hang up"


def end_phrases() -> list[str]:
    raw = db.get_setting("end_phrases") or DEFAULT_END_PHRASES
    return [p for p in (_norm_end(x) for x in raw.split(",")) if p]


def _norm_end(text: str) -> str:
    t = re.sub(r"[^\w\s']", " ", text.lower())
    return re.sub(r"\s+", " ", t).strip()


def is_end_phrase(text: str) -> bool:
    """True if a SHORT utterance contains an end-call phrase ("shut up", "ok goodbye Jarvis")."""
    t = _norm_end(text)
    if not t:
        return False
    words = len(t.split())
    for p in end_phrases():
        # the phrase must open or close what was said, with at most a few extra words ("ok, goodbye Jarvis")
        if (t == p or t.endswith(" " + p) or t.startswith(p + " ")) and words <= len(p.split()) + 3:
            return True
    return False


def describe() -> str:
    rs = all_routines()
    if not rs:
        return ""
    return "User's routines (run with run_routine when they say a trigger): " + "; ".join(
        f"{r['name']} — triggers {', '.join(repr(t) for t in r['triggers'])}" for r in rs)
