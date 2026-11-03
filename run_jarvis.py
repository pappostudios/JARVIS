"""Start the JARVIS server only (no window). Open http://localhost:8765 or connect from your phone."""
from __future__ import annotations

import socket

import uvicorn

from jarvis import config


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def banner() -> None:
    scheme = "https" if config.SSL_CERTFILE else "http"
    print("\n  ╔══════════════════════════════════════════════╗")
    print("  ║            J . A . R . V . I . S .           ║")
    print("  ╚══════════════════════════════════════════════╝")
    print(f"  Desktop : {scheme}://localhost:{config.PORT}")
    if config.HOST in ("0.0.0.0", "::"):
        print(f"  Phone   : {scheme}://{lan_ip()}:{config.PORT}   (same Wi-Fi)")
        print("            or your Tailscale HTTPS address (see README)")
    if not config.GEMINI_API_KEY:
        print("\n  ⚠  GEMINI_API_KEY is not set in .env — JARVIS cannot think yet.")
    if not config.ACCESS_CODE:
        print("  ⚠  JARVIS_ACCESS_CODE is empty — anyone on your network could open the dashboard.")
    print()


def serve() -> None:
    uvicorn.run(
        "jarvis.server:app",
        host=config.HOST,
        port=config.PORT,
        ssl_certfile=config.SSL_CERTFILE,
        ssl_keyfile=config.SSL_KEYFILE,
        log_level="warning",
    )


if __name__ == "__main__":
    banner()
    serve()
