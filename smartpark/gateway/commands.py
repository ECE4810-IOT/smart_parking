"""Telegram command router with role-based access control.

Roles are decided by the sender's chat id (Telegram authenticates the chat):
    admin      chat id stored in the vault as admin_chat_id
    user       a registered, active user whose Telegram is linked
    stranger   anyone else - may only /register or ask for /help
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from ..util import fmt_duration, hhmm, now_str

if TYPE_CHECKING:  # pragma: no cover
    from .app import Gateway

log = logging.getLogger(__name__)

ADMIN_HELP = """Admin commands
/menu - button panel
/status <bay|all> - live bay status
/report - today's analytics
/enable <bay|all>, /disable <bay|all> - pause/resume monitoring
/threshold <bay|all> <cm> - set the occupied threshold
/adduser <name> - create a user (returns enrolment code)
/removeuser <name>, /users, /unlockuser <name>
/resetpin <name> - new enrolment code so the user can choose a new PIN
/night on|off - closed-hours security mode
/open - open the barrier manually
/alerts - last 10 alerts
/lights <bay|all> - LED self-test"""

USER_HELP = """Commands
/free - free bays right now
/mybay - your current permit
/extend <hours> - extend your permit (1-4)
/help - this message"""

STRANGER_HELP = """SmartPark bot.
To join: ask the admin for an enrolment code, then send
/register <username> <code>"""


class CommandRouter:
    def __init__(self, gw: "Gateway"):
        self.gw = gw
        self._reg_attempts: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    def handle(self, update: dict):
        if "callback_query" in update:
            cq = update["callback_query"]
            chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            self.gw.tg.answer_callback(cq.get("id", ""))
            data = cq.get("data", "")
            if data.startswith("cmd:"):
                self._dispatch(chat, data[4:])
            return
        msg = update.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if chat and text:
            self._dispatch(chat, text)

    def _reply(self, chat: str, text: str, buttons: list[list[tuple[str, str]]] | None = None):
        markup = None
        if buttons:
            markup = {"inline_keyboard": [[{"text": t, "callback_data": f"cmd:{c}"} for t, c in row]
                                          for row in buttons]}
        self.gw.tg.send(chat, text, kind="reply", reply_markup=markup)

    def _dispatch(self, chat: str, text: str):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        gw = self.gw
        is_admin = bool(gw.admin_chat) and chat == gw.admin_chat
        user = gw.access.user_for_chat(chat)
        log.info("Telegram command from %s: %s", "admin" if is_admin else (user or "stranger"),
                 cmd if cmd != "/register" else "/register ***")
        if is_admin:
            fn = getattr(self, "admin_" + cmd.lstrip("/"), None)
            if fn:
                return fn(chat, args)
        if user:
            fn = getattr(self, "user_" + cmd.lstrip("/"), None)
            if fn:
                return fn(chat, user, args)
        if cmd == "/register":
            return self.register(chat, args)
        if is_admin:
            return self._reply(chat, ADMIN_HELP + "\n\n" + USER_HELP)
        if user:
            return self._reply(chat, USER_HELP)
        return self._reply(chat, STRANGER_HELP)

    # ------------------------------------------------------------ helpers
    def _targets(self, chat, args) -> list[str] | None:
        if not args:
            self._reply(chat, f"Specify a bay: {'|'.join(self.gw.nodes)}|all")
            return None
        t = args[0].lower()
        if t == "all":
            return list(self.gw.nodes)
        if t in self.gw.nodes:
            return [t]
        self._reply(chat, f"Unknown bay '{t}'. Options: {'|'.join(self.gw.nodes)}|all")
        return None

    def _fanout(self, targets, cmd: dict) -> list[dict]:
        """Send to several bays in parallel so /status all is fast."""
        results: dict[str, dict] = {}

        def run(b):
            results[b] = self.gw.send_command(b, cmd)
        threads = [threading.Thread(target=run, args=(b,)) for b in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(8)
        return [results.get(b, {"message": f"{b}: timeout"}) for b in targets]

    # ------------------------------------------------------------ admin
    def admin_menu(self, chat, args):
        self._reply(chat, "SmartPark control panel", [
            [("Status", "/status all"), ("Report", "/report")],
            [("Alerts", "/alerts"), ("Users", "/users")],
            [("Night ON", "/night on"), ("Night OFF", "/night off")],
            [("Open barrier", "/open"), ("LED test", "/lights all")],
        ])

    admin_start = admin_menu

    def admin_help(self, chat, args):
        self._reply(chat, ADMIN_HELP + "\n\n" + USER_HELP)

    def admin_status(self, chat, args):
        targets = self._targets(chat, args or ["all"])
        if not targets:
            return
        lines = [r.get("message", "?") for r in self._fanout(targets, {"cmd": "status"})]
        free = len(self.gw.free_bays())
        lines.append(f"\n{free} of {len(self.gw.nodes)} bay(s) free")
        for b, a in self.gw.assignments.items():
            lines.append(f"{b}: permit {a['user']} until {hhmm(a['exp'])}")
        self._reply(chat, "\n".join(lines))

    def _simple(self, chat, args, cmd):
        targets = self._targets(chat, args)
        if targets:
            self._reply(chat, "\n".join(r.get("message", "?") for r in self._fanout(targets, cmd)))

    def admin_enable(self, chat, args):
        self._simple(chat, args, {"cmd": "enable"})

    def admin_disable(self, chat, args):
        self._simple(chat, args, {"cmd": "disable"})

    def admin_lights(self, chat, args):
        self._simple(chat, args or ["all"], {"cmd": "lights_test"})

    def admin_threshold(self, chat, args):
        if len(args) != 2:
            return self._reply(chat, "Usage: /threshold <bay|all> <cm>")
        try:
            value = float(args[1])
        except ValueError:
            return self._reply(chat, "Threshold must be a number (cm)")
        self._simple(chat, args[:1], {"cmd": "threshold", "value": value})

    def admin_report(self, chat, args):
        r = self.gw.report()
        occ = ", ".join(f"{b} {p}%" for b, p in r["occupancy_pct"].items())
        turn = ", ".join(f"{b} {n}" for b, n in r["turnover_per_bay"].items())
        self._reply(chat, "\n".join([
            f"SmartPark report (today, {now_str()})",
            f"Arrivals: {r['arrivals']}   Departures: {r['departures']}",
            f"Average occupancy: {r['avg_occupancy_pct']}%  ({occ})",
            f"Turnover (cars/bay): {turn}",
            f"Average stay: {r['avg_dwell_min'] if r['avg_dwell_min'] is not None else '-'} min "
            f"(max {r['max_dwell_min'] if r['max_dwell_min'] is not None else '-'})",
            f"Peak arrival hour: {r['peak_hour'] or '-'}",
            f"Unauthorised parking: {r['unauthorised']}",
            f"False alarms avoided by ML: {r['transients_ignored']} movement, {r['noise_ignored']} noise",
            f"Classifier corrections (reconciled): {r['reconciled']}",
            f"Avg edge inference time: {r['avg_infer_ms'] if r['avg_infer_ms'] is not None else '-'} ms",
        ]))

    def admin_adduser(self, chat, args):
        if len(args) != 1:
            return self._reply(chat, "Usage: /adduser <username>")
        try:
            code = self.gw.access.create_user(args[0])
        except ValueError as e:
            return self._reply(chat, f"Could not add user: {e}")
        ttl = self.gw.cfg["access"]["enrol_code_ttl_min"]
        self._reply(chat, f"User '{args[0].lower()}' created.\nEnrolment code: {code} (valid {ttl} min)\n\n"
                          f"Give this code to the user IN PERSON. They then:\n"
                          f"1) send /register {args[0].lower()} {code} to this bot\n"
                          f"2) choose their PIN at the kiosk (New user setup)")

    def admin_removeuser(self, chat, args):
        if len(args) != 1:
            return self._reply(chat, "Usage: /removeuser <username>")
        ok = self.gw.access.remove_user(args[0])
        self._reply(chat, f"User '{args[0]}' removed." if ok else "No such user.")

    def admin_users(self, chat, args):
        users = self.gw.access.list_users()
        if not users:
            return self._reply(chat, "No users yet. Add one with /adduser <name>.")
        lines = [f"{u['username']}: {u['status']}{' | telegram' if u['telegram'] else ''}"
                 f"{' | LOCKED' if u['locked'] else ''}" for u in users]
        self._reply(chat, "Users\n" + "\n".join(lines))

    def admin_resetpin(self, chat, args):
        if len(args) != 1:
            return self._reply(chat, "Usage: /resetpin <username>")
        try:
            code = self.gw.access.reset_pin(args[0])
        except ValueError as e:
            return self._reply(chat, f"Could not reset: {e}")
        ttl = self.gw.cfg["access"]["enrol_code_ttl_min"]
        self._reply(chat, f"PIN reset for '{args[0].lower()}'. New enrolment code: {code} (valid {ttl} min).\n"
                          f"Their Telegram stays linked - they just do 'New user setup' at the kiosk again "
                          f"with this code and choose a new PIN.")

    def admin_unlockuser(self, chat, args):
        if len(args) != 1:
            return self._reply(chat, "Usage: /unlockuser <username>")
        self.gw.access.unlock_user(args[0])
        self._reply(chat, f"Lockout cleared for '{args[0]}'.")

    def admin_night(self, chat, args):
        if not args or args[0].lower() not in ("on", "off"):
            return self._reply(chat, f"Night mode is {'ON' if self.gw.night_mode else 'OFF'}. Use /night on|off")
        self.gw.night_mode = args[0].lower() == "on"
        n = self.gw.cfg["night_mode"]
        self.gw.feed(f"Night mode {'enabled' if self.gw.night_mode else 'disabled'}", "info")
        self._reply(chat, f"Night mode {'ON' if self.gw.night_mode else 'OFF'} (closed hours {n['start']}-{n['end']}). "
                          "Any presence in that window triggers an alert.")

    def admin_open(self, chat, args):
        self.gw.barrier.open()
        self.gw.store.add_alert("audit", "Barrier opened manually via Telegram by admin")
        self._reply(chat, "Barrier opened (closes automatically).")

    def admin_alerts(self, chat, args):
        rows = self.gw.store.recent_alerts(10)
        if not rows:
            return self._reply(chat, "No alerts recorded.")
        self._reply(chat, "Last alerts\n" + "\n".join(f"{now_str(ts)[5:16]} [{lvl}] {txt}" for ts, lvl, txt in rows))

    # ------------------------------------------------------------ users
    def user_help(self, chat, user, args):
        self._reply(chat, USER_HELP)

    user_start = user_help

    def user_free(self, chat, user, args):
        free = self.gw.free_bays()
        self._reply(chat, f"{len(free)} of {len(self.gw.nodes)} bays free" +
                    (f": {', '.join(free)}" if free else " - car park full"))

    def user_mybay(self, chat, user, args):
        for b, a in self.gw.assignments.items():
            if a["user"] == user:
                import time as _t
                left = a["exp"] - _t.time()
                state = "parked" if a.get("arrived") else "reserved (waiting for your car)"
                return self._reply(chat, f"{b}: {state}. Permit until {hhmm(a['exp'])} "
                                         f"({fmt_duration(left)} left).")
        self._reply(chat, "You have no active permit. Use the kiosk at the entrance to get one.")

    def user_extend(self, chat, user, args):
        try:
            hours = float(args[0]) if args else 1.0
        except ValueError:
            hours = 0
        if not 0 < hours <= 4:
            return self._reply(chat, "Usage: /extend <hours>  (1-4)")
        self._reply(chat, self.gw.extend(user, hours))

    # ------------------------------------------------------------ strangers
    def register(self, chat, args):
        if len(args) != 2:
            return self._reply(chat, "Usage: /register <username> <enrolment code>")
        import time as _t
        recent = [t for t in self._reg_attempts.get(chat, []) if t > _t.time() - 3600]
        if len(recent) >= 5:  # brute-force protection on enrolment codes
            return self._reply(chat, "Too many attempts. Try again later.")
        self._reg_attempts[chat] = recent + [_t.time()]
        if self.gw.access.link_telegram(args[0], args[1], chat):
            self.gw.feed(f"Telegram linked for {args[0].lower()}", "info")
            self._reply(chat, "Telegram linked. Final step: at the kiosk tap 'New user setup', enter your "
                              "username and the same code, then choose a 6-digit PIN.")
        else:
            self._reply(chat, "Invalid or expired code.")
