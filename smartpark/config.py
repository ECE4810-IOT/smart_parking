"""Configuration loading.

Settings live in a TOML file per device (see config/*.example.toml).
Secrets (bot token, API keys, user PINs, private keys) are NEVER stored here -
they live in the encrypted vault (security/vault.py) on the gateway.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore


GATEWAY_DEFAULTS = {
    "role": "gateway",
    "device_id": "gateway",
    "data_dir": "~/smartpark_data",
    "mock_hardware": False,
    "network": {"listen_host": "0.0.0.0", "listen_port": 8765},
    # bay_id -> "host:port" (LAN IP, hostname.local or Tailscale name/IP)
    "bays": {},
    "kiosk": {"enabled": True, "host": "127.0.0.1", "port": 8080},
    "telegram": {"enabled": True, "poll_timeout_s": 25, "coalesce_window_s": 3.0},
    "thingspeak": {"enabled": True, "interval_s": 20},
    "access": {
        "otp_ttl_s": 60,
        "max_attempts": 3,
        "lockout_base_s": 300,
        "request_cooldown_s": 30,
        "global_requests_per_min": 10,
        "default_stay_min": 120,
        "no_show_min": 10,
        "overstay_reminder_min": 10,
        "enrol_code_ttl_min": 30,
    },
    "barrier": {"mode": "mock", "servo_pin": 12, "led_pin": 18, "open_s": 8},
    "security": {"max_clock_skew_s": 60, "heartbeat_timeout_s": 35},
    "night_mode": {"enabled": False, "start": "22:00", "end": "06:00"},
}

BAY_DEFAULTS = {
    "role": "bay",
    "device_id": "bay1",
    "data_dir": "~/smartpark_data",
    "mock_hardware": False,
    "gateway": "gateway.local:8765",
    "network": {"listen_host": "0.0.0.0", "listen_port": 8765},
    # BOARD pin numbering (same as the lab wiring: TRIG=7, ECHO=11)
    "sensor": {
        "trigger_pin": 7,
        "echo_pin": 11,
        "echo_timeout_s": 0.04,
        "temperature_c": 25.0,
        "min_cm": 2.0,
        "max_cm": 400.0,
    },
    "leds": {"green_pin": 13, "yellow_pin": 15, "red_pin": 16},
    "occupancy": {
        "occupied_below_cm": 10.0,   # enter-occupied threshold
        "vacant_above_cm": 12.0,     # leave-occupied threshold (hysteresis)
        "window_pre_s": 1.5,         # event window captured before the crossing
        "window_post_s": 2.5,        # ... and after the crossing
        "reconcile_s": 6.0,          # safety net if classifier suppressed a real change
        "min_confidence": 0.6,
    },
    "sampling": {
        "idle_period_s": 0.5,       # 2 Hz while nothing is happening
        "active_period_s": 0.1,     # 10 Hz while something moves
        "idle_burst": 3,
        "active_change_cm": 2.0,
        "active_hold_s": 5.0,
    },
    "ml": {"model_path": "models/event_model.joblib", "record_label": ""},
    "heartbeat_s": 10,
    "bay_mode": "permit",  # "permit" (needs kiosk assignment) or "open"
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None, role: str | None = None) -> dict:
    """Load a TOML config and merge it over the role defaults."""
    raw: dict = {}
    if path:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    role = role or raw.get("role", "gateway")
    defaults = GATEWAY_DEFAULTS if role == "gateway" else BAY_DEFAULTS
    cfg = _deep_merge(defaults, raw)
    cfg["role"] = role
    cfg["data_dir"] = str(Path(os.path.expanduser(cfg["data_dir"])).resolve())
    Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def data_path(cfg: dict, *parts: str) -> Path:
    p = Path(cfg["data_dir"]).joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
