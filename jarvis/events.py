"""Server → dashboard push events (Server-Sent Events). Used for the wake word ("Hey JARVIS" → start a call)."""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None
_queues: set[asyncio.Queue] = set()
_lock = threading.Lock()


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def publish(event: dict[str, Any]) -> int:
    """Send an event to every connected dashboard. Safe to call from any thread. Returns listeners reached."""
    if _loop is None:
        return 0
    with _lock:
        queues = list(_queues)
    for q in queues:
        _loop.call_soon_threadsafe(q.put_nowait, event)
    return len(queues)


async def stream():
    q: asyncio.Queue = asyncio.Queue()
    with _lock:
        _queues.add(q)
    try:
        yield "retry: 3000\n\n"
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=25)
                yield f"data: {json.dumps(ev)}\n\n"
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
    finally:
        with _lock:
            _queues.discard(q)
