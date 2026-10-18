"""Simulate the full 5-Pi system (gateway + N bays) on one computer.

Everything real runs - secure handshakes, AES-GCM envelopes, tickets, OTP,
analytics, the kiosk web UI - only the sensors/LEDs/servo are mocked and
Telegram/ThingSpeak are printed to the console (unless a real bot token is
given). Useful for development, testing and as a demo backup.

    python -m smartpark simulate            # then open http://127.0.0.1:8080
"""

from __future__ import annotations

import logging
import os
import shlex
import tempfile
import threading
import time
from pathlib import Path

from .bay.app import BayNode
from .gateway.launcher import GatewayLauncher
from .hardware.sensor import MockSensor
from .kiosk.server import KioskServer
from .net.telegram import FakeTelegram
from .provision import create_gateway_vault
from .security.channel import DeviceIdentity
from .util import init_metrics

log = logging.getLogger("smartpark.sim")

DEMO_PASSWORD = "DemoAdmin2026"
DEMO_ADMIN_CHAT = "999"
DEMO_USERS = {"alice": ("1001", "246810"), "bob": ("1002", "135792")}

HELP = """Simulator commands (N = bay number):
  arrive N | leave N | walk N | noise N   drive the mock sensor of bay N
  fault N | fix N                          simulate a broken / repaired sensor
  offline N | online N                     stop / restart bay N's Pi
  tg <text>                                send a Telegram message as the ADMIN (e.g. tg /status all)
  tgu <user> <text>                        send a Telegram message as a user (e.g. tgu alice /mybay)
  access <user> [pin]                      full kiosk flow with the code from the fake Telegram
  report                                   print today's analytics
  demo                                     run the scripted demo scenario
  help | quit"""


