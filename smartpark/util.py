"""Small shared helpers: logging setup, metrics CSV writer, time helpers."""

from __future__ import annotations

import csv
import logging
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path

_metrics_lock = threading.Lock()
_metrics_dir: Path | None = None


def setup_logging(device_id: str, data_dir: str, verbose: bool = False) -> logging.Logger:
    log_path = Path(data_dir) / "logs" / f"{device_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(f"%(asctime)s [%(levelname)s] [{device_id}] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not any(getattr(h, "_smartpark", False) for h in root.handlers):
        for h in (logging.FileHandler(log_path), logging.StreamHandler()):
            h.setFormatter(fmt)
            h._smartpark = True  # type: ignore[attr-defined]
            root.addHandler(h)
    return logging.getLogger("smartpark")


def init_metrics(data_dir: str) -> None:
    """All report evidence is written as CSV under <data_dir>/metrics/."""
    global _metrics_dir
    _metrics_dir = Path(data_dir) / "metrics"
    _metrics_dir.mkdir(parents=True, exist_ok=True)


def metric(name: str, **fields) -> None:
    """Append one row to metrics/<name>.csv (header written on first use)."""
    if _metrics_dir is None:
        return
    fields = {"ts": round(time.time(), 3), **fields}
    path = _metrics_dir / f"{name}.csv"
    with _metrics_lock:
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(fields.keys()))
            if new:
                w.writeheader()
            w.writerow(fields)


def now_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d %H:%M:%S")


def hhmm(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime("%H:%M")


def in_time_window(start: str, end: str, at: datetime | None = None) -> bool:
    """True if `at` lies in [start, end) where the window may wrap midnight."""
    at = at or datetime.now()
    s = dtime.fromisoformat(start)
    e = dtime.fromisoformat(end)
    t = at.time()
    return (s <= t < e) if s <= e else (t >= s or t < e)


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
