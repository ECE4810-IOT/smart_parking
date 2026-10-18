"""Output devices: bay traffic-light LEDs and the gateway barrier (servo + LED).

Bay LED meaning (mirrors mall parking-guidance lights):
    green          vacant
    red            occupied
    yellow         assigned to a user, waiting for their car
    red blinking   unauthorised vehicle in a permit bay
    yellow blink   sensor fault
"""

from __future__ import annotations

import logging
import threading
import time

from . import get_gpio

log = logging.getLogger(__name__)


class BayLights:
    PATTERNS = ("vacant", "occupied", "assigned", "unauthorised", "fault", "off")

    def __init__(self, cfg: dict, mock: bool = False):
        leds = cfg["leds"]
        self.pins = {"green": leds["green_pin"], "yellow": leds["yellow_pin"], "red": leds["red_pin"]}
        self.gpio = get_gpio(mock)
        for p in self.pins.values():
            self.gpio.setup(p, self.gpio.OUT)
            self.gpio.output(p, 0)
        self.pattern = "off"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bay-lights", daemon=True)
        self._thread.start()

    def set(self, pattern: str) -> None:
        if pattern not in self.PATTERNS:
            raise ValueError(pattern)
        if pattern != self.pattern:
            log.info("Lights -> %s", pattern)
        self.pattern = pattern

    def _write(self, green=0, yellow=0, red=0):
        self.gpio.output(self.pins["green"], green)
        self.gpio.output(self.pins["yellow"], yellow)
        self.gpio.output(self.pins["red"], red)

    def _run(self):
        tick = False
        while not self._stop.wait(0.4):
            tick = not tick
            p = self.pattern
            if p == "vacant":
                self._write(green=1)
            elif p == "occupied":
                self._write(red=1)
            elif p == "assigned":
                self._write(yellow=1)
            elif p == "unauthorised":
                self._write(red=int(tick))
            elif p == "fault":
                self._write(yellow=int(tick))
            else:
                self._write()

    def state(self) -> dict:
        return {name: self.gpio.input(pin) for name, pin in self.pins.items()}

    def close(self):
        self._stop.set()
        self._write()


class Barrier:
    """Entry barrier. 'servo' mode drives an SG90 on a PWM pin; 'mock' mode only
    logs. In both modes an LED mirrors the barrier so the demo shows something
    physical even without a servo."""

    CLOSED_DEG = 0
    OPEN_DEG = 90

    def __init__(self, cfg: dict, mock: bool = False):
        b = cfg["barrier"]
        self.mode = "mock" if mock else b.get("mode", "mock")
        self.open_s = b.get("open_s", 8)
        self.gpio = get_gpio(mock)
        self.led_pin = b.get("led_pin")
        if self.led_pin:
            self.gpio.setup(self.led_pin, self.gpio.OUT)
            self.gpio.output(self.led_pin, 0)
        self._pwm = None
        if self.mode == "servo":
            pin = b["servo_pin"]
            self.gpio.setup(pin, self.gpio.OUT)
            self._pwm = self.gpio.PWM(pin, 50)  # 50 Hz servo signal
            self._pwm.start(self._duty(self.CLOSED_DEG))
        self.is_open = False
        self._lock = threading.Lock()
        self._close_timer: threading.Timer | None = None
        self.listeners: list = []  # callables(is_open: bool)

    @staticmethod
    def _duty(deg: float) -> float:
        # SG90: 0.5 ms (0 deg) .. 2.5 ms (180 deg) pulse in a 20 ms period
        return 2.5 + (deg / 180.0) * 10.0

    def _move(self, deg: float):
        if self._pwm is not None:
            self._pwm.ChangeDutyCycle(self._duty(deg))
            time.sleep(0.4)
            self._pwm.ChangeDutyCycle(0)  # stop pulses -> no servo jitter
        log.info("Barrier %s (%s mode)", "OPEN" if deg == self.OPEN_DEG else "CLOSED", self.mode)

    def open(self, hold_s: float | None = None):
        with self._lock:
            if self._close_timer:
                self._close_timer.cancel()
            if not self.is_open:
                self._move(self.OPEN_DEG)
                self.is_open = True
                if self.led_pin:
                    self.gpio.output(self.led_pin, 1)
                self._notify()
            self._close_timer = threading.Timer(hold_s or self.open_s, self.close)
            self._close_timer.daemon = True
            self._close_timer.start()

    def close(self):
        with self._lock:
            if self.is_open:
                self._move(self.CLOSED_DEG)
                self.is_open = False
                if self.led_pin:
                    self.gpio.output(self.led_pin, 0)
                self._notify()

    def _notify(self):
        for cb in list(self.listeners):
            try:
                cb(self.is_open)
            except Exception:  # pragma: no cover
                log.exception("barrier listener failed")
