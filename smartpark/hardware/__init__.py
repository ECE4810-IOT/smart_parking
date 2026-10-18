"""Hardware abstraction: real Raspberry Pi GPIO or a mock for laptop testing."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class MockGPIO:
    """Minimal stand-in for RPi.GPIO so the whole system runs without a Pi."""

    BOARD = "BOARD"
    BCM = "BCM"
    OUT = "OUT"
    IN = "IN"
    HIGH = 1
    LOW = 0

    def __init__(self):
        self.pins: dict[int, int] = {}

    def setmode(self, mode):
        pass

    def setwarnings(self, flag):
        pass

    def setup(self, pin, mode):
        self.pins.setdefault(pin, 0)

    def output(self, pin, value):
        self.pins[pin] = int(bool(value))

    def input(self, pin):
        return self.pins.get(pin, 0)

    def cleanup(self):
        self.pins.clear()

    class PWM:  # noqa: D401 - mirrors RPi.GPIO.PWM
        def __init__(self, pin, freq):
            self.pin, self.freq, self.duty = pin, freq, 0.0

        def start(self, duty):
            self.duty = duty

        def ChangeDutyCycle(self, duty):
            self.duty = duty

        def stop(self):
            self.duty = 0.0


_gpio = None


def get_gpio(mock: bool):
    """Return the RPi.GPIO module (BOARD numbering) or a MockGPIO instance."""
    global _gpio
    if _gpio is not None:
        return _gpio
    if mock:
        _gpio = MockGPIO()
    else:
        try:
            import RPi.GPIO as GPIO  # type: ignore
        except ImportError as e:  # pragma: no cover - only on non-Pi machines
            raise RuntimeError(
                "RPi.GPIO not available. Install python3-rpi.gpio (or rpi-lgpio) "
                "or set mock_hardware = true in the config."
            ) from e
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        _gpio = GPIO
    return _gpio


def cleanup_gpio():
    global _gpio
    if _gpio is not None:
        try:
            _gpio.cleanup()
        except Exception:  # pragma: no cover
            pass
        _gpio = None
