"""One-click Gemini connection test: network → API key → each model (simple) → a full JARVIS request.
Every step has a hard time limit, so the test itself can never hang."""
from __future__ import annotations

import concurrent.futures
import os
import socket
import ssl
import time
from typing import Any, Callable

from google.genai import types

from . import config

HOST = "generativelanguage.googleapis.com"
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="diag")


def _step(name: str, fn: Callable[[], Any], limit: float) -> dict:
    t0 = time.time()
    fut = _POOL.submit(fn)
    try:
        detail = fut.result(timeout=limit)
        return {"step": name, "ok": True, "seconds": round(time.time() - t0, 1), "detail": str(detail)[:300]}
    except concurrent.futures.TimeoutError:
        return {"step": name, "ok": False, "seconds": round(time.time() - t0, 1),
                "detail": f"No answer within {limit:.0f}s (hung)"}
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace(config.GEMINI_API_KEY, "***") if config.GEMINI_API_KEY else str(e)
        return {"step": name, "ok": False, "seconds": round(time.time() - t0, 1),
                "detail": f"{type(e).__name__}: {msg}"[:400]}


def _connect(family: int) -> str:
    infos = socket.getaddrinfo(HOST, 443, family, socket.SOCK_STREAM)
    addr = infos[0][4]
    with socket.create_connection(addr[:2], timeout=8) as sock:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
            return f"{addr[0]} TLS {tls.version()}"


def run() -> dict:
    from .agent import jarvis

    steps: list[dict] = []
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    steps.append({"step": "Settings", "ok": bool(config.GEMINI_API_KEY), "seconds": 0,
                  "detail": ("API key present" if config.GEMINI_API_KEY else "GEMINI_API_KEY missing in .env")
                  + (f"; proxy {proxy}" if proxy else "; no proxy")})
    steps.append(_step("Network: IPv4 connection to Google", lambda: _connect(socket.AF_INET), 12))
    v6 = _step("Network: IPv6 connection to Google (optional)", lambda: _connect(socket.AF_INET6), 12)
    if not v6["ok"]:
        v6.update(ok=True, detail="Not available on this network — that's fine, IPv4 is used.")
    steps.append(v6)

    client = jarvis._ensure_client()
    jarvis._available = None
    steps.append(_step("API key: list models", lambda: f"{len(jarvis.available_models())} chat models available", 20))

    order = jarvis.model_order()[:6]
    simple_cfg = types.GenerateContentConfig(http_options=types.HttpOptions(timeout=22000))
    working = None
    for m in order:
        r = _step(f"Simple reply from {m}",
                  lambda m=m: (client.models.generate_content(model=m, contents="Reply with just: OK",
                                                              config=simple_cfg).text or "").strip(), 20)
        steps.append(r)
        if r["ok"] and not working:
            working = m

    if working:
        def full() -> str:
            cfg = jarvis.build_config()
            cfg.http_options = types.HttpOptions(timeout=32000)
            resp = client.models.generate_content(
                model=working, contents=[types.Content(role="user", parts=[types.Part(text="hi")])], config=cfg)
            return (resp.text or "(tool call)").strip()[:120]

        steps.append(_step(f"Full JARVIS request (tools + persona) on {working}", full, 30))
    jarvis._cooldown.clear()  # give every model a fresh chance after testing
    return {"steps": steps, "order": order}