class Simulator:
    def __init__(self, n_bays: int = 4, base_port: int = 9100, kiosk_port: int = 8080,
                 data_dir: str | None = None, password: str = DEMO_PASSWORD, real_telegram: bool = False,
                 kiosk_host: str = "127.0.0.1", fast_kdf: bool = False):
        self.root = Path(data_dir or tempfile.mkdtemp(prefix="smartpark_sim_"))
        self.password = password
        self.n = n_bays
        bays = {f"bay{i}": f"127.0.0.1:{base_port + i}" for i in range(1, n_bays + 1)}
        self.gw_cfg = self._cfg("gateway", {
            "device_id": "gateway", "data_dir": str(self.root / "gateway"), "mock_hardware": True,
            "network": {"listen_host": "127.0.0.1", "listen_port": base_port}, "bays": bays,
            "kiosk": {"host": kiosk_host, "port": kiosk_port},
            "telegram": {"coalesce_window_s": 1.0}, "thingspeak": {"interval_s": 15},
            "access": {"request_cooldown_s": 0, "no_show_min": 3},
        })
        init_metrics(self.gw_cfg["data_dir"])
        vault_file = Path(self.gw_cfg["data_dir"]) / "vault.json"
        if not vault_file.exists():
            users = {u: {"status": "active", "chat_id": chat, "pin": pin, "created": time.time()}
                     for u, (chat, pin) in DEMO_USERS.items()}
            token = os.environ.get("SMARTPARK_TELEGRAM_TOKEN", "") if real_telegram else ""
            admin = os.environ.get("SMARTPARK_ADMIN_CHAT", DEMO_ADMIN_CHAT) if real_telegram else DEMO_ADMIN_CHAT
            create_gateway_vault(self.gw_cfg, password, {"telegram_token": token, "admin_chat_id": admin,
                                                          "thingspeak_write_key": "", "thingspeak_channel_id": ""},
                                 users=users, scrypt_n=2 ** 12 if fast_kdf else None)
        self.gw_keys = Path(self.gw_cfg["data_dir"]) / "keys"
        self.tg = None if real_telegram else FakeTelegram()
        self.launcher = GatewayLauncher(self.gw_cfg, fake_cloud=True, telegram=self.tg)
        self.kiosk = KioskServer(self.launcher, kiosk_host, kiosk_port)
        self.sensors: dict[int, MockSensor] = {}
        self.bays: dict[int, BayNode] = {}
        self.bay_cfgs: dict[int, dict] = {}
        for i in range(1, n_bays + 1):
            cfg = self._cfg("bay", {
                "device_id": f"bay{i}", "data_dir": str(self.root / f"bay{i}"), "mock_hardware": True,
                "gateway": f"127.0.0.1:{base_port}",
                "network": {"listen_host": "127.0.0.1", "listen_port": base_port + i}, "heartbeat_s": 5,
            })
            ident = DeviceIdentity.load_or_create(cfg["device_id"], Path(cfg["data_dir"]) / "keys")
            ident.trust("gateway", (self.gw_keys / "gateway_public.pem").read_bytes())
            DeviceIdentity("gateway", self.gw_keys).trust(cfg["device_id"], ident.public_pem)
            self.bay_cfgs[i] = cfg
            self.sensors[i] = MockSensor(seed=i)

    @staticmethod
    def _cfg(role: str, override: dict) -> dict:
        from .config import BAY_DEFAULTS, GATEWAY_DEFAULTS, _deep_merge

        cfg = _deep_merge(GATEWAY_DEFAULTS if role == "gateway" else BAY_DEFAULTS, override)
        Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
        return cfg

    # ------------------------------------------------------------------
    def start(self, auto_unlock: bool = True):
        self.kiosk.start()
        if auto_unlock:
            ok, msg = self.launcher.unlock(self.password)
            log.info("Auto-unlock: %s", msg)
        for i in self.bay_cfgs:
            self.start_bay(i)

    def start_bay(self, i: int):
        node = BayNode(self.bay_cfgs[i], sensor=self.sensors[i])
        node.start()
        self.bays[i] = node

    def stop_bay(self, i: int):
        node = self.bays.pop(i, None)
        if node:
            node.stop()

    @property
    def gw(self):
        return self.launcher.gateway

    def stop(self):
        for i in list(self.bays):
            self.stop_bay(i)
        self.kiosk.stop()
        self.launcher.stop()

    def wait_connected(self, timeout: float = 15) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.gw and all(n.online for n in self.gw.nodes.values()):
                return True
            time.sleep(0.2)
        return False

    # ------------------------------------------------------------------ helpers
    def last_code_for(self, chat: str) -> str | None:
        if not isinstance(self.tg, FakeTelegram):
            return None
        for m in reversed(self.tg.sent):
            if m["chat_id"] == chat and m["kind"] == "otp":
                return m["text"].split(":")[1].split("\n")[0].replace(" ", "")
        return None

    def kiosk_access(self, user: str, pin: str | None = None) -> dict:
        chat, default_pin = DEMO_USERS.get(user, (None, None))
        r = self.gw.request_access(user)
        if r["status"] != "sent":
            return r
        time.sleep(0.2)
        code = self.last_code_for(chat) if chat else "000000"
        return self.gw.verify_access(r["request_id"], code or "000000", pin or default_pin or "000000")

    def command(self, line: str) -> bool:
        parts = shlex.split(line)
        if not parts:
            return True
        c, args = parts[0].lower(), parts[1:]
        try:
            if c in ("arrive", "leave", "walk", "noise"):
                getattr(self.sensors[int(args[0])], c)()
            elif c == "fault":
                self.sensors[int(args[0])].set_fault(True)
            elif c == "fix":
                self.sensors[int(args[0])].set_fault(False)
            elif c == "offline":
                self.stop_bay(int(args[0]))
            elif c == "online":
                self.start_bay(int(args[0]))
            elif c == "tg":
                self.tg.inject(self.gw.admin_chat, " ".join(args))
            elif c == "tgu":
                self.tg.inject(DEMO_USERS[args[0]][0], " ".join(args[1:]))
            elif c == "access":
                print(self.kiosk_access(args[0], args[1] if len(args) > 1 else None))
            elif c == "report":
                for k, v in self.gw.report().items():
                    print(f"  {k}: {v}")
            elif c == "demo":
                self.demo()
            elif c in ("quit", "exit", "q"):
                return False
            else:
                print(HELP)
        except (IndexError, KeyError, ValueError) as e:
            print(f"Bad command ({e}). Type 'help'.")
        return True

    def demo(self):
        """Scripted scenario that exercises every feature (used for testing and the demo backup)."""
        steps = [
            ("Alice unlocks a bay at the kiosk", lambda: print(self.kiosk_access("alice"))),
            ("Alice parks in her assigned bay (bay1)", lambda: self.sensors[1].arrive()),
            ("A person walks past bay2 (should be ignored, not 'car parked')", lambda: self.sensors[2].walk()),
            ("A car parks in bay3 WITHOUT a permit (unauthorised)", lambda: self.sensors[3].arrive()),
            ("Wrong PIN attempt for bob", lambda: print(self.kiosk_access("bob", "111111"))),
            ("Admin checks status via Telegram", lambda: self.tg.inject(self.gw.admin_chat, "/status all")),
            ("Alice extends her permit", lambda: self.tg.inject(DEMO_USERS["alice"][0], "/extend 1")),
            ("Alice leaves bay1", lambda: self.sensors[1].leave()),
            ("Admin asks for the report", lambda: self.tg.inject(self.gw.admin_chat, "/report")),
        ]
        for title, fn in steps:
            print(f"\n>>> {title}")
            fn()
            time.sleep(7)


def run_simulator(args):
    sim = Simulator(n_bays=args.bays, kiosk_port=args.kiosk_port, data_dir=args.data_dir,
                    real_telegram=args.real_telegram, kiosk_host=args.kiosk_host)
    sim.start(auto_unlock=not args.locked)
    print(f"\nSimulator data: {sim.root}")
    print(f"Kiosk UI: http://{args.kiosk_host}:{args.kiosk_port}   (admin password: {sim.password})")
    print("Demo users: alice PIN 246810, bob PIN 135792\n")
    print(HELP)
    if args.script:
        sim.wait_connected()
        sim.demo()
        time.sleep(3)
        sim.stop()
        return
    try:
        while True:
            line = input("sim> ")
            if not sim.command(line):
                break
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        sim.stop()
