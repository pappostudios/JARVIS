"""Self-updater: pulls the latest JARVIS code from GitHub with git, keeps .env / data / .venv untouched,
reinstalls packages only if requirements.txt changed, then restarts JARVIS.

Usage:  python -m jarvis.updater          (what update.bat runs)
        Settings → "Check for updates" in the dashboard
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import config

REPO_URL = os.getenv("UPDATE_REPO", "https://github.com/pappostudios/JARVIS.git").strip()
ROOT = config.ROOT
_NO_WINDOW = 0x08000000 if platform.system() == "Windows" else 0

state: dict = {"running": False, "step": "", "error": None, "done": False}


class UpdateError(RuntimeError):
    pass


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(*args: str, check: bool = True, timeout: int = 180) -> str:
    env = dict(os.environ)
    if not (sys.stdin and sys.stdin.isatty()):
        env["GIT_TERMINAL_PROMPT"] = "0"  # in the background, never hang waiting for a typed password
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                       env=env, creationflags=_NO_WINDOW)
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        if "Authentication failed" in msg or "could not read Username" in msg or "Repository not found" in msg:
            msg = ("GitHub refused access. Run update.bat once from the JARVIS folder and sign in to GitHub "
                   "when asked (needed because the repository is private).\n\n" + msg)
        raise UpdateError(msg)
    return r.stdout.strip()


def _branch() -> str:
    """The branch to follow: UPDATE_BRANCH from .env, else the repository's default branch on GitHub."""
    override = os.getenv("UPDATE_BRANCH", "").strip()
    if override:
        return override
    out = _git("ls-remote", "--symref", REPO_URL, "HEAD")
    for line in out.splitlines():
        if line.startswith("ref:"):
            return line.split()[1].removeprefix("refs/heads/")
    up = _git("rev-parse", "--abbrev-ref", "@{u}", check=False)
    return up.split("/", 1)[1] if up.startswith("origin/") else "main"


def _ensure_repo() -> str:
    """Turn a downloaded-ZIP folder into a git checkout (one time). Untracked files (.env, data, .venv) are kept."""
    if not git_available():
        raise UpdateError("Git is not installed. Run update.bat once — it installs Git for you.")
    if not (ROOT / ".git").exists():
        state["step"] = "Linking this folder to GitHub (first time only)"
        _git("init", "-q")
        _git("remote", "add", "origin", REPO_URL)
    elif not _git("remote", check=False):
        _git("remote", "add", "origin", REPO_URL)
    branch = _branch()
    state["step"] = "Downloading updates"
    _git("fetch", "-q", "origin", branch)
    return branch


def _req_hash() -> str:
    p = ROOT / "requirements.txt"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def check() -> dict:
    branch = _ensure_repo()
    remote = _git("rev-parse", f"origin/{branch}")
    try:
        local = _git("rev-parse", "HEAD")
    except UpdateError:
        local = ""  # fresh conversion from a ZIP: no local commit yet
    if local == remote:
        return {"available": False, "branch": branch, "current": local[:7]}
    changes: list[str] = []
    if local:
        log = _git("log", "--format=%s", f"{local}..{remote}", check=False)
        changes = [line for line in log.splitlines() if line][:15]
    return {"available": True, "branch": branch, "current": local[:7] or "zip-download",
            "latest": remote[:7], "changes": changes or ["Latest version of JARVIS"]}


def apply() -> dict:
    """Bring the code to the latest version. Your .env, data/ and .venv are never touched."""
    branch = _ensure_repo()
    before = _req_hash()
    state["step"] = "Installing new version"
    _git("checkout", "-q", "-B", branch, f"origin/{branch}", "--force")
    _git("branch", "-q", f"--set-upstream-to=origin/{branch}", branch, check=False)
    if _req_hash() != before:
        state["step"] = "Installing new packages"
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        r = subprocess.run([str(python), "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=ROOT,
                           capture_output=True, text=True, timeout=900, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise UpdateError("Package install failed:\n" + (r.stderr or r.stdout)[-1500:])
    return {"updated": True, "version": _git("rev-parse", "--short", "HEAD")}


def restart_detached() -> None:
    """Start a fresh JARVIS that waits for this one to exit, then stop this one."""
    exe = Path(sys.executable)
    if platform.system() == "Windows" and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    kwargs: dict = {"cwd": ROOT, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([str(exe), "desktop.py", "--after-update", "--no-window"], **kwargs)


def apply_in_background(on_success) -> dict:
    if state["running"]:
        return {"started": False}
    state.update(running=True, step="Starting", error=None, done=False)

    def _run():
        try:
            apply()
            state.update(step="Restarting", done=True)
            on_success()
        except Exception as e:  # noqa: BLE001
            state["error"] = str(e)
        finally:
            state["running"] = False

    threading.Thread(target=_run, daemon=True, name="jarvis-updater").start()
    return {"started": True}


def _restart_running_server() -> None:
    """Used by update.bat: ask a running JARVIS to stop, then start the new version."""
    import json
    import urllib.request

    base = f"http://127.0.0.1:{config.PORT}/api/"
    try:
        urllib.request.urlopen(base + "health", timeout=2)
    except Exception:  # noqa: BLE001
        return  # not running; update.bat starts it afterwards
    token = (config.DATA_DIR / "admin.token").read_text().strip()
    req = urllib.request.Request(base + "shutdown", data=json.dumps({}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Admin-Token": token})
    urllib.request.urlopen(req, timeout=5)


if __name__ == "__main__":
    print("\n  J.A.R.V.I.S. updater\n")
    try:
        info = check()
        if not info["available"]:
            print(f"  Already up to date ({info['current']}).")
        else:
            print("  New version available:")
            for c in info["changes"]:
                print("   -", c)
            apply()
            print("\n  Updated successfully. Your settings, data and Google sign-in were kept.")
        _restart_running_server()
    except UpdateError as e:
        print("  Update failed:\n ", e)
        sys.exit(1)
