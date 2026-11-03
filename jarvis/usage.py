"""Usage tracking: every Gemini request JARVIS makes, per day, model and purpose — so you can see how close you are
to the free tier and what the same usage would cost on the paid tier."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from . import config, db

log = logging.getLogger("jarvis.usage")

db.execute("""CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0,
    quota_errors INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0, seconds REAL NOT NULL DEFAULT 0,
    last_quota_error TEXT,
    PRIMARY KEY (day, model, kind))""")

KIND_LABELS = {"chat": "Chat (typed & voice messages)", "transcribe": "Voice → text",
               "voice": "Gemini voice (speech)", "live": "Live calls", "test": "Diagnostics test"}

# Paid-tier prices per 1M tokens (input, output, cached input) from Google's Gemini 3.8 Flash price page.
# Used only to ESTIMATE what your real usage would cost if you upgraded. Edit if prices change.
PAID_PRICES = {
    "Gemini 3.8 Flash — 2026 price": (0.75, 3.75, 0.075),
    "Gemini 3.8 Flash — from Jan 2027": (1.50, 7.50, 0.15),
}


def _today() -> str:
    return config.now().date().isoformat()


def _bump(model: str, kind: str, **inc: Any) -> None:
    try:
        model = (model or "unknown").removeprefix("models/")
        db.execute("INSERT OR IGNORE INTO usage(day, model, kind) VALUES(?,?,?)", (_today(), model, kind))
        sets = ", ".join(f"{k} = {k} + ?" for k in inc if k != "last_quota_error")
        vals = [v for k, v in inc.items() if k != "last_quota_error"]
        if "last_quota_error" in inc:
            sets += (", " if sets else "") + "last_quota_error = ?"
            vals.append(inc["last_quota_error"])
        db.execute(f"UPDATE usage SET {sets} WHERE day=? AND model=? AND kind=?", (*vals, _today(), model, kind))
    except Exception as e:  # noqa: BLE001  (tracking must never break JARVIS)
        log.warning("usage tracking failed: %r", e)


def record_ok(model: str, kind: str, response: Any = None, seconds: float = 0.0) -> None:
    um = getattr(response, "usage_metadata", None)
    inp = int(getattr(um, "prompt_token_count", 0) or 0) + int(getattr(um, "tool_use_prompt_token_count", 0) or 0)
    out = int(getattr(um, "candidates_token_count", 0) or getattr(um, "response_token_count", 0) or 0) \
        + int(getattr(um, "thoughts_token_count", 0) or 0)
    cached = int(getattr(um, "cached_content_token_count", 0) or 0)
    _bump(model, kind, requests=1, ok=1, input_tokens=inp, output_tokens=out, cached_tokens=cached, seconds=seconds)


def record_tokens(model: str, kind: str, usage_metadata: Any) -> None:
    """Token counts reported during a Live call (no extra request)."""
    if not usage_metadata:
        return
    inp = int(getattr(usage_metadata, "prompt_token_count", 0) or 0)
    out = int(getattr(usage_metadata, "response_token_count", 0) or 0) + \
        int(getattr(usage_metadata, "thoughts_token_count", 0) or 0)
    _bump(model, kind, input_tokens=inp, output_tokens=out,
          cached_tokens=int(getattr(usage_metadata, "cached_content_token_count", 0) or 0))


def record_error(model: str, kind: str, error: Exception | str) -> None:
    text = str(error)
    quota = "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower()
    if quota:
        _bump(model, kind, requests=1, quota_errors=1, last_quota_error=config.now().strftime("%H:%M"))
    else:
        _bump(model, kind, requests=1, errors=1)


class DailyLimitReached(RuntimeError):
    pass


def requests_today() -> int:
    """Gemini requests made today, excluding Live calls (billed by minutes, not requests)."""
    r = db.query_one("SELECT COALESCE(SUM(requests), 0) AS n FROM usage WHERE day=? AND kind != 'live'", (_today(),))
    return int(r["n"]) if r else 0


def daily_limit() -> int:
    try:
        return max(0, int(float(db.get_setting("daily_request_limit") or 0)))
    except ValueError:
        return 0


def check_limit() -> None:
    """Raise before a Gemini request if today's limit (Settings) has been reached."""
    limit = daily_limit()
    if limit and requests_today() >= limit:
        raise DailyLimitReached(
            f"I've reached today's limit of {limit} Gemini requests that you set, so I've paused to keep you within "
            "budget. You can raise or remove the limit in Settings → Usage limit; it resets at midnight. "
            "(Live calls still work.)")


