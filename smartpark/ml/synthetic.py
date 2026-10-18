"""SYNTHETIC dataset generator - for testing the ML pipeline end-to-end ONLY.

Results trained on this data must NOT be reported as real results. Record
real windows on the bays with `python -m smartpark bay --config ... --record <label>`.
Every row written here has bay = "SYNTHETIC" so train.py can refuse it.
"""

from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path

from . import LABELS
from .features import FEATURE_NAMES, extract_features

RATE_HZ = 10
PRE, POST = 1.5, 2.5


def _ease(x):
    x = min(1.0, max(0.0, x))
    return 0.5 - 0.5 * math.cos(math.pi * x)


def generate_window(kind: str, rng: random.Random, occ=10.0, vac=12.0) -> list:
    empty = rng.uniform(22, 40)
    parked = rng.uniform(3.5, 8.0)
    noise = rng.uniform(0.15, 0.6)
    drop = rng.uniform(0.0, 0.05)
    dur = rng.uniform(0.8, 3.0)                     # manoeuvre duration (speed varies)
    ts = [round(-PRE + i / RATE_HZ, 2) for i in range(int((PRE + POST) * RATE_HZ) + 1)]
    out = []
    # time offset so the threshold crossing happens at t = 0
    if kind == "arrival":
        frac_cross = math.acos(1 - 2 * ((empty - occ) / (empty - parked))) / math.pi
        start = -frac_cross * dur
        f = lambda t: empty + (parked - empty) * _ease((t - start) / dur)  # noqa: E731
    elif kind == "departure":
        frac_cross = math.acos(1 - 2 * ((vac - parked) / (empty - parked))) / math.pi
        start = -frac_cross * dur
        f = lambda t: parked + (empty - parked) * _ease((t - start) / dur)  # noqa: E731
    elif kind == "transient":
        depth = rng.uniform(3.5, 9.0)
        width = rng.uniform(0.6, 2.2)
        frac = math.asin(min(1.0, (empty - occ) / (empty - depth))) / math.pi
        start = -frac * width

        def f(t):
            x = (t - start) / width
            if 0 <= x <= 1:
                return empty - (empty - depth) * math.sin(math.pi * x) + rng.gauss(0, 1.0)
            return empty
    else:  # noise: spurious echoes around a stable background
        base = rng.choice([empty, parked])
        spikes = {round(rng.uniform(-PRE, POST), 1) for _ in range(rng.randint(3, 9))} | {0.0}

        def f(t):
            if round(t, 1) in spikes:
                return rng.uniform(2.5, base + 15)
            return base
    for t in ts:
        if rng.random() < drop:
            out.append((t, None))
        else:
            out.append((t, round(max(2.0, f(t) + rng.gauss(0, noise)), 2)))
    return out


def write_dataset(path: str, per_class: int = 120, seed: int = 42):
    rng = random.Random(seed)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "bay", "label", "predicted", *FEATURE_NAMES, "window"])
        for label in LABELS:
            for i in range(per_class):
                win = generate_window(label, rng)
                feats = extract_features(win, 10.0, 12.0)
                w.writerow([i, "SYNTHETIC", label, "", *[round(x, 4) for x in feats], json.dumps(win)])
    print(f"Wrote {per_class * len(LABELS)} SYNTHETIC windows to {p} (pipeline testing only - not real data)")
    return p
