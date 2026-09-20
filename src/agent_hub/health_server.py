"""Minimal HTTP server, purely to satisfy Cloud Run's health-check port.

This process is a Telegram long-polling worker, not an HTTP service - no
legitimate caller ever hits this port. Runs in a background thread so the
main thread is free to run `Application.run_polling()`, which owns the
asyncio event loop itself.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, *args: object) -> None:  # silence per-request access logs
        pass


def start_health_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