def _cost(inp: int, out: int, cached: int, price: tuple[float, float, float]) -> float:
    p_in, p_out, p_cache = price
    return ((max(inp - cached, 0)) * p_in + cached * p_cache + out * p_out) / 1_000_000


def summary(days: int = 14) -> dict:
    today = config.now().date()
    start = (today - timedelta(days=days - 1)).isoformat()
    rows = db.query("SELECT * FROM usage WHERE day >= ? ORDER BY day", (start,))

    per_day: dict[str, dict] = {}
    for d in range(days):
        key = (today - timedelta(days=days - 1 - d)).isoformat()
        per_day[key] = {"day": key, "requests": 0, "ok": 0, "quota_errors": 0, "errors": 0,
                        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "live_minutes": 0.0}
    for r in rows:
        d = per_day.get(r["day"])
        if not d:
            continue
        for k in ("requests", "ok", "quota_errors", "errors", "input_tokens", "output_tokens", "cached_tokens"):
            d[k] += r[k]
        if r["kind"] == "live":
            d["live_minutes"] += r["seconds"] / 60

    today_rows = [r for r in rows if r["day"] == today.isoformat()]
    by_model: dict[str, dict] = {}
    for r in today_rows:
        m = by_model.setdefault(r["model"], {"model": r["model"], "requests": 0, "ok": 0, "quota_errors": 0,
                                             "errors": 0, "tokens": 0, "last_quota_error": None, "kinds": set()})
        m["requests"] += r["requests"]
        m["ok"] += r["ok"]
        m["quota_errors"] += r["quota_errors"]
        m["errors"] += r["errors"]
        m["tokens"] += r["input_tokens"] + r["output_tokens"]
        m["last_quota_error"] = r["last_quota_error"] or m["last_quota_error"]
        m["kinds"].add(KIND_LABELS.get(r["kind"], r["kind"]))
    models_today = sorted(({**m, "kinds": sorted(m["kinds"])} for m in by_model.values()),
                          key=lambda m: -m["requests"])

    active = [d for d in per_day.values() if d["requests"]]
    # Text-model tokens only: Live calls are billed by audio at the Live model's own prices.
    text_rows = [r for r in rows if r["kind"] != "live"]
    tot_in = sum(r["input_tokens"] for r in text_rows)
    tot_out = sum(r["output_tokens"] for r in text_rows)
    tot_cached = sum(r["cached_tokens"] for r in text_rows)
    live_rows = [r for r in rows if r["kind"] == "live"]
    live = {"minutes": round(sum(r["seconds"] for r in live_rows) / 60, 1),
            "tokens": sum(r["input_tokens"] + r["output_tokens"] for r in live_rows),
            "models": sorted({r["model"] for r in live_rows})}
    avg_factor = 30 / max(len(active), 1)
    estimates = [{"label": label, "per_month": round(_cost(tot_in, tot_out, tot_cached, price) * avg_factor, 2),
                  "so_far": round(_cost(tot_in, tot_out, tot_cached, price), 3)}
                 for label, price in PAID_PRICES.items()]

    quota_days = sum(1 for d in active if d["quota_errors"])
    n_days = f"{len(active)} day" + ("" if len(active) == 1 else "s")
    t = per_day[today.isoformat()]
    if not active:
        verdict = "No usage recorded yet — use JARVIS normally for a few days, then check back."
    elif quota_days == 0:
        verdict = (f"The free tier is enough so far: no quota refusals in {n_days} of use. "
                   "Upgrade only if you want the privacy of the paid tier.")
    elif quota_days <= max(1, len(active) // 4):
        verdict = (f"You hit a free-tier limit on {quota_days} of {n_days}. JARVIS switched models to "
                   "cope; upgrading would remove these interruptions.")
    else:
        verdict = (f"You hit free-tier limits on {quota_days} of {n_days} — upgrading to the paid tier "
                   "is recommended for reliable use.")
    return {"days": list(per_day.values()), "today": t, "models_today": models_today,
            "limit": daily_limit(), "requests_today": requests_today(),
            "estimates": estimates, "live": live, "active_days": len(active), "quota_days": quota_days, "verdict": verdict,
            "kinds": KIND_LABELS}
