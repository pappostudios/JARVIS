"""Wake word: say "Hey JARVIS" (or "Hi JARVIS") and a Live call opens in the JARVIS window.

Runs entirely on this computer with openWakeWord's "hey_jarvis" model — no audio leaves the PC while listening.
The extra packages (~100 MB: onnxruntime, numpy/scipy, sounddevice) are installed the first time it's switched on.
Detection pauses during Live calls so saying "Jarvis" in a call doesn't retrigger it.
"""
from __future__ import annotations

import importlib
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import db, events

log = logging.getLogger("jarvis.wakeword")

PACKAGES = ["openwakeword>=0.6", "sounddevice>=0.4", "numpy"]
THRESHOLDS = {"low": 0.7, "normal": 0.5, "high": 0.35}   # sensitivity → score needed
COOLDOWN_S = 4.0
SAMPLE_RATE, CHUNK = 16000, 1280                           # openWakeWord expects 80 ms frames at 16 kHz

state = {"status": "off", "message": "", "last_wake": None, "score": 0.0}
_stop = threading.Event()
_thread: threading.Thread | None = None
_calls = 0                  # active Live calls (detection paused while > 0)
_lock = threading.Lock()


def enabled() -> bool:
    return (db.get_setting("wake_word") or "false").lower() == "true"


def call_started() -> None:
    global _calls
    with _lock:
        _calls += 1


def call_ended() -> None:
    global _calls
    with _lock:
        _calls = max(0, _calls - 1)


def _set(status: str, message: str = "") -> None:
    state.update(status=status, message=message)
    log.info("wake word: %s %s", status, message)


def _python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def _ensure_packages() -> None:
    try:
        importlib.import_module("openwakeword")
        importlib.import_module("sounddevice")
        return
    except ImportError:
        pass
    _set("installing", "Installing the wake-word engine (one time, ~100 MB)…")
    flags = 0x08000000 if sys.platform == "win32" else 0
    r = subprocess.run([_python(), "-m", "pip", "install", "-q", *PACKAGES], capture_output=True, text=True,
                       timeout=1800, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError("Installing the wake-word engine failed: " + (r.stderr or r.stdout)[-600:])
    importlib.invalidate_caches()


def _run() -> None:
    try:
        _ensure_packages()
        _set("loading", "Downloading the 'hey jarvis' model…")
        import numpy as np
        import sounddevice as sd
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        download_models(["hey_jarvis"])
        model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        key = next(iter(model.models))
        last_fire = 0.0
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK) as stream:
            _set("listening", 'Listening for "Hey JARVIS"')
            while not _stop.is_set():
                data, _ = stream.read(CHUNK)
                if _calls > 0:
                    if state["status"] != "paused":
                        _set("paused", "Paused during the call")
                        model.reset()
                    continue
                if state["status"] == "paused":
                    _set("listening", 'Listening for "Hey JARVIS"')
                score = float(model.predict(np.asarray(data[:, 0], dtype=np.int16))[key])
                state["score"] = round(score, 2)
                threshold = THRESHOLDS.get(db.get_setting("wake_sensitivity") or "normal", 0.5)
                if score >= threshold and time.time() - last_fire > COOLDOWN_S:
                    last_fire = time.time()
                    model.reset()
                    state["last_wake"] = time.strftime("%H:%M:%S")
                    reached = events.publish({"type": "wake", "score": round(score, 2)})
                    log.info("wake word heard (score %.2f) — notified %d window(s)", score, reached)
        _set("off")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "PortAudio" in msg or "Invalid device" in msg or "No Default Input Device" in msg:
            msg = "No microphone found on this PC. Plug one in (or pick a default input in Windows sound settings)."
        _set("error", msg[:400])


def apply() -> dict:
    """Start or stop listening to match the setting."""
    global _thread
    if enabled():
        if not (_thread and _thread.is_alive()):
            _stop.clear()
            _thread = threading.Thread(target=_run, daemon=True, name="wake-word")
            _thread.start()
    else:
        _stop.set()
        if state["status"] != "error":
            _set("off")
    return status()


def status() -> dict:
    return {"enabled": enabled(), **state, "sensitivity": db.get_setting("wake_sensitivity") or "normal"}
