"""Minimal JSON-over-HTTP transport used by the secure channel.

The HTTP layer itself is NOT trusted: every request body after the handshake
is an AES-GCM envelope (security/channel.py). Tailscale (WireGuard) adds a
second, independent encryption layer when it is used - defence in depth.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import requests

log = logging.getLogger(__name__)
MAX_BODY = 64 * 1024

Handler = Callable[[dict, str], tuple[int, dict]]


class JsonServer:
    def __init__(self, host: str, port: int, routes: dict[str, Handler], name: str = "relay"):
        routes_ = routes

        class _H(BaseHTTPRequestHandler):
            server_version = "SmartPark/1"
            sys_version = ""

            def do_POST(self):  # noqa: N802
                fn = routes_.get(self.path)
                if fn is None:
                    return self._reply(404, {"error": "not_found"})
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if length > MAX_BODY:
                        return self._reply(413, {"error": "too_large"})
                    body = json.loads(self.rfile.read(length) or b"{}")
                    status, out = fn(body, self.client_address[0])
                except json.JSONDecodeError:
                    status, out = 400, {"error": "bad_json"}
                except Exception:
                    log.exception("handler %s failed", self.path)
                    status, out = 500, {"error": "internal"}
                self._reply(status, out)

            def _reply(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.client_address[0], fmt % args)

        self.httpd = ThreadingHTTPServer((host, port), _H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name=name, daemon=True)

    def start(self):
        self._thread.start()
        log.info("Listening on port %d", self.port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


_session = requests.Session()


def post_json(address: str, path: str, body: dict, timeout: float = 5.0) -> tuple[int, dict]:
    """POST to http://<address><path>. Returns (status, json) or raises ConnectionError."""
    try:
        r = _session.post(f"http://{address}{path}", json=body, timeout=timeout)
    except requests.RequestException as e:
        raise ConnectionError(str(e)) from e
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": "bad_response"}
