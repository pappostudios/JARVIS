"""Web search for up-to-date information (news, prices, opening hours, facts), plus reading a web page.

web_search asks Gemini with Google Search grounding first (answers with sources). If that isn't available on the
key's tier or model, it falls back to DuckDuckGo results, which JARVIS then summarises.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

from google.genai import types

from . import usage

log = logging.getLogger("jarvis.web")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
_grounding_broken: dict[str, float] = {}  # model → when Google Search grounding failed (skip it for 6 hours)
RETRY_GROUNDING_S = 6 * 3600


def _grounded_models() -> list[str]:
    from .agent import jarvis

    import time

    order = [m for m in jarvis.model_order() if time.time() - _grounding_broken.get(m, 0) > RETRY_GROUNDING_S]
    return order[:3]


def _gemini_search(query: str) -> dict | None:
    from .agent import jarvis

    client = jarvis._ensure_client()
    prompt = ("Search the web and answer factually and concisely (a few sentences, include key numbers, names and "
              f"dates; say how recent the information is):\n\n{query}")
    for model in _grounded_models():
        usage.check_limit()
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    http_options=types.HttpOptions(timeout=30000, retry_options=types.HttpRetryOptions(attempts=1))),
            )
        except Exception as e:  # noqa: BLE001
            usage.record_error(model, "search", e)
            import time

            _grounding_broken[model] = time.time()
            log.info("google search grounding unavailable on %s: %s", model, str(e)[:160])
            continue
        usage.record_ok(model, "search", resp)
        text = (resp.text or "").strip()
        sources = []
        gm = resp.candidates[0].grounding_metadata if resp.candidates else None
        for ch in (gm.grounding_chunks or []) if gm else []:
            if ch.web and ch.web.uri:
                sources.append({"title": ch.web.title or ch.web.domain or "", "url": ch.web.uri})
        if text:
            return {"answer": text, "sources": sources[:6], "via": f"Google Search ({model})"}
    return None


def _ddg_search(query: str, max_results: int) -> dict:
    from ddgs import DDGS

    with DDGS() as d:
        rows = d.text(query, max_results=max_results) or []
    return {"results": [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
                        for r in rows], "via": "DuckDuckGo"}


def web_search(query: str, max_results: int = 6) -> dict:
    """Search the internet for current or factual information: news, prices, sports results, weather, opening hours,
    product details, people, companies, "what is…", "latest…". Returns an answer with sources, or search results to
    summarise. Mention where the information came from when you answer.

    Args:
        query: What to search for, phrased like a search query.
        max_results: How many results (for the DuckDuckGo fallback).
    """
    q = query.strip()
    if not q:
        return {"error": "Empty search."}
    try:
        g = _gemini_search(q)
        if g:
            return g
    except usage.DailyLimitReached:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("gemini search failed: %r", e)
    try:
        return _ddg_search(q, max(1, min(int(max_results), 10)))
    except ImportError:
        return {"error": "Web search fallback isn't installed. Update JARVIS (it installs the 'ddgs' package)."}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Web search failed: {e}"}


def _public_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return bool(host)
    except Exception:  # noqa: BLE001
        return False


def read_webpage(url: str, max_chars: int = 8000) -> dict:
    """Open a web page (e.g. a search result) and return its readable text, to answer from its details.

    Args:
        url: Full http(s) address.
        max_chars: How much text to return.
    """
    import httpx

    from .google_services import _html_to_text

    u = url.strip()
    if not u.lower().startswith(("http://", "https://")):
        return {"error": "Only http(s) web pages can be read."}
    if not _public_url(u):
        return {"error": "That address points to a private/local network, which I don't read."}
    try:
        with httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": UA}) as c:
            r = c.get(u)
        if r.status_code >= 400:
            return {"error": f"The page returned HTTP {r.status_code}."}
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return {"error": f"That isn't a web page I can read ({ctype or 'unknown type'})."}
        html = r.text[:2_000_000]
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        text = _html_to_text(html)
        n = max(500, min(int(max_chars), 20000))
        out: dict[str, Any] = {"url": str(r.url), "title": (title.group(1).strip() if title else "")[:200],
                               "text": text[:n]}
        if len(text) > n:
            out["truncated"] = True
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": f"Couldn't read the page: {e}"}
