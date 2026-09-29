"""HTTP endpoint for TradingView webhook alerts.

The Pine indicator's "any alert() function call" alert sends JSON like::

    {"passphrase": "...", "event": "setup", "ticker": "BTCUSDT", "tf": "15",
     "side": "long", "model": "reversal", "entry": 1.0, "sl": 0.9, "tp": 1.2,
     "score": 7, "grade": "A", "features": {...}}

TradingView only posts to ports 80/443, so put this behind a reverse proxy or
a tunnel (Caddy, nginx, Cloudflare Tunnel, ngrok) with HTTPS.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024


def make_handler(passphrase: str, on_alert: Callable[[dict[str, Any]], dict[str, Any]]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "smc-agent"

        def _reply(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # health check
            self._reply(200, {"ok": True})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._reply(413 if length > MAX_BODY else 400, {"error": "bad body size"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._reply(400, {"error": "body must be JSON"})
                return
            if not isinstance(payload, dict):
                self._reply(400, {"error": "body must be a JSON object"})
                return
            supplied = str(payload.pop("passphrase", ""))
            if not passphrase or not hmac.compare_digest(supplied.encode(), passphrase.encode()):
                log.warning("webhook: rejected alert with bad passphrase from %s", self.client_address[0])
                self._reply(403, {"error": "forbidden"})
                return
            if payload.get("event", "setup") != "setup":
                self._reply(200, {"ignored": payload.get("event")})
                return
            try:
                result = on_alert(payload)
            except Exception as exc:  # noqa: BLE001
                log.exception("webhook: handler failed")
                self._reply(500, {"error": str(exc)})
                return
            self._reply(200, result)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("webhook: " + fmt, *args)

    return Handler


def serve(host: str, port: int, passphrase_env: str,
          on_alert: Callable[[dict[str, Any]], dict[str, Any]]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    passphrase = os.environ.get(passphrase_env, "")
    if not passphrase:
        raise SystemExit(f"set the {passphrase_env} environment variable (same value as the Pine input)")
    server = ThreadingHTTPServer((host, port), make_handler(passphrase, on_alert))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="webhook")
    thread.start()
    log.info("webhook listening on http://%s:%d", host, port)
    return server, thread
