"""PC control: open apps, websites, files and folders, and control media/volume — on the computer JARVIS runs on.

Every public function here is a Gemini tool. Requests from the phone also act on this computer.
Safety: the file opener never launches programs or scripts (use open_app for apps), and the whole feature can be
switched off in Settings ("Allow PC control").
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Optional

from . import db

SYSTEM = platform.system()  # Windows | Darwin | Linux
HOME = Path.home()

# Files with these extensions run code — they are never opened through open_file / find_files.
EXECUTABLE = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp",
              ".scr", ".pif", ".reg", ".hta", ".cpl", ".jar", ".sh", ".app", ".command", ".py", ".pyw"}

# Friendly names → how to open them, for apps that are not normal Start-menu entries.
WINDOWS_ALIASES = {
    "calculator": "calc.exe", "calc": "calc.exe", "notepad": "notepad.exe", "paint": "mspaint.exe",
    "file explorer": "explorer.exe", "explorer": "explorer.exe", "files": "explorer.exe",
    "task manager": "taskmgr.exe", "settings": "ms-settings:", "windows settings": "ms-settings:",
    "control panel": "control.exe", "command prompt": "cmd.exe", "terminal": "wt.exe", "snipping tool": "ms-screenclip:",
    "camera": "microsoft.windows.camera:", "mail": "outlookmail:", "store": "ms-windows-store:", "clock": "ms-clock:",
}


class PCControlOff(RuntimeError):
    pass


def _allowed() -> None:
    if (db.get_setting("pc_control") or "true").lower() != "true":
        raise PCControlOff("PC control is switched off (Settings → PC control).")


def _launch(target: str) -> None:
    if SYSTEM == "Windows":
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    elif SYSTEM == "Darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _score(query: str, name: str) -> int:
    q, n = _norm(query), _norm(name)
    if not q or not n:
        return 0
    if n == q:
        return 100
    if n.startswith(q):
        return 80
    if q in n:
        return 60
    words = q.split()
    if words and all(w in n for w in words):
        return 45
    return 0


# ── apps ──────────────────────────────────────────────────────
def _start_menu_entries() -> list[tuple[str, Path]]:
    roots = [Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
             Path(os.environ.get("APPDATA", str(HOME / "AppData/Roaming"))) / "Microsoft/Windows/Start Menu/Programs",
             HOME / "Desktop", Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"]
    out = []
    for root in roots:
        if root.exists():
            for p in root.rglob("*"):
                if p.suffix.lower() in (".lnk", ".url", ".appref-ms"):
                    out.append((p.stem, p))
    return out


def _mac_apps() -> list[tuple[str, Path]]:
    out = []
    for root in (Path("/Applications"), Path("/System/Applications"), HOME / "Applications"):
        if root.exists():
            for p in root.glob("*.app"):
                out.append((p.stem, p))
            for p in root.glob("*/*.app"):
                out.append((p.stem, p))
    return out


def _linux_apps() -> list[tuple[str, Path]]:
    out = []
    for root in (Path("/usr/share/applications"), HOME / ".local/share/applications"):
        if root.exists():
            for p in root.glob("*.desktop"):
                name = p.stem
                try:
                    m = re.search(r"^Name=(.+)$", p.read_text(errors="ignore"), re.M)
                    name = m.group(1) if m else name
                except OSError:
                    pass
                out.append((name, p))
    return out


def open_app(name: str) -> dict:
    """Open / launch an application on the user's computer by name (e.g. "Spotify", "Chrome", "Word", "Calculator",
    "File Explorer", "Settings"). Acts on the PC where JARVIS runs, even when the user is on the phone.

    Args:
        name: The app's name as the user says it.
    """
    _allowed()
    q = name.strip()
    if SYSTEM == "Windows" and _norm(q) in WINDOWS_ALIASES:
        _launch(WINDOWS_ALIASES[_norm(q)])
        return {"opened": q}
    entries = _start_menu_entries() if SYSTEM == "Windows" else _mac_apps() if SYSTEM == "Darwin" else _linux_apps()
    ranked = sorted(((_score(q, n), n, p) for n, p in entries), key=lambda x: (-x[0], len(x[1])))
    ranked = [r for r in ranked if r[0] > 0 and "uninstall" not in r[1].lower()]
    if ranked:
        _, n, p = ranked[0]
        if SYSTEM == "Linux" and p.suffix == ".desktop" and shutil.which("gtk-launch"):
            subprocess.Popen(["gtk-launch", p.stem], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            _launch(str(p))
        others = [r[1] for r in ranked[1:4] if r[1] != n]
        return {"opened": n, **({"other_matches": others} if others else {})}
    exe = shutil.which(q) or shutil.which(q.replace(" ", ""))
    if exe:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"opened": q}
    return {"error": f"I couldn't find an app called '{name}' on this computer."}


def open_website(url: str) -> dict:
    """Open a website in the computer's default browser.

    Args:
        url: Address, e.g. "youtube.com" or a full https:// link.
    """
    _allowed()
    u = url.strip()
    if not re.match(r"^[a-z]+://", u, re.I):
        u = "https://" + u
    if not u.lower().startswith(("http://", "https://")):
        return {"error": "Only web addresses (http/https) can be opened."}
    webbrowser.open(u)
    return {"opened": u}


# ── files ─────────────────────────────────────────────────────
def _search_roots() -> list[Path]:
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "Movies"]
    roots = [HOME / n for n in names]
    for od in HOME.glob("OneDrive*"):
        roots += [od] + [od / n for n in names]
    seen, out = set(), []
    for r in roots:
        try:
            key = r.resolve()
        except OSError:
            continue
        if r.exists() and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _walk(root: Path, query: str, want_dirs: bool, deadline: float, found: list) -> None:
    skip = {"node_modules", ".git", "__pycache__", "AppData", ".venv", "venv", "$RECYCLE.BIN"}
    for dirpath, dirnames, filenames in os.walk(root):
        if time.time() > deadline:
            return
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        if dirpath[len(str(root)):].count(os.sep) > 6:
            dirnames[:] = []
        items = dirnames if want_dirs else filenames
        for name in items:
            sc = _score(query, Path(name).stem if not want_dirs else name)
            if sc:
                p = Path(dirpath) / name
                if not want_dirs and p.suffix.lower() in EXECUTABLE:
                    continue
                try:
                    found.append((sc, p.stat().st_mtime, p))
                except OSError:
                    pass


def find_files(query: str, kind: str = "file", limit: int = 10) -> list[dict]:
    """Search the user's own folders (Desktop, Documents, Downloads, Pictures, Music, Videos, OneDrive) for files or
    folders whose name matches. Best matches first, then most recently changed. Programs/scripts are never returned.

    Args:
        query: Words from the file or folder name.
        kind: "file" or "folder".
        limit: Maximum results.
    """
    _allowed()
    found: list = []
    deadline = time.time() + 6
    for root in _search_roots():
        _walk(root, query, kind == "folder", deadline, found)
    found.sort(key=lambda x: (-x[0], -x[1]))
    out, seen = [], set()
    for sc, mtime, p in found:
        if str(p) in seen:
            continue
        seen.add(str(p))
        out.append({"name": p.name, "path": str(p), "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))})
        if len(out) >= max(1, int(limit)):
            break
    return out


def open_file(name_or_path: str) -> dict:
    """Open a document, photo, video, music file etc. with its default app. Give a full path (from find_files) or
    just words from the file name — the best match in the user's folders is opened. Programs and scripts are refused.

    Args:
        name_or_path: Full path, or part of the file name.
    """
    _allowed()
    p = Path(os.path.expandvars(os.path.expanduser(name_or_path.strip().strip('"'))))
    if not p.is_file():
        matches = find_files(name_or_path, "file", 5)
        if not matches:
            return {"error": f"No file matching '{name_or_path}' in your Desktop/Documents/Downloads/etc."}
        p = Path(matches[0]["path"])
        others = [m["name"] for m in matches[1:]]
    else:
        others = []
    if p.suffix.lower() in EXECUTABLE:
        return {"error": f"{p.name} is a program or script; I don't run those from the file finder. Use open_app."}
    _launch(str(p))
    return {"opened": str(p), **({"other_matches": others} if others else {})}


def open_folder(name_or_path: str) -> dict:
    """Open a folder in the file manager — a full path, a standard folder ("Downloads", "Documents", "Desktop"),
    or words from a folder name inside the user's folders.

    Args:
        name_or_path: Folder path or name.
    """
    _allowed()
    q = name_or_path.strip().strip('"')
    p = Path(os.path.expandvars(os.path.expanduser(q)))
    if not p.is_dir():
        std = {n.lower(): HOME / n for n in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")}
        p = std.get(q.lower(), p)
    if not p.is_dir():
        matches = find_files(q, "folder", 3)
        if not matches:
            return {"error": f"No folder matching '{name_or_path}'."}
        p = Path(matches[0]["path"])
    _launch(str(p))
    return {"opened": str(p)}


# ── media & volume ────────────────────────────────────────────
_VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
       "mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF}


def _press(vk: int, times: int = 1) -> None:
    import ctypes

    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 1, 0)       # KEYEVENTF_EXTENDEDKEY
        ctypes.windll.user32.keybd_event(vk, 0, 1 | 2, 0)   # + KEYEVENTF_KEYUP
        time.sleep(0.01)


def _run(cmd: list[str]) -> bool:
    if not shutil.which(cmd[0]):
        return False
    return subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0


def media_control(action: str, volume_percent: Optional[int] = None) -> dict:
    """Control music/video playback and the computer's volume (works with Spotify, YouTube in the browser, etc.).

    Args:
        action: One of: play_pause, next, previous, stop, volume_up, volume_down, mute, set_volume.
        volume_percent: For set_volume: 0-100.
    """
    _allowed()
    a = action.strip().lower().replace(" ", "_").replace("-", "_")
    a = {"play": "play_pause", "pause": "play_pause", "resume": "play_pause", "skip": "next", "back": "previous",
         "louder": "volume_up", "quieter": "volume_down", "unmute": "mute", "volume": "set_volume"}.get(a, a)
    if a not in _VK and a != "set_volume":
        return {"error": "action must be play_pause, next, previous, stop, volume_up, volume_down, mute or set_volume"}

    if SYSTEM == "Windows":
        if a == "set_volume":
            pct = max(0, min(100, int(volume_percent if volume_percent is not None else 50)))
            _press(_VK["volume_down"], 50)            # each key press is 2% — go to 0, then up
            _press(_VK["volume_up"], round(pct / 2))
            return {"done": f"volume set to about {pct}%"}
        steps = 5 if a in ("volume_up", "volume_down") else 1   # 10% per request
        _press(_VK[a], steps)
        return {"done": a}

    if SYSTEM == "Darwin":
        script = {
            "volume_up": "set volume output volume ((output volume of (get volume settings)) + 10)",
            "volume_down": "set volume output volume ((output volume of (get volume settings)) - 10)",
            "mute": "set volume output muted (not (output muted of (get volume settings)))",
            "set_volume": f"set volume output volume {max(0, min(100, int(volume_percent or 50)))}",
        }.get(a)
        if script is None:  # playback: ask the music apps that are running
            verb = {"play_pause": "playpause", "next": "next track", "previous": "previous track", "stop": "pause"}[a]
            script = ('repeat with appName in {"Spotify", "Music"}\n if application appName is running then '
                      f'tell application appName to {verb}\nend repeat')
        ok = _run(["osascript", "-e", script])
        return {"done": a} if ok else {"error": "macOS refused the media command."}

    # Linux: playerctl for playback, pactl for volume
    if a in ("volume_up", "volume_down", "mute", "set_volume"):
        arg = {"volume_up": ["set-sink-volume", "@DEFAULT_SINK@", "+10%"],
               "volume_down": ["set-sink-volume", "@DEFAULT_SINK@", "-10%"],
               "mute": ["set-sink-mute", "@DEFAULT_SINK@", "toggle"],
               "set_volume": ["set-sink-volume", "@DEFAULT_SINK@", f"{max(0, min(100, int(volume_percent or 50)))}%"]}[a]
        ok = _run(["pactl", *arg])
    else:
        ok = _run(["playerctl", {"play_pause": "play-pause", "next": "next", "previous": "previous", "stop": "stop"}[a]])
    return {"done": a} if ok else {"error": "Install playerctl / pactl for media control on Linux."}
