"""Bay node (edge device): one Raspberry Pi per parking bay.

Responsibilities
  * sample the ultrasonic sensor with adaptive rate (1 Hz idle -> 10 Hz active)
  * classify events locally (edge ML) and maintain occupancy
  * drive the traffic-light LEDs
  * hold and verify Kerberos-style permit tickets issued by the gateway
  * talk to the gateway only over the authenticated, encrypted channel
"""

from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

from ..config import data_path
from ..core.occupancy import BayEvent, OccupancyTracker
from ..hardware.outputs import BayLights
from ..hardware.sensor import MockSensor, UltrasonicSensor
from ..ml.classifier import EventClassifier
from ..ml.features import FEATURE_NAMES, extract_features
from ..net.transport import JsonServer, post_json
from ..security.channel import ChannelError, DeviceIdentity, HandshakeInitiator, Session
from ..security.tickets import TicketError, verify_ticket
from ..util import metric

log = logging.getLogger(__name__)
FAULT_AFTER = 10  # consecutive failed readings before declaring a sensor fault


class BayNode:
    def __init__(self, cfg: dict, sensor=None, identity: DeviceIdentity | None = None):
        self.cfg = cfg
        self.id = cfg["device_id"]
        mock = cfg["mock_hardware"]
        self.sensor = sensor or (MockSensor(cfg) if mock else UltrasonicSensor(cfg, mock=False))
        self.lights = BayLights(cfg, mock=mock)
        occ = cfg["occupancy"]
        model_path = cfg["ml"]["model_path"]
        if model_path and not os.path.isabs(model_path):
            model_path = str(data_path(cfg, model_path))
        self.classifier = EventClassifier(model_path, occ["occupied_below_cm"], occ["vacant_above_cm"])
        self.tracker = OccupancyTracker(occ, self.classifier)
        self.identity = identity or DeviceIdentity.load_or_create(self.id, data_path(cfg, "keys", "x").parent)
        self.skew = 60
        self.session: Session | None = None
        self._session_lock = threading.Lock()
        self.monitoring = True
        self.assignment: dict | None = None    # verified ticket payload
        self.unauthorised = False
        self.fault = False
        self._fail_streak = 0
        self.mode = "idle"
        self.rate_hz = 0.0
        self.started = time.time()
        self._pending: deque = deque(maxlen=200)  # events waiting for the gateway
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self.record_label = cfg["ml"].get("record_label") or ""
        self.server = JsonServer(cfg["network"]["listen_host"], cfg["network"]["listen_port"],
                                 {"/msg": self._handle_msg}, name=f"{self.id}-server")

    # ================================================================ lifecycle
    def start(self):
        self.server.start()
        threading.Thread(target=self._sensor_loop, name=f"{self.id}-sensor", daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, name=f"{self.id}-heartbeat", daemon=True).start()
        log.info("Bay node %s started (classifier: %s, mode: %s)", self.id, self.classifier.source,
                 self.cfg["bay_mode"])

    def stop(self):
        self._stop.set()
        self.server.stop()
        self.lights.close()

    # ================================================================ sensing
    def _sensor_loop(self):
        s = self.cfg["sampling"]
        last_d, active_until = None, 0.0
        win_start, win_count = time.monotonic(), 0
        cpu_start = time.process_time()
        while not self._stop.is_set():
            loop_t = time.monotonic()
            burst = 1 if self.mode == "active" else s["idle_burst"]
            d = self.sensor.read(burst=burst)
            win_count += 1
            self._update_health(d)
            if d is not None and self.monitoring:
                for ev in self.tracker.feed(d, loop_t):
                    self._on_event(ev)
            elif d is not None:
                self.tracker.last_distance = d
            # adaptive sampling: go fast while something is moving or being captured
            changed = d is not None and last_d is not None and abs(d - last_d) >= s["active_change_cm"]
            if changed or self.tracker.capturing():
                active_until = loop_t + s["active_hold_s"]
            new_mode = "active" if loop_t < active_until else "idle"
            if new_mode != self.mode:
                log.debug("sampling -> %s", new_mode)
                self.mode = new_mode
            if d is not None:
                last_d = d
            # sampling-rate / CPU metrics every 10 s (evidence for the efficiency section)
            if loop_t - win_start >= 10:
                elapsed = loop_t - win_start
                self.rate_hz = round(win_count / elapsed, 2)
                cpu = (time.process_time() - cpu_start) / elapsed * 100
                metric("sampling", bay=self.id, mode=self.mode, hz=self.rate_hz, cpu_pct=round(cpu, 2))
                win_start, win_count, cpu_start = loop_t, 0, time.process_time()
            period = s["active_period_s"] if self.mode == "active" else s["idle_period_s"]
            self._stop.wait(max(0.0, period - (time.monotonic() - loop_t)))

    def _update_health(self, d):
        if d is None:
            self._fail_streak += 1
            if self._fail_streak >= FAULT_AFTER and not self.fault:
                self.fault = True
                log.warning("Sensor fault: %d consecutive failed readings", self._fail_streak)
                self._send_event({"kind": "fault", "detail": f"{self._fail_streak} failed readings"})
                self._refresh_lights()
        else:
            if self.fault:
                self.fault = False
                log.info("Sensor recovered")
                self._send_event({"kind": "recovered"})
                self._refresh_lights()
            self._fail_streak = 0

    # ================================================================ events
    def _assignment_valid(self) -> bool:
        if self.assignment and self.assignment["exp"] > time.time():
            return True
        return False

    def _on_event(self, ev: BayEvent):
        metric("events", bay=self.id, kind=ev.kind, changed=int(ev.state_changed), conf=ev.confidence,
               source=ev.source, feature_ms=ev.feature_ms, infer_ms=ev.infer_ms)
        if self.record_label and ev.window:
            self._record(ev)
        authorised = None
        user = None
        if ev.state_changed and ev.occupied:
            authorised = self.cfg["bay_mode"] == "open" or self._assignment_valid()
            user = self.assignment["user"] if (self.assignment and authorised) else None
            self.unauthorised = not authorised
            if self.assignment:
                self.assignment["arrived"] = time.time()
        elif ev.state_changed and not ev.occupied:
            user = self.assignment["user"] if self.assignment else None
            self.unauthorised = False
            self.assignment = None          # permit consumed when the car leaves
        self._refresh_lights()
        log.info("Event: %s (conf %.2f, %s)%s", ev.kind, ev.confidence, ev.source,
                 "" if authorised is None else f" authorised={authorised}")
        payload = ev.to_dict()
        payload.update(authorised=authorised, user=user)
        self._send_event(payload)

    def _record(self, ev: BayEvent):
        """Recording mode for building the training set (label set in config / CLI)."""
        path = data_path(self.cfg, "dataset", "windows.csv")
        new = not path.exists()
        feats = extract_features(ev.window, self.tracker.occ, self.tracker.vac)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "bay", "label", "predicted", *FEATURE_NAMES, "window"])
            w.writerow([round(ev.ts, 3), self.id, self.record_label, ev.kind, *[round(x, 4) for x in feats],
                        json.dumps(ev.window)])
        log.info("Recorded window labelled '%s' (predicted %s)", self.record_label, ev.kind)

    def _refresh_lights(self):
        if self.fault:
            p = "fault"
        elif self.unauthorised and self.tracker.occupied:
            p = "unauthorised"
        elif self.tracker.occupied:
            p = "occupied"
        elif self._assignment_valid():
            p = "assigned"
        else:
            p = "vacant"
        self.lights.set(p)

    # ================================================================ state
    def state(self) -> dict:
        a = self.assignment if self._assignment_valid() else None
        return {
            "device_id": self.id,
            "occupied": self.tracker.occupied,
            "distance": self.tracker.last_distance,
            "monitoring": self.monitoring,
            "occupied_below_cm": self.tracker.occ,
            "vacant_above_cm": self.tracker.vac,
            "fault": self.fault,
            "unauthorised": self.unauthorised,
            "assignment": {k: a[k] for k in ("user", "exp", "tid", "iat")} | {"arrived": a.get("arrived")} if a else None,
            "lights": self.lights.pattern,
            "mode": self.mode,
            "rate_hz": self.rate_hz,
            "classifier": self.classifier.source,
            "stats": dict(self.tracker.stats),
            "uptime_s": int(time.time() - self.started),
            "occupied_since": self.tracker.occupied_since,
            "bay_mode": self.cfg["bay_mode"],
        }

    # ================================================================ gateway link
    def _handshake(self) -> bool:
        try:
            ini = HandshakeInitiator(self.identity, "gateway", self.skew)
            status, resp = post_json(self.cfg["gateway"], "/handshake", ini.hello(), timeout=8)
            if status != 200:
                log.warning("Handshake rejected by gateway: %s", resp.get("error"))
                return False
            self.session = ini.finish(resp)
            log.info("Secure session established with gateway (sid %s...)", self.session.sid[:6])
            return True
        except (ConnectionError, ChannelError, KeyError) as e:
            log.warning("Handshake failed: %s", e)
            return False

    def _send(self, obj: dict) -> dict | None:
        with self._session_lock:
            for _ in range(2):
                if self.session is None and not self._handshake():
                    return None
                env = self.session.seal(obj)
                try:
                    status, resp = post_json(self.cfg["gateway"], "/msg", {"from": self.id, "env": env})
                except ConnectionError as e:
                    log.warning("Gateway unreachable: %s", e)
                    return None
                if status == 401:           # gateway restarted -> new handshake
                    self.session = None
                    continue
                if status != 200:
                    log.warning("Gateway error %s: %s", status, resp.get("error"))
                    return None
                try:
                    return self.session.open(resp["env"])
                except (ChannelError, KeyError) as e:
                    log.warning("Bad reply from gateway: %s", e)
                    return None
        return None

    def _send_event(self, payload: dict):
        payload.setdefault("ts", time.time())
        self._pending.append(payload)
        threading.Thread(target=self._flush_pending, daemon=True).start()

    def _flush_pending(self):
        """Send queued events in order. Only one thread flushes at a time; the
        outer loop re-checks after releasing so no event is left behind."""
        while self._pending:
            if not self._flush_lock.acquire(blocking=False):
                return
            try:
                while self._pending:
                    if self._send({"type": "event", "event": self._pending[0], "state": self.state()}) is None:
                        return  # gateway unreachable - heartbeat loop retries later
                    self._pending.popleft()
            finally:
                self._flush_lock.release()

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            self._send({"type": "heartbeat", "state": self.state()})
            if self._pending:
                self._flush_pending()
            if self.assignment and not self._assignment_valid() and not self.tracker.occupied:
                log.info("Permit for %s expired unused", self.assignment.get("user"))
                self.assignment = None
                self._refresh_lights()
            self._stop.wait(self.cfg["heartbeat_s"])

    # ================================================================ commands from gateway
    def _handle_msg(self, body: dict, ip: str) -> tuple[int, dict]:
        sess = self.session
        if sess is None or body.get("from") != "gateway" or body.get("env", {}).get("sid") != sess.sid:
            return 401, {"error": "no_session"}
        try:
            cmd = sess.open(body["env"])
        except ChannelError as e:
            log.warning("Rejected message from %s: %s", ip, e)
            return 403, {"error": "rejected"}
        reply = self._execute(cmd)
        reply["state"] = self.state()
        return 200, {"env": sess.seal(reply)}

    def _execute(self, cmd: dict) -> dict:
        c = cmd.get("cmd")
        if c == "status":
            st = self.state()
            occ = "FAULT" if st["fault"] else ("OCCUPIED" if st["occupied"] else "VACANT")
            return {"ok": True, "message": f"{self.id}: {occ} | {st['distance']} cm | "
                                           f"monitoring {'ON' if self.monitoring else 'OFF'} | {st['rate_hz']} Hz"}
        if c == "enable":
            self.monitoring = True
            return {"ok": True, "message": f"{self.id}: monitoring ENABLED"}
        if c == "disable":
            self.monitoring = False
            return {"ok": True, "message": f"{self.id}: monitoring DISABLED"}
        if c == "threshold":
            try:
                self.tracker.set_threshold(float(cmd["value"]))
            except (KeyError, ValueError) as e:
                return {"ok": False, "message": f"{self.id}: invalid threshold ({e})"}
            return {"ok": True, "message": f"{self.id}: occupied below {self.tracker.occ} cm, "
                                           f"vacant above {self.tracker.vac:.1f} cm"}
        if c == "assign":
            return self._assign(cmd.get("ticket", {}))
        if c == "revoke":
            if self.assignment and (not cmd.get("tid") or cmd["tid"] == self.assignment.get("tid")):
                self.assignment = None
                self._refresh_lights()
                return {"ok": True, "message": f"{self.id}: permit revoked"}
            return {"ok": False, "message": f"{self.id}: no matching permit"}
        if c == "lights_test":
            threading.Thread(target=self._lights_test, daemon=True).start()
            return {"ok": True, "message": f"{self.id}: running LED test"}
        return {"ok": False, "message": f"{self.id}: unknown command"}

    def _assign(self, ticket: dict) -> dict:
        gw_key = self.identity.trusted_key("gateway")
        try:
            payload = verify_ticket(gw_key, ticket, self.id, self.skew)
        except TicketError as e:
            log.warning("Rejected permit ticket: %s", e)
            return {"ok": False, "message": f"{self.id}: ticket rejected ({e})"}
        same_user = self.assignment and self.assignment.get("user") == payload["user"]
        if self.tracker.occupied and not same_user:
            return {"ok": False, "message": f"{self.id}: bay is occupied"}
        arrived = self.assignment.get("arrived") if same_user else None
        self.assignment = dict(payload, arrived=arrived)
        if same_user and self.tracker.occupied:
            self.unauthorised = False
        self._refresh_lights()
        log.info("Permit accepted for %s until %s", payload["user"], time.strftime("%H:%M", time.localtime(payload["exp"])))
        return {"ok": True, "message": f"{self.id}: permit accepted"}

    def _lights_test(self):
        prev = self.lights.pattern
        for p in ("vacant", "assigned", "occupied", "unauthorised", "fault"):
            self.lights.set(p)
            time.sleep(1.2)
        self.lights.set(prev)
