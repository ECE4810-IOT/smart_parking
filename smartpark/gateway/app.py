"""Gateway (fog node): the brain of the system.

  * terminates the secure channel from every bay node (handshake + envelopes)
  * runs the OTP access flow for the kiosk and issues permit tickets
  * turns bay events into Telegram notifications / alerts
  * monitors node health, permits (no-show, overstay) and car-park capacity
  * aggregates everything into one ThingSpeak channel
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from ..config import data_path
from ..hardware.outputs import Barrier
from ..net.telegram import FakeTelegram, TelegramClient
from ..net.thingspeak import ThingSpeakUploader
from ..net.transport import JsonServer, post_json
from ..security.auth import AccessManager
from ..security.channel import ChannelError, DeviceIdentity, HandshakeResponder, Session
from ..security.crypto import load_private_pem
from ..security.tickets import issue_ticket
from ..util import fmt_duration, hhmm, in_time_window, metric
from .analytics import EventStore
from .commands import CommandRouter

log = logging.getLogger(__name__)

KIND_TEXT = {"arrival": "car parked", "departure": "car left", "transient": "movement (passing object)",
             "noise": "sensor noise"}


def label(bay_id: str) -> str:
    return "Bay " + bay_id[3:] if bay_id.startswith("bay") else bay_id


@dataclass
class Node:
    id: str
    address: str
    session: Session | None = None
    last_seen: float = 0.0
    state: dict = field(default_factory=dict)
    online: bool = False
    ever_seen: bool = False


class Gateway:
    def __init__(self, cfg: dict, vault, telegram=None, fake_cloud: bool = False):
        self.cfg = cfg
        self.vault = vault
        self.id = cfg["device_id"]
        mock = cfg["mock_hardware"]
        keys_dir = data_path(cfg, "keys", "x").parent
        self.identity = DeviceIdentity(self.id, keys_dir,
                                       private_key=load_private_pem(vault.data["gateway_private_key"].encode()))
        skew = cfg["security"]["max_clock_skew_s"]
        self.responder = HandshakeResponder(self.identity, skew)
        self.access = AccessManager(vault, cfg["access"])
        self.store = EventStore(data_path(cfg, "events.db"))
        self.admin_chat = str(vault.secret("admin_chat_id") or "")
        self.commands = CommandRouter(self)
        token = vault.secret("telegram_token")
        if telegram is not None:
            self.tg = telegram
            self.tg.on_update = self.commands.handle
        elif cfg["telegram"]["enabled"] and token and not fake_cloud:
            t = cfg["telegram"]
            self.tg = TelegramClient(token, data_path(cfg, "outbox.db"), poll=True,
                                     poll_timeout_s=t["poll_timeout_s"], coalesce_window_s=t["coalesce_window_s"],
                                     on_update=self.commands.handle)
        else:
            self.tg = FakeTelegram(on_update=self.commands.handle)
        self.barrier = Barrier(cfg, mock=mock)
        self.barrier.listeners.append(lambda o: self.feed(f"Barrier {'opened' if o else 'closed'}", "info"))
        self.thingspeak = None
        key = vault.secret("thingspeak_write_key")
        if cfg["thingspeak"]["enabled"] and (key or fake_cloud):
            self.thingspeak = ThingSpeakUploader(key or "", vault.secret("thingspeak_channel_id"),
                                                 cfg["thingspeak"]["interval_s"], self.thingspeak_snapshot,
                                                 data_path(cfg, "outbox.db"), fake=fake_cloud)
        self.nodes: dict[str, Node] = {bid: Node(bid, addr) for bid, addr in sorted(cfg["bays"].items())}
        self.assignments: dict[str, dict] = {}
        self.night_mode = bool(cfg["night_mode"]["enabled"])
        self.notify_transients = True
        self.feed_items: deque = deque(maxlen=30)
        self._counters = {"events": 0, "alerts": 0, "dwell": []}
        self._otp_msgs: dict[str, tuple[str, int]] = {}
        self._unknown_attempts: deque = deque()
        self._full_alerted = False
        self._fault_state: dict[str, dict] = {}
        self._last_rate_alert = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.started = time.time()
        self.server = JsonServer(cfg["network"]["listen_host"], cfg["network"]["listen_port"],
                                 {"/handshake": self._handle_handshake, "/msg": self._handle_msg},
                                 name="gateway-server")

    # ================================================================ lifecycle
    def start(self):
        self.server.start()
        self.tg.start()
        if self.thingspeak:
            self.thingspeak.start()
        threading.Thread(target=self._monitor_loop, name="gw-monitor", daemon=True).start()
        self.notify_admin(f"SmartPark gateway online - {len(self.nodes)} bay(s) configured. Send /menu for controls.")
        self.feed("Gateway started", "info")

    def stop(self):
        self._stop.set()
        self.server.stop()
        self.tg.stop()
        if self.thingspeak:
            self.thingspeak.stop()

    # ================================================================ messaging helpers
    def feed(self, text: str, level: str = "info"):
        self.feed_items.appendleft({"ts": time.time(), "time": hhmm(), "text": text, "level": level})

    def notify_admin(self, text: str, coalesce: bool = False, kind: str = "info"):
        if self.admin_chat:
            self.tg.send(self.admin_chat, text, kind=kind, coalesce=coalesce)

    def notify_user(self, username: str | None, text: str, kind: str = "user"):
        chat = self.access.chat_for_user(username) if username else None
        if chat:
            self.tg.send(chat, text, kind=kind)

    def alert(self, text: str, urgent: bool = True):
        self._counters["alerts"] += 1
        self.store.add_alert("alert" if urgent else "warn", text)
        self.feed(text, "alert" if urgent else "warn")
        log.warning("ALERT: %s", text)
        self.notify_admin(("ALERT: " if urgent else "") + text, coalesce=not urgent, kind="alert")

    def is_night(self) -> bool:
        n = self.cfg["night_mode"]
        return self.night_mode and in_time_window(n["start"], n["end"])

    # ================================================================ secure channel endpoints
    def _handle_handshake(self, body: dict, ip: str) -> tuple[int, dict]:
        try:
            welcome, session = self.responder.respond(body)
        except ChannelError as e:
            log.warning("Handshake from %s rejected: %s", ip, e)
            self.store.add_alert("security", f"Rejected handshake from {ip}: {e}")
            return 403, {"error": "rejected"}
        node = self.nodes.get(session.peer)
        if node is None:
            return 403, {"error": "rejected"}
        with self._lock:
            node.session = session
            node.last_seen = time.time()
        log.info("Secure session with %s (%s)", node.id, ip)
        if node.id in self.assignments:
            # the bay rebooted and lost its permit -> re-issue it once the session is up
            t = threading.Timer(1.5, self._reissue_permit, args=(node.id,))
            t.daemon = True
            t.start()
        return 200, welcome

    def _reissue_permit(self, bay: str):
        a = self.assignments.get(bay)
        if not a or a["exp"] <= time.time():
            return
        if (self.nodes[bay].state.get("assignment") or {}).get("tid") == a["tid"]:
            return
        ticket = issue_ticket(self.identity.private_key, a["user"], bay, int(a["exp"] - time.time()))
        reply = self.send_command(bay, {"cmd": "assign", "ticket": ticket})
        if reply.get("ok"):
            a["tid"] = ticket["payload"]["tid"]
            log.info("Re-issued permit for %s to %s after reconnect", a["user"], bay)

    def _handle_msg(self, body: dict, ip: str) -> tuple[int, dict]:
        node = self.nodes.get(body.get("from", ""))
        env = body.get("env") or {}
        if node is None or node.session is None or env.get("sid") != node.session.sid:
            return 401, {"error": "no_session"}
        try:
            msg = node.session.open(env)
        except ChannelError as e:
            log.warning("Rejected message from %s (%s): %s", node.id, ip, e)
            self.store.add_alert("security", f"Rejected message claiming to be {node.id} from {ip}: {e}")
            return 403, {"error": "rejected"}
        self._touch(node, msg.get("state") or {})
        if msg.get("type") == "event":
            try:
                self._handle_event(node, msg.get("event") or {})
            except Exception:
                log.exception("event handling failed")
        return 200, {"env": node.session.seal({"ok": True})}

    def _touch(self, node: Node, state: dict):
        with self._lock:
            was_online = node.online
            node.last_seen = time.time()
            node.online = True
            if state:
                node.state = state
            a = state.get("assignment") if state else None
            if a and node.id not in self.assignments and a.get("exp", 0) > time.time():
                # gateway restarted: rebuild permit from the bay's verified ticket
                self.assignments[node.id] = {"user": a["user"], "tid": a["tid"], "exp": a["exp"],
                                             "issued": a.get("iat", time.time()), "arrived": a.get("arrived"),
                                             "reminded": False, "overstay": False}
        if not was_online:
            if node.ever_seen:
                self.alert(f"{label(node.id)} is back online", urgent=False)
            else:
                self.feed(f"{label(node.id)} connected", "info")
            node.ever_seen = True

    # ================================================================ commands to bays
    def send_command(self, bay_id: str, cmd: dict, timeout: float = 5.0) -> dict:
        node = self.nodes.get(bay_id)
        if node is None:
            return {"ok": False, "message": f"{bay_id}: unknown bay"}
        if node.session is None:
            return {"ok": False, "message": f"{bay_id}: not connected"}
        try:
            status, resp = post_json(node.address, "/msg", {"from": self.id, "env": node.session.seal(cmd)},
                                     timeout=timeout)
            if status == 401:
                node.session = None
                return {"ok": False, "message": f"{bay_id}: session expired (bay will reconnect)"}
            if status != 200:
                return {"ok": False, "message": f"{bay_id}: error {status}"}
            reply = node.session.open(resp["env"])
            self._touch(node, reply.get("state") or {})
            return reply
        except (ConnectionError, ChannelError, KeyError) as e:
            return {"ok": False, "message": f"{bay_id}: UNREACHABLE ({e.__class__.__name__})"}

    # ================================================================ bay events
    def _handle_event(self, node: Node, ev: dict):
        kind = ev.get("kind")
        bay = node.id
        name = label(bay)
        if kind in ("fault", "recovered"):
            self._sensor_health_event(bay, kind, ev)
            return
        self.store.add_event(bay, ev)
        conf = ev.get("confidence", 0)
        src = ev.get("source", "?")
        tag = f"[{src} {conf:.2f}]" if src != "reconciled" else "[reconciled]"
        night = self.is_night()
        if ev.get("state_changed"):
            self._counters["events"] += 1
        if ev.get("state_changed") and ev.get("occupied"):
            user = ev.get("user")
            if ev.get("authorised") is False:
                self.alert(f"Unauthorised vehicle in {name} (no valid permit) {tag}")
            else:
                self.feed(f"{name}: car parked" + (f" ({user})" if user else ""), "event")
                self.notify_admin(f"{name}: car parked{f' by {user}' if user else ''} {tag}", coalesce=True)
                a = self.assignments.get(bay)
                if a:
                    a["arrived"] = time.time()
                    self.notify_user(user, f"Parked at {name} at {hhmm()}. Your permit runs until "
                                           f"{hhmm(a['exp'])}. Send /extend 1 for more time.")
            if night:
                self.alert(f"Vehicle arrived at {name} during closed hours")
        elif ev.get("state_changed"):
            dwell = ev.get("dwell_s")
            if dwell:
                self._counters["dwell"].append(dwell)
            stay = f" after {fmt_duration(dwell)}" if dwell else ""
            self.feed(f"{name}: car left{stay}", "event")
            self.notify_admin(f"{name}: car left{stay} {tag}", coalesce=True)
            a = self.assignments.pop(bay, None)
            if a:
                self.notify_user(a["user"], f"You left {name}{stay}. Thanks for parking with SmartPark.")
        elif kind == "transient":
            if night:
                self.alert(f"Movement detected at {name} during closed hours {tag}")
            else:
                self.feed(f"{name}: movement detected - ignored (not a car)", "info")
                if self.notify_transients:
                    self.notify_admin(f"{name}: movement detected (passing object, not counted) {tag}",
                                      coalesce=True)
        else:
            self.feed(f"{name}: {KIND_TEXT.get(kind, kind)} ignored", "info")
        self._check_capacity()

    def _sensor_health_event(self, bay: str, kind: str, ev: dict):
        """Fault alerts are rate-limited per bay (max one Telegram alert per 10 min, and a
        'recovered' message only if the fault alert was actually sent), so a flaky sensor or a
        backlog of queued events cannot flood Telegram."""
        name = label(bay)
        now = time.time()
        st = self._fault_state.setdefault(bay, {"last_alert": 0.0, "alerted": False, "suppressed": 0})
        if kind == "fault":
            if now - st["last_alert"] >= 600:
                extra = f", {st['suppressed']} more since last alert" if st["suppressed"] else ""
                self.alert(f"{name}: sensor fault ({ev.get('detail', '')}{extra}) - check wiring")
                st.update(last_alert=now, alerted=True, suppressed=0)
            else:
                st["suppressed"] += 1
                self.feed(f"{name}: sensor fault (repeat)", "warn")
        else:
            if st["alerted"]:
                self.alert(f"{name}: sensor recovered", urgent=False)
                st["alerted"] = False
            else:
                self.feed(f"{name}: sensor recovered", "info")

    # ================================================================ capacity / bays
    def bay_status(self, bay_id: str) -> str:
        n = self.nodes[bay_id]
        st = n.state
        if not n.online:
            return "offline"
        if st.get("fault"):
            return "fault"
        if st.get("occupied"):
            return "unauthorised" if st.get("unauthorised") else "occupied"
        if bay_id in self.assignments:
            return "assigned"
        return "vacant"

    def free_bays(self) -> list[str]:
        return [b for b in self.nodes if self.bay_status(b) == "vacant"]

    def _check_capacity(self):
        free = len(self.free_bays())
        online = sum(n.online for n in self.nodes.values())
        if free == 0 and online and not self._full_alerted:
            self._full_alerted = True
            self.alert("Car park FULL - kiosk will refuse new entries", urgent=False)
        elif free > 0 and self._full_alerted:
            self._full_alerted = False
            self.alert(f"Spaces available again ({free} free)", urgent=False)

    # ================================================================ monitor loop
    def _monitor_loop(self):
        acfg = self.cfg["access"]
        timeout = self.cfg["security"]["heartbeat_timeout_s"]
        while not self._stop.wait(5):
            now = time.time()
            for node in self.nodes.values():
                if node.online and now - node.last_seen > timeout:
                    node.online = False
                    self.alert(f"{label(node.id)} OFFLINE - no heartbeat for {int(now - node.last_seen)} s")
            for bay, a in list(self.assignments.items()):
                status = self.bay_status(bay)
                if a["arrived"] is None and now > a["issued"] + acfg["no_show_min"] * 60:
                    self.send_command(bay, {"cmd": "revoke", "tid": a["tid"]})
                    self.assignments.pop(bay, None)
                    self.feed(f"{label(bay)}: permit for {a['user']} released (no-show)", "warn")
                    self.notify_user(a["user"], f"Your reservation for {label(bay)} was released because "
                                                f"no car arrived within {acfg['no_show_min']} min.")
                    continue
                if a["arrived"] and not a["reminded"] and now > a["exp"] - acfg["overstay_reminder_min"] * 60:
                    a["reminded"] = True
                    self.notify_user(a["user"], f"Reminder: your permit for {label(bay)} ends at {hhmm(a['exp'])}. "
                                                f"Send /extend 1 to add an hour.")
                if now > a["exp"] and not a["overstay"] and status in ("occupied", "unauthorised"):
                    a["overstay"] = True
                    self.alert(f"Overstay: {a['user']} in {label(bay)} - permit expired at {hhmm(a['exp'])}",
                               urgent=False)
                    self.notify_user(a["user"], f"Your permit for {label(bay)} has expired. Please move your "
                                                f"car or send /extend 1.")
            self._check_capacity()

    # ================================================================ access flow (kiosk)
    def request_access(self, username: str) -> dict:
        t0 = time.perf_counter()
        if not self.free_bays() and not any(a["user"] == username.strip().lower()
                                            for a in self.assignments.values()):
            metric("auth", stage="request", outcome="full", ms=0)
            return {"status": "full"}
        out = self.access.request_otp(username)
        if out.deliver_to:
            rid = out.request_id

            def remember(mid, rid=rid, chat=out.deliver_to):
                if mid:
                    self._otp_msgs[rid] = (chat, mid)
                    p = self.access.pending_request(rid)
                    if p:
                        p.delivered_ms = (time.perf_counter() - t0) * 1000
                        metric("auth", stage="otp_delivered", outcome="ok", ms=round(p.delivered_ms, 1))
            spaced = " ".join(out.cipher)
            self.tg.send(out.deliver_to,
                         f"SmartPark access code: {spaced}\n\nEnter this code AND your 6-digit PIN at the kiosk "
                         f"within {out.expires_in} s. The kiosk decodes it - never share your PIN.\n"
                         f"Didn't request this? Ignore it and tell the car park admin.",
                         kind="otp", ttl_s=out.expires_in, on_sent=remember)
            timer = threading.Timer(out.expires_in + 2, self._delete_otp_msg, args=(rid,))
            timer.daemon = True
            timer.start()
        elif out.reason == "locked":
            self.alert(f"Kiosk access attempt for LOCKED user '{out.username}'", urgent=False)
        elif out.reason == "unknown_user":
            now = time.time()
            self._unknown_attempts.append(now)
            while self._unknown_attempts and self._unknown_attempts[0] < now - 300:
                self._unknown_attempts.popleft()
            if len(self._unknown_attempts) == 3:
                self.alert("3 unknown usernames tried at the kiosk within 5 min (possible guessing attack)")
        elif out.reason == "global_rate_limit" and time.time() - self._last_rate_alert > 60:
            self._last_rate_alert = time.time()
            self.alert("Kiosk request rate limit hit (possible abuse)")
        metric("auth", stage="request", outcome=out.reason, ms=round((time.perf_counter() - t0) * 1000, 2))
        # identical answer for every case -> kiosk cannot be used to discover usernames
        return {"status": "sent", "request_id": out.request_id, "expires_in": out.expires_in}

    def _delete_otp_msg(self, rid: str):
        item = self._otp_msgs.pop(rid, None)
        if item:
            self.tg.delete(*item)

    def verify_access(self, request_id: str, code: str, pin: str) -> dict:
        t0 = time.perf_counter()
        res = self.access.verify(request_id, code, pin)
        if not res.ok:
            metric("auth", stage="verify", outcome=res.reason, ms=round((time.perf_counter() - t0) * 1000, 2))
            if res.locked and res.username:
                self.alert(f"User '{res.username}' locked after repeated wrong codes at the kiosk")
                self.notify_user(res.username, "Security notice: your access code was entered wrongly several "
                                               "times at the kiosk. Your account is temporarily locked. If this "
                                               "wasn't you, contact the car park admin.")
            return {"ok": False, "attempts_left": res.attempts_left, "locked": res.locked, "expired": res.expired}
        self._delete_otp_msg(request_id)
        user = res.username
        grant = self._grant_bay(user)
        ms = round((time.perf_counter() - t0) * 1000, 2)
        metric("auth", stage="verify", outcome="ok" if grant else "no_bay", ms=ms)
        if not grant:
            self.notify_user(user, "Access code accepted, but no bay could be reserved (car park full or bays "
                                   "offline). Please contact the admin.")
            return {"ok": False, "attempts_left": 0, "locked": False, "expired": False, "no_bay": True}
        bay, exp = grant
        self.barrier.open()
        self.feed(f"Access granted to {user} -> {label(bay)}", "event")
        self.notify_user(user, f"Access granted. Proceed to {label(bay)} (yellow light). Permit valid until "
                               f"{hhmm(exp)}. Commands: /mybay /extend /free")
        self.notify_admin(f"Access granted: {user} -> {label(bay)} until {hhmm(exp)}", coalesce=True)
        return {"ok": True, "user": user, "bay": bay, "bay_label": label(bay), "until": hhmm(exp)}

    def _grant_bay(self, user: str, lifetime_s: int | None = None) -> tuple[str, float] | None:
        lifetime_s = lifetime_s or self.cfg["access"]["default_stay_min"] * 60
        existing = [b for b, a in self.assignments.items() if a["user"] == user]
        candidates = existing + [b for b in self.free_bays() if b not in existing]
        for bay in candidates:
            ticket = issue_ticket(self.identity.private_key, user, bay, lifetime_s)
            reply = self.send_command(bay, {"cmd": "assign", "ticket": ticket})
            if reply.get("ok"):
                p = ticket["payload"]
                prev = self.assignments.get(bay, {})
                self.assignments[bay] = {"user": user, "tid": p["tid"], "exp": p["exp"], "issued": p["iat"],
                                         "arrived": prev.get("arrived") if prev.get("user") == user else None,
                                         "reminded": False, "overstay": False}
                return bay, p["exp"]
            log.warning("Could not assign %s: %s", bay, reply.get("message"))
        return None

    def extend(self, user: str, hours: float) -> str:
        bay = next((b for b, a in self.assignments.items() if a["user"] == user), None)
        if not bay:
            return "You have no active permit."
        a = self.assignments[bay]
        remaining = max(0, a["exp"] - time.time())
        new_life = int(remaining + hours * 3600)
        if new_life > 8 * 3600:
            return "Permits are limited to 8 hours."
        ticket = issue_ticket(self.identity.private_key, user, bay, new_life)
        reply = self.send_command(bay, {"cmd": "assign", "ticket": ticket})
        if not reply.get("ok"):
            return f"Could not extend: {reply.get('message')}"
        a.update(tid=ticket["payload"]["tid"], exp=ticket["payload"]["exp"], reminded=False, overstay=False)
        return f"Permit for {label(bay)} extended until {hhmm(a['exp'])}."

    # ================================================================ snapshots
    def kiosk_state(self) -> dict:
        bays = [{"id": b, "label": label(b), "status": self.bay_status(b)} for b in self.nodes]
        return {"locked": False, "bays": bays, "free": len(self.free_bays()), "total": len(bays),
                "night_mode": self.night_mode, "barrier_open": self.barrier.is_open,
                "feed": list(self.feed_items)[:10], "telegram_online": getattr(self.tg, "online", True)}

    def thingspeak_snapshot(self) -> dict:
        code = {"vacant": 0, "occupied": 1, "assigned": 2, "unauthorised": 3, "offline": -1, "fault": -1}
        fields = {}
        for i, b in enumerate(list(self.nodes)[:4], start=1):
            fields[f"field{i}"] = code[self.bay_status(b)]
        fields["field5"] = len(self.free_bays())
        fields["field6"] = self._counters["events"]
        fields["field7"] = self._counters["alerts"]
        d = self._counters["dwell"]
        if d:
            fields["field8"] = round(sum(d) / len(d) / 60, 2)
        self._counters = {"events": 0, "alerts": 0, "dwell": []}
        return fields

    def report(self) -> dict:
        return self.store.report(list(self.nodes))
