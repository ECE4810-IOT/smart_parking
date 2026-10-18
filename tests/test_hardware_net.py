"""Tests for the real (non-mock) code paths that the simulator does not exercise:
the Telegram HTTP client and the HC-SR04 timing driver."""

import threading
import time

import pytest

from smartpark.config import BAY_DEFAULTS
from smartpark.net.telegram import TelegramClient


# ----------------------------------------------------------------- Telegram client
class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


class FakeSession:
    """Records calls like requests.Session.post and answers like the Bot API."""

    def __init__(self):
        self.calls = []
        self.updates = [{"update_id": 7, "message": {"chat": {"id": 1}, "text": "/status all"}}]
        self.served = False

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, json, timeout))
        if method == "getUpdates":
            if json.get("offset") is None and "timeout" not in json:
                return FakeResp({"ok": True, "result": []})          # stale-skip call
            if not self.served:
                self.served = True
                return FakeResp({"ok": True, "result": self.updates})
            time.sleep(0.05)
            return FakeResp({"ok": True, "result": []})
        if method == "sendMessage":
            return FakeResp({"ok": True, "result": {"message_id": 42}})
        return FakeResp({"ok": True, "result": True})


def test_telegram_poll_send_delete(tmp_path):
    got = []
    tg = TelegramClient("TOKEN", tmp_path / "outbox.db", poll=True, poll_timeout_s=1,
                        coalesce_window_s=0.1, on_update=got.append)
    fake = FakeSession()
    tg._session = fake
    sent = threading.Event()
    ids = []
    tg.start()
    tg.send("1", "hello", on_sent=lambda mid: (ids.append(mid), sent.set()))
    assert sent.wait(5) and ids == [42]
    deadline = time.time() + 5
    while not got and time.time() < deadline:
        time.sleep(0.05)
    tg.stop()
    assert got and got[0]["message"]["text"] == "/status all"
    polls = [c for c in fake.calls if c[0] == "getUpdates" and "timeout" in (c[1] or {})]
    assert polls, "long poll must send Telegram's own 'timeout' parameter"
    assert all(c[2] > c[1]["timeout"] for c in polls), "HTTP timeout must exceed the long-poll timeout"


def test_telegram_coalesces(tmp_path):
    tg = TelegramClient("TOKEN", tmp_path / "o.db", poll=False, coalesce_window_s=0.3)
    fake = FakeSession()
    tg._session = fake
    tg.start()
    for i in range(3):
        tg.send("1", f"event {i}", coalesce=True)
    time.sleep(1.0)
    tg.stop()
    sends = [c for c in fake.calls if c[0] == "sendMessage"]
    assert len(sends) == 1 and sends[0][1]["text"].count("event") == 3


# ----------------------------------------------------------------- HC-SR04 driver
class EchoGPIO:
    """Fake RPi.GPIO: after each trigger, ECHO goes high 0.3 ms later for `pulse_s`."""
    OUT, IN, HIGH, LOW, BOARD = "OUT", "IN", 1, 0, "BOARD"

    def __init__(self, pulse_s, stuck_high_s=0.0):
        self.pulse_s, self.trig_t = pulse_s, None
        self.stuck_until = time.perf_counter() + stuck_high_s

    def setup(self, *a): pass
    def setmode(self, *a): pass
    def setwarnings(self, *a): pass
    def cleanup(self): pass

    def output(self, pin, v):
        if pin == 7 and v == 0:
            self.trig_t = time.perf_counter()

    def input(self, pin):
        now = time.perf_counter()
        if now < self.stuck_until:
            return 1
        if self.trig_t is None or self.pulse_s is None:
            return 0
        start = self.trig_t + 0.0003
        return 1 if start <= now < start + self.pulse_s else 0


def make_sensor(monkeypatch, gpio):
    import smartpark.hardware.sensor as sensor_mod
    monkeypatch.setattr(sensor_mod, "get_gpio", lambda mock: gpio)
    cfg = {"sensor": dict(BAY_DEFAULTS["sensor"], temperature_c=20.0)}
    return sensor_mod.UltrasonicSensor(cfg)


def test_sensor_measures_distance(monkeypatch):
    s = make_sensor(monkeypatch, EchoGPIO(pulse_s=0.0006))      # ~10.3 cm at 20 C
    d = s.read(burst=3)
    assert d is not None and 9.0 < d < 12.0


def test_sensor_no_echo_is_reported(monkeypatch):
    s = make_sensor(monkeypatch, EchoGPIO(pulse_s=None))
    assert s.read() is None and s.fail_reasons["no_echo_start"] == 1


def test_sensor_waits_for_stuck_echo(monkeypatch):
    s = make_sensor(monkeypatch, EchoGPIO(pulse_s=0.0006, stuck_high_s=0.1))
    d = s.read()
    assert d is not None and 9.0 < d < 12.0   # waited for ECHO to drop instead of measuring garbage


def test_sensor_echo_too_long(monkeypatch):
    s = make_sensor(monkeypatch, EchoGPIO(pulse_s=0.2))
    assert s.read() is None and s.fail_reasons["echo_too_long"] == 1
