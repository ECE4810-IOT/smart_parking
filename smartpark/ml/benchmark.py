"""Measure the real cost of edge inference on THIS device (run it on a bay Pi).

    python -m smartpark benchmark --model ~/smartpark_data/models/event_model.joblib --data dataset/windows.csv

Reports feature-extraction and prediction latency (median / p95), throughput,
model size and peak memory, for the trained model and the rule baseline.
Results are also appended to benchmark_results.csv for the report.
"""

from __future__ import annotations

import csv
import json
import os
import platform
import random
import statistics
import time
import tracemalloc
from pathlib import Path

from . import LABELS
from .classifier import EventClassifier
from .synthetic import generate_window


def _windows(data: str | None, n: int):
    if data and Path(data).exists():
        with open(data, newline="") as f:
            wins = [[tuple(p) for p in json.loads(r["window"])] for r in csv.DictReader(f)]
        if wins:
            return wins
    rng = random.Random(1)
    return [generate_window(rng.choice(LABELS), rng) for _ in range(min(n, 400))]


def _run(clf: EventClassifier, wins, n):
    feat, inf = [], []
    tracemalloc.start()
    t_all = time.perf_counter()
    for i in range(n):
        p = clf.classify(wins[i % len(wins)])
        feat.append(p.feature_ms)
        inf.append(p.infer_ms)
    total = time.perf_counter() - t_all
    peak = tracemalloc.get_traced_memory()[1] / 1024
    tracemalloc.stop()
    feat.sort()
    inf.sort()
    q = lambda v, f: round(v[int(f * (len(v) - 1))], 4)  # noqa: E731
    return {"feature_ms_median": round(statistics.median(feat), 4), "feature_ms_p95": q(feat, .95),
            "infer_ms_median": round(statistics.median(inf), 4), "infer_ms_p95": q(inf, .95),
            "throughput_per_s": round(n / total, 1), "peak_mem_kb": round(peak, 1)}


def benchmark(model: str | None, data: str | None, n: int = 1000):
    wins = _windows(data, n)
    results = {}
    if model:
        clf = EventClassifier(model, 10.0, 12.0)
        if clf.model is None:
            raise SystemExit(f"Could not load model {model}")
        results[clf.model_meta.get("name", "model")] = (_run(clf, wins, n), round(os.path.getsize(model) / 1024, 1))
    rules = EventClassifier(None, 10.0, 12.0)
    results["Rule baseline"] = (_run(rules, wins, n), 0)
    print(f"Device: {platform.node()} {platform.machine()} | Python {platform.python_version()} | {n} windows")
    print(f"{'classifier':28s} {'feat med':>9s} {'feat p95':>9s} {'pred med':>9s} {'pred p95':>9s} "
          f"{'per s':>8s} {'mem KB':>8s} {'size KB':>8s}")
    out = Path("benchmark_results.csv")
    new = not out.exists()
    with open(out, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "device", "classifier", "feature_ms_median", "feature_ms_p95", "infer_ms_median",
                        "infer_ms_p95", "throughput_per_s", "peak_mem_kb", "size_kb"])
        for name, (r, size) in results.items():
            print(f"{name:28s} {r['feature_ms_median']:9.4f} {r['feature_ms_p95']:9.4f} {r['infer_ms_median']:9.4f} "
                  f"{r['infer_ms_p95']:9.4f} {r['throughput_per_s']:8.0f} {r['peak_mem_kb']:8.1f} {size:8.1f}")
            w.writerow([round(time.time()), platform.node(), name, *r.values(), size])
    print(f"\nAppended to {out.resolve()}")
