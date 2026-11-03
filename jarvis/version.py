"""Code version (git commit) — used to show the version and to replace an outdated running JARVIS."""
from __future__ import annotations

import os
import subprocess

from . import config

_cached: str | None = None


def current() -> str:
    global _cached
    if _cached is None:
        try:
            _cached = subprocess.run(
                ["git", "log", "-1", "--format=%h %cd", "--date=format:%d %b %H:%M"], cwd=config.ROOT,
                capture_output=True, text=True, timeout=5, creationflags=0x08000000 if os.name == "nt" else 0,
            ).stdout.strip() or "unknown"
        except Exception:  # noqa: BLE001
            _cached = "unknown"
    return _cached
