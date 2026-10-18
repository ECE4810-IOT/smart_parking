"""Feature extraction for one event window.

A window is a list of (t, distance_cm) samples where t is seconds relative to
the threshold crossing that triggered capture (negative = before) and
distance may be None for a dropped echo. Pure Python on purpose: it runs on
every bay node and must be fast with no heavy dependencies.
"""

from __future__ import annotations

import math
import statistics

FEATURE_NAMES = (
    "d_start",          # median distance in the first 0.5 s
    "d_end",            # median distance in the last 0.5 s
    "d_min",
    "d_max",
    "d_mean",
    "d_std",
    "delta",            # d_end - d_start  (negative = something arrived)
    "frac_below",       # fraction of samples below the occupied threshold
    "frac_below_last",  # same, last 1.0 s only
    "time_below_s",     # longest continuous stretch below the threshold
    "n_crossings",      # threshold crossings (with hysteresis)
    "slope_post",       # linear trend after the crossing (cm/s)
    "max_step",         # largest jump between consecutive samples
    "roughness",        # mean |second difference| -> jitter / walking legs
    "dropout_frac",     # fraction of failed echoes
)


def _median(vals, default):
    return statistics.median(vals) if vals else default


def _slope(points):
    if len(points) < 3:
        return 0.0
    n = len(points)
    mt = sum(t for t, _ in points) / n
    md = sum(d for _, d in points) / n
    num = sum((t - mt) * (d - md) for t, d in points)
    den = sum((t - mt) ** 2 for t, _ in points)
    return num / den if den else 0.0


def extract_features(window, occupied_below: float, vacant_above: float) -> list[float]:
    total = len(window)
    pts = [(t, d) for t, d in window if d is not None]
    if not pts:
        return [0.0] * (len(FEATURE_NAMES) - 1) + [1.0]
    t0, t1 = pts[0][0], pts[-1][0]
    ds = [d for _, d in pts]
    mean = sum(ds) / len(ds)
    d_start = _median([d for t, d in pts if t <= t0 + 0.5], ds[0])
    d_end = _median([d for t, d in pts if t >= t1 - 0.5], ds[-1])
    last = [d for t, d in pts if t >= t1 - 1.0]

    # longest continuous run below threshold + hysteresis crossings
    longest = run_start = 0.0
    in_run = False
    state_below = ds[0] < occupied_below
    crossings = 0
    for t, d in pts:
        below = d < occupied_below
        if below and not in_run:
            in_run, run_start = True, t
        elif not below and in_run:
            in_run = False
            longest = max(longest, t - run_start)
        if state_below and d > vacant_above:
            state_below = False
            crossings += 1
        elif not state_below and d < occupied_below:
            state_below = True
            crossings += 1
    if in_run:
        longest = max(longest, t1 - run_start)

    steps = [abs(b - a) for a, b in zip(ds, ds[1:])]
    second = [abs(ds[i + 1] - 2 * ds[i] + ds[i - 1]) for i in range(1, len(ds) - 1)]

    return [
        d_start,
        d_end,
        min(ds),
        max(ds),
        mean,
        statistics.pstdev(ds) if len(ds) > 1 else 0.0,
        d_end - d_start,
        sum(d < occupied_below for d in ds) / len(ds),
        (sum(d < occupied_below for d in last) / len(last)) if last else 0.0,
        longest,
        float(crossings),
        _slope([(t, d) for t, d in pts if t >= 0]),
        max(steps) if steps else 0.0,
        (sum(second) / len(second)) if second else 0.0,
        1.0 - len(pts) / total if total else 1.0,
    ]


def features_dict(vec) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) and math.isfinite(v) else v)
            for k, v in zip(FEATURE_NAMES, vec)}
