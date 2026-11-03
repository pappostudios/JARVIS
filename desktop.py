"""JARVIS desktop app: starts the server in the background and opens the dashboard in its own app window.

- Launched via JARVIS.bat / the desktop icon it runs WITHOUT a console window (pythonw), so it can't be closed
  by accident. Closing the dashboard window does NOT stop JARVIS (your phone stays connected).
  To stop him: Settings -> "Shut down JARVIS".
- Clicking the icon while JARVIS is already running just reopens the window.
- Everything is logged to data/jarvis.log.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

from jarvis import config, logsetup  # noqa: E402

logsetup.setup()

import run_jarvis  # noqa: E402

URL = run_jarvis.local_url()
_ctx = ssl._create_unverified_context()  # local self-signed certs are fine here


def alert(message: str, title: str = "J.A.R.V.I.S.") -> None:
    """Show a message even when there is no console window."""
    print(f"\n  {message}\n")
    if platform.system() == "Windows":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
        except Exception:  # noqa: BLE001
            pass


def health() -> dict | None:
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=1.5, context=_ctx) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def wait_for_server(thread: threading.Thread, timeout: float = 40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if health():
            return True
        if not thread.is_alive():
            return False
        time.sleep(0.3)
    return False


def find_chromium() -> str | None:
    system = platform.system()
    candidates: list[str] = []
    if system == "Windows":
        for base in (os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                candidates += [
                    os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                ]
    elif system == "Darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for c in candidates:
        if os.path.exists(c):
            return c
    for name in ("microsoft-edge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def open_window() -> None:
    browser = find_chromium()
    if browser:
        profile = config.DATA_DIR / "app-window-profile"
        flags = 0x08000000 if platform.system() == "Windows" else 0  # CREATE_NO_WINDOW
        subprocess.Popen([
            browser, f"--app={URL}", f"--user-data-dir={profile}", "--window-size=1440,920",
            "--no-first-run", "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
        ], creationflags=flags)
        return
    webbrowser.open(URL)


def main() -> None:
    run_jarvis.banner()

    # Already running? Just show the window.
    h = health()
    if h and h.get("app") == "jarvis":
        print("  JARVIS is already running - opening the window.")
        open_window()
        return
    if run_jarvis.port_in_use():
        alert(f"Port {config.PORT} is being used by another program.\n\n"
              f"Change PORT in the .env file (e.g. PORT=8777) and start JARVIS again.")
        sys.exit(1)

    server_thread = threading.Thread(target=run_jarvis.serve, name="jarvis-server", daemon=True)
    server_thread.start()
    if not wait_for_server(server_thread):
        alert("JARVIS could not start.\n\nLast lines of data\\jarvis.log:\n\n" + logsetup.tail(15)[-1500:])
        sys.exit(1)

    open_window()
    print("  JARVIS is online. Shut him down from Settings -> 'Shut down JARVIS' (or press Ctrl+C here).\n")
    try:
        while server_thread.is_alive():
            server_thread.join(1.0)
    except KeyboardInterrupt:
        run_jarvis.request_shutdown()
        server_thread.join(5)
        print("  Goodbye, sir.")
        return
    if not getattr(run_jarvis._server, "should_exit", False):
        alert("JARVIS stopped unexpectedly.\n\nDetails are in data\\jarvis.log:\n\n" + logsetup.tail(15)[-1500:])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        alert(f"JARVIS hit an error while starting:\n\n{type(e).__name__}: {e}\n\nSee data\\jarvis.log")
        sys.exit(1)
