"""Local web server behind the Touch Display 2 kiosk.

Security
  * bound to 127.0.0.1 by default - only the Chromium kiosk on the gateway
    itself can reach it, never the network
  * every POST must carry a per-boot random token embedded in the page
    (blocks cross-site requests from any other page)
  * admin actions need the admin password again and expire after 5 minutes
"""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
ADMIN_SESSION_S = 300


class KioskServer:
    def __init__(self, launcher, host: str = "127.0.0.1", port: int = 8080):
        self.launcher = launcher
        self.token = secrets.token_urlsafe(24)
        self._admin_tokens: dict[str, float] = {}
        self._admin_fail: list[float] = []
        self._lock = threading.Lock()
        srv = self

        class _H(BaseHTTPRequestHandler):
            server_version = "SmartParkKiosk/1"
            sys_version = ""

            def log_message(self, fmt, *args):
                log.debug(fmt % args)

            def _send(self, status, body: bytes, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status, obj):
                self._send(status, json.dumps(obj).encode())

            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    html = (STATIC / "index.html").read_text().replace("{{TOKEN}}", srv.token)
                    return self._send(200, html.encode(), "text/html; charset=utf-8")
                if path == "/api/state":
                    return self._json(200, srv.state())
                if path.startswith("/static/"):
                    f = (STATIC / path[len("/static/"):]).resolve()
                    if STATIC.resolve() in f.parents and f.is_file():
                        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                        return self._send(200, f.read_bytes(), ctype)
                self._json(404, {"error": "not_found"})

            def do_POST(self):  # noqa: N802
                if not secrets.compare_digest(self.headers.get("X-Kiosk-Token", ""), srv.token):
                    return self._json(403, {"error": "bad_token"})
                try:
                    length = min(int(self.headers.get("Content-Length", 0)), 16384)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    status, out = srv.post(self.path, body)
                except Exception:
                    log.exception("kiosk request failed")
                    status, out = 500, {"error": "internal"}
                self._json(status, out)

        self.httpd = ThreadingHTTPServer((host, port), _H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, name="kiosk", daemon=True).start()
        log.info("Kiosk UI on http://%s:%d", *self.httpd.server_address[:2])

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------
    def state(self) -> dict:
        if self.launcher.locked:
            return {"locked": True, **self.launcher.guard.status()}
        return self.launcher.gateway.kiosk_state()

    def _admin_ok(self, token: str) -> bool:
        exp = self._admin_tokens.get(token or "")
        return bool(exp and exp > time.time())

    def post(self, path: str, b: dict) -> tuple[int, dict]:
        if path == "/api/unlock":
            ok, msg = self.launcher.unlock(str(b.get("password", "")))
            return 200, {"ok": ok, "message": msg}
        gw = self.launcher.gateway
        if gw is None:
            return 423, {"error": "locked"}
        if path == "/api/access/request":
            return 200, gw.request_access(str(b.get("username", ""))[:32])
        if path == "/api/access/verify":
            return 200, gw.verify_access(str(b.get("request_id", "")), str(b.get("code", "")), str(b.get("pin", "")))
        if path == "/api/setup":
            ok, msg = gw.access.complete_setup(str(b.get("username", "")), str(b.get("code", "")),
                                               str(b.get("pin", "")))
            if ok:
                gw.feed(f"New user {str(b.get('username')).lower()} completed setup", "info")
                gw.notify_admin(f"User {str(b.get('username')).lower()} completed setup at the kiosk")
            return 200, {"ok": ok, "message": msg}
        if path == "/api/admin/login":
            return self._admin_login(gw, str(b.get("password", "")))
        if path == "/api/admin/action":
            if not self._admin_ok(b.get("token")):
                return 401, {"error": "admin_session_expired"}
            return 200, self._admin_action(gw, b.get("action", ""), b.get("arg", ""))
        return 404, {"error": "not_found"}

    def _admin_login(self, gw, password: str) -> tuple[int, dict]:
        with self._lock:
            now = time.time()
            self._admin_fail = [t for t in self._admin_fail if t > now - 60]
            if len(self._admin_fail) >= 3:
                return 200, {"ok": False, "message": "Too many attempts - wait 1 minute"}
            if not gw.vault.verify_password(password):
                self._admin_fail.append(now)
                gw.store.add_alert("security", "Failed admin login at kiosk")
                if len(self._admin_fail) >= 3:
                    gw.alert("3 failed admin logins at the kiosk")
                return 200, {"ok": False, "message": "Wrong password"}
            tok = secrets.token_urlsafe(16)
            self._admin_tokens = {t: e for t, e in self._admin_tokens.items() if e > now}
            self._admin_tokens[tok] = now + ADMIN_SESSION_S
            gw.store.add_alert("audit", "Admin logged in at kiosk")
            return 200, {"ok": True, "token": tok}

    def _admin_action(self, gw, action: str, arg: str) -> dict:
        arg = (arg or "").strip()
        if action == "users":
            return {"ok": True, "users": gw.access.list_users()}
        if action == "adduser":
            try:
                code = gw.access.create_user(arg)
            except ValueError as e:
                return {"ok": False, "message": str(e)}
            return {"ok": True, "message": f"User '{arg.lower()}' created. Enrolment code: {code}",
                    "code": code, "ttl_min": gw.cfg["access"]["enrol_code_ttl_min"]}
        if action == "removeuser":
            return {"ok": gw.access.remove_user(arg), "message": f"Removed {arg}"}
        if action == "resetpin":
            try:
                code = gw.access.reset_pin(arg)
            except ValueError as e:
                return {"ok": False, "message": str(e)}
            return {"ok": True, "message": f"PIN reset for '{arg.lower()}'. New enrolment code: {code} - "
                                           f"they repeat New user setup with it.",
                    "code": code, "ttl_min": gw.cfg["access"]["enrol_code_ttl_min"]}
        if action == "unlockuser":
            gw.access.unlock_user(arg)
            return {"ok": True, "message": f"Lockout cleared for {arg}"}
        if action == "open":
            gw.barrier.open()
            gw.store.add_alert("audit", "Barrier opened manually at kiosk")
            return {"ok": True, "message": "Barrier opened"}
        if action == "night":
            gw.night_mode = not gw.night_mode
            gw.feed(f"Night mode {'enabled' if gw.night_mode else 'disabled'}", "info")
            return {"ok": True, "message": f"Night mode {'ON' if gw.night_mode else 'OFF'}"}
        if action == "lights":
            for b in gw.nodes:
                threading.Thread(target=gw.send_command, args=(b, {"cmd": "lights_test"}), daemon=True).start()
            return {"ok": True, "message": "LED test started on all bays"}
        if action == "report":
            return {"ok": True, "report": gw.report()}
        if action == "alerts":
            return {"ok": True, "alerts": [{"ts": ts, "level": lv, "text": t} for ts, lv, t in gw.store.recent_alerts(10)]}
        return {"ok": False, "message": "unknown action"}
