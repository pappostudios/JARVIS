"""Logging to data/jarvis.log so problems can be diagnosed even when JARVIS runs without a console window."""
from __future__ import annotations

import faulthandler
import io
import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler

from . import config

LOG_FILE = config.DATA_DIR / "jarvis.log"
_done = False


class _Tee(io.TextIOBase):
    """Write to the original stream (if any) and to the log file."""

    def __init__(self, original, logfile):
        self.original, self.logfile = original, logfile

    def write(self, s):  # type: ignore[override]
        for stream in (self.original, self.logfile):
            if stream is not None:
                try:
                    stream.write(s)
                    stream.flush()
                except Exception:  # noqa: BLE001
                    pass
        return len(s)

    def flush(self):  # type: ignore[override]
        for stream in (self.original, self.logfile):
            try:
                stream and stream.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self):  # uvicorn checks this for colours
        return False


def setup() -> None:
    global _done
    if _done:
        return
    _done = True
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
        LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
    logfile = open(LOG_FILE, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
    sys.stdout = _Tee(sys.stdout, logfile)  # type: ignore[assignment]
    sys.stderr = _Tee(sys.stderr, logfile)  # type: ignore[assignment]
    faulthandler.enable(file=logfile)

    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("httpx", "googleapiclient.discovery_cache", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def _excepthook(exc_type, exc, tb):
        logging.getLogger("jarvis").critical("Uncaught error:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))

    def _thread_hook(args):
        logging.getLogger("jarvis").critical(
            "Uncaught error in thread %s:\n%s",
            args.thread.name if args.thread else "?",
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    logging.getLogger("jarvis").info("──── JARVIS starting (python %s) ────", sys.version.split()[0])


def tail(lines: int = 150) -> str:
    try:
        data = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except FileNotFoundError:
        return "(no log yet)"
