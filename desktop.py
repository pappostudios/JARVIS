"""JARVIS desktop app: starts the server and opens the dashboard in its own app window.

Uses Edge/Chrome "app mode" (a clean, frameless window with full microphone support).
Falls back to pywebview if installed, else your default browser.
The server keeps running while this console is open, so your phone stays connected.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

from jarvis import config  # noqa: E402
from run_jarvis import banner, serve  # noqa: E402


def url() -> str:
    scheme = "https" if config.SSL_CERTFILE else "http"
    return f"{scheme}://localhost:{config.PORT}/"


def wait_for_server(timeout: float = 30) -> bool:
    import ssl

    ctx = ssl._create_unverified_context()  # local self-signed certs are fine here
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url() + "api/health", timeout=1, context=ctx)
            return True
        except Exception:  # noqa: BLE001
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
    target = url()
    browser = find_chromium()
    if browser:
        profile = config.DATA_DIR / "app-window-profile"
        subprocess.Popen([
            browser, f"--app={target}", f"--user-data-dir={profile}", "--window-size=1440,920",
            "--no-first-run", "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
        ])
        return
    try:
        import webview  # type: ignore  # optional: pip install pywebview

        webview.create_window("J.A.R.V.I.S.", target, width=1440, height=920, background_color="#03070d")
        webview.start()  # blocks until window closes
        return
    except ImportError:
        pass
    webbrowser.open(target)


def main() -> None:
    banner()
    threading.Thread(target=serve, daemon=True).start()
    if not wait_for_server():
        print("  ✗ Server did not start. Is another program using port", config.PORT, "?")
        sys.exit(1)
    open_window()
    print("  JARVIS is online. Close this window (or press Ctrl+C) to shut him down.\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("  Goodbye, sir.")


if __name__ == "__main__":
    main()
