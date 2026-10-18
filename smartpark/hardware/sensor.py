"""HC-SR04 ultrasonic sensor driver.

Improvements over the lab code (each is measurable for the report):
  * a lock so two threads can never trigger the sensor at once (echo collisions)
  * time.perf_counter() for microsecond timing instead of time.time()
  * echo timeouts so a missed echo can never hang the program
  * temperature-compensated speed of sound (331.3 + 0.606*T m/s)
  * burst-median filtering and rejection of out-of-range readings
"""

from __future__ import annotations

import logging
import math
import random
import statistics
import threading
import time
from collections import deque

from . import get_gpio

log = logging.getLogger(__name__)

MIN_CYCLE_S = 0.06  # HC-SR04 datasheet: >= 60 ms between triggers


class UltrasonicSensor:
    def __init__(self, cfg: dict, mock: bool = False):
        s = cfg["sensor"]
        self.trig = s["trigger_pin"]
        self.echo = s["echo_pin"]
        self.timeout = s["echo_timeout_s"]
        self.min_cm = s["min_cm"]
        self.max_cm = s["max_cm"]
        self.set_temperature(s.get("temperature_c", 25.0))
        self._lock = threading.Lock()
        self._last_trigger = 0.0
        self.gpio = get_gpio(mock)
        self.gpio.setup(self.trig, self.gpio.OUT)
        self.gpio.setup(self.echo, self.gpio.IN)
        self.gpio.output(self.trig, self.gpio.LOW)
        self.timeouts = 0
        self.readings = 0
        # why pings fail - printed by `python -m smartpark sensor-test` for wiring diagnosis
        self.fail_reasons = {"echo_stuck_high": 0, "no_echo_start": 0, "echo_too_long": 0, "out_of_range": 0}

    def set_temperature(self, celsius: float) -> None:
        speed_m_s = 331.3 + 0.606 * celsius
        self._half_speed_cm_s = speed_m_s * 100 / 2  # round trip -> one way

    def _ping(self) -> float | None:
        """One trigger/echo cycle. Caller must hold the lock."""
        wait = MIN_CYCLE_S - (time.perf_counter() - self._last_trigger)
        if wait > 0:
            time.sleep(wait)
        g = self.gpio
        # Some HC-SR04 clones keep ECHO high for up to ~200 ms after a missed echo. Triggering
        # again while it is still high would measure garbage, so wait for it to drop first.
        t_wait = time.perf_counter()
        while g.input(self.echo) == 1:
            if time.perf_counter() - t_wait > 0.25:
                self.timeouts += 1
                self.fail_reasons["echo_stuck_high"] += 1
                return None
            time.sleep(0.001)
        g.output(self.trig, g.HIGH)
        time.sleep(0.00001)
        g.output(self.trig, g.LOW)
        self._last_trigger = time.perf_counter()

        start = time.perf_counter()
        pulse_start = start
        while g.input(self.echo) == 0:
            pulse_start = time.perf_counter()
            if pulse_start - start > self.timeout:
                self.timeouts += 1
                self.fail_reasons["no_echo_start"] += 1   # wiring: TRIG/ECHO/VCC/GND or divider
                return None
        pulse_end = pulse_start
        while g.input(self.echo) == 1:
            pulse_end = time.perf_counter()
            if pulse_end - pulse_start > self.timeout:
                self.timeouts += 1
                self.fail_reasons["echo_too_long"] += 1   # nothing in range / soft or angled target
                return None
        d = (pulse_end - pulse_start) * self._half_speed_cm_s
        if not (self.min_cm <= d <= self.max_cm):
            self.fail_reasons["out_of_range"] += 1
            return None
        return d

    def read(self, burst: int = 1) -> float | None:
        """Median of `burst` valid pings, or None if every ping failed."""
        with self._lock:
            vals = [v for v in (self._ping() for _ in range(max(1, burst))) if v is not None]
        self.readings += 1
        if not vals:
            return None
        return round(statistics.median(vals), 2)


class MockSensor:
    """Simulated sensor driven by scripted 'world' actions.

    Used by the simulator and tests. Produces realistic trajectories:
      arrive : car drives in and stops ~6 cm from the sensor (scale model)
      leave  : car reverses out
      walk   : something passes through briefly (person / hand / trolley)
      noise  : a burst of spurious echoes
    plus Gaussian noise and occasional dropouts, like the real HC-SR04.
    """

    def __init__(self, cfg: dict | None = None, empty_cm: float = 30.0, parked_cm: float = 6.0,
                 noise_cm: float = 0.3, dropout: float = 0.01, seed: int | None = None):
        self.empty_cm = empty_cm
        self.parked_cm = parked_cm
        self.noise_cm = noise_cm
        self.dropout = dropout
        self.rng = random.Random(seed)
        self._base = empty_cm
        self._actions: deque = deque()
        self._lock = threading.Lock()
        self.faulty = False
        self.timeouts = 0
        self.readings = 0

    # --- scripted actions -------------------------------------------------
    def _schedule(self, kind: str, duration: float, **kw):
        with self._lock:
            start = time.monotonic()
            if self._actions:
                start = max(start, self._actions[-1][1] + self._actions[-1][2])
            self._actions.append((kind, start, duration, kw))

    def arrive(self, duration: float = 1.6):
        self._schedule("arrive", duration)

    def leave(self, duration: float = 1.6):
        self._schedule("leave", duration)

    def walk(self, duration: float = 2.0, depth_cm: float = 6.0):
        self._schedule("walk", duration, depth=depth_cm)

    def noise(self, duration: float = 1.0):
        self._schedule("noise", duration)

    def set_fault(self, faulty: bool):
        self.faulty = faulty

    # --- reading ----------------------------------------------------------
    def _true_distance(self, now: float) -> float:
        with self._lock:
            while self._actions:
                kind, start, dur, kw = self._actions[0]
                if now < start:
                    return self._base
                frac = (now - start) / dur
                if frac >= 1.0:
                    self._actions.popleft()
                    if kind == "arrive":
                        self._base = self.parked_cm
                    elif kind == "leave":
                        self._base = self.empty_cm
                    continue
                ease = 0.5 - 0.5 * math.cos(math.pi * frac)  # smooth in/out
                if kind == "arrive":
                    return self.empty_cm + (self.parked_cm - self.empty_cm) * ease
                if kind == "leave":
                    return self.parked_cm + (self.empty_cm - self.parked_cm) * ease
                if kind == "walk":
                    dip = math.sin(math.pi * frac)
                    return self._base - (self._base - kw["depth"]) * dip + self.rng.gauss(0, 1.2)
                if kind == "noise":
                    return self.rng.uniform(3, self._base + 10)
            return self._base

    def read(self, burst: int = 1) -> float | None:
        self.readings += 1
        if self.faulty:
            self.timeouts += 1
            return None
        vals = []
        for _ in range(max(1, burst)):
            if self.rng.random() < self.dropout:
                self.timeouts += 1
                continue
            vals.append(self._true_distance(time.monotonic()) + self.rng.gauss(0, self.noise_cm))
        if not vals:
            return None
        return round(max(2.0, statistics.median(vals)), 2)
