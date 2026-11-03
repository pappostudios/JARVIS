"""Start the JARVIS server only (no window). Open http://127.0.0.1:8765 or connect from your phone."""
from __future__ import annotations

import socket
import sys

import uvicorn

from jarvis import config, logsetup

_server: uvicorn.Server | None = None


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def local_url() -> str:
    scheme = "https" if config.SSL_CERTFILE else "http"
    return f"{scheme}://127.0.0.1:{config.PORT}/"


def banner() -> None:
    scheme = "https" if config.SSL_CERTFILE else "http"
    print("\n  ==============================================")
    print("              J . A . R . V . I . S .")
    print("  ==============================================")
    print(f"  Desktop : {local_url()}")
    if config.HOST in ("0.0.0.0", "::"):
        print(f"  Phone   : {scheme}://{lan_ip()}:{config.PORT}   (same Wi-Fi)")
        print("            or your Tailscale HTTPS address (see README)")
    if not config.GEMINI_API_KEY:
        print("\n  WARNING: GEMINI_API_KEY is not set in .env - JARVIS cannot think yet.")
    if not config.ACCESS_CODE:
        print("  WARNING: JARVIS_ACCESS_CODE is empty - anyone on your network could open the dashboard.")
    print(f"  Log file: {logsetup.LOG_FILE}\n")


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", config.PORT)) == 0


def make_server() -> uvicorn.Server:
    global _server
    cfg = uvicorn.Config(
        "jarvis.server:app",
        host=config.HOST,
        port=config.PORT,
        ssl_certfile=config.SSL_CERTFILE,
        ssl_keyfile=config.SSL_KEYFILE,
        log_level="warning",
        use_colors=False,
    )
    _server = uvicorn.Server(cfg)
    return _server


def request_shutdown() -> None:
    if _server is not None:
        _server.should_exit = True


def serve() -> None:
    make_server().run()


if __name__ == "__main__":
    logsetup.setup()
    banner()
    if port_in_use():
        print(f"  Port {config.PORT} is already in use - is JARVIS already running? Change PORT in .env if not.")
        sys.exit(1)
    serve()
