"""`python -m smartpark sensor-test --config config/bay.toml` - wiring / placement diagnosis.

Stop the bay service first (sudo systemctl stop smartpark-bay) so only one program drives the pins.
"""

from __future__ import annotations

import statistics
import time

from . import cleanup_gpio
from .sensor import MockSensor, UltrasonicSensor

HINTS = {
    "no_echo_start": "ECHO never went high: check VCC->pin 2 (5V), GND->pin 6, TRIG->pin 7, and that the "
                     "divider output goes to pin 11. A dead or unpowered sensor looks like this.",
    "echo_too_long": "ECHO stayed high too long: nothing within ~4 m, or the target is soft/angled. Point the "
                     "sensor at a flat surface (the car / cardboard) and keep it within 2-400 cm.",
    "echo_stuck_high": "ECHO stuck high between pings: typical of clone modules after a missed echo, or ECHO "
                       "wired straight to 5 V logic. Check the 1k/2k divider.",
    "out_of_range": "Readings outside 2-400 cm: target too close (<2 cm) or electrical noise.",
}


def sensor_test(cfg: dict, n: int = 40):
    s = cfg["sensor"]
    mock = cfg.get("mock_hardware", False)
    print(f"Sensor test on TRIG pin {s['trigger_pin']}, ECHO pin {s['echo_pin']} (BOARD numbering)"
          f"{' [MOCK]' if mock else ''}, echo timeout {s['echo_timeout_s'] * 1000:.0f} ms")
    sensor = MockSensor(cfg) if mock else UltrasonicSensor(cfg, mock=False)
    vals = []
    try:
        for i in range(n):
            d = sensor.read(burst=1)
            print(f"{i + 1:3d}: {'FAILED' if d is None else f'{d:7.2f} cm'}")
            if d is not None:
                vals.append(d)
            time.sleep(0.1)
    finally:
        cleanup_gpio()
    print(f"\nValid readings: {len(vals)}/{n}")
    if vals:
        print(f"median {statistics.median(vals):.2f} cm | min {min(vals):.2f} | max {max(vals):.2f} | "
              f"spread {statistics.pstdev(vals):.2f} cm")
    reasons = getattr(sensor, "fail_reasons", {})
    for k, v in reasons.items():
        if v:
            print(f"  {k}: {v}  -> {HINTS[k]}")
    if len(vals) == n:
        print("Sensor OK.")
    elif len(vals) >= 0.9 * n:
        print("Mostly OK - occasional misses are normal and are filtered by the median/debounce logic.")
    else:
        print("Too many failures - fix the cause above before running the bay service.")
