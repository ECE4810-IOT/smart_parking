"""Per-bay occupancy state machine with ML event classification.

Pipeline for every sample:
    1. Hysteresis: a crossing is "below occupied_below" (vacant -> ?) or
       "above vacant_above" (occupied -> ?). Values in between never trigger.
    2. On a crossing, capture a window (window_pre_s before .. window_post_s after).
    3. Classify the window: arrival / departure / transient / noise.
    4. Only a confident arrival/departure changes state. A transient (person
       walking through, a hand) is reported as movement but does NOT flip the
       bay -> this is what removes false "car parked" alerts.
    5. Safety net ("reconciliation"): if the classifier suppressed a real change,
       and the readings stay on the other side for reconcile_s, the state is
       corrected anyway and the miss is logged (a metric for the report).
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass, field


@dataclass
class BayEvent:
    kind: str                 # arrival | departure | transient | noise
    state_changed: bool
    occupied: bool | None     # state AFTER the event
    confidence: float
    source: str               # model | rules | reconciled
    feature_ms: float = 0.0
    infer_ms: float = 0.0
    dwell_s: float | None = None
    distance: float | None = None
    ts: float = field(default_factory=time.time)
    window: list | None = None  # raw samples (kept only for recording)

    def to_dict(self, include_window: bool = False) -> dict:
        d = asdict(self)
        if not include_window:
            d.pop("window", None)
        return d


class OccupancyTracker:
    def __init__(self, occ_cfg: dict, classifier):
        self.occ = occ_cfg["occupied_below_cm"]
        self.vac = occ_cfg["vacant_above_cm"]
        if self.vac <= self.occ:
            raise ValueError("vacant_above_cm must be greater than occupied_below_cm")
        self.pre = occ_cfg["window_pre_s"]
        self.post = occ_cfg["window_post_s"]
        self.reconcile_s = occ_cfg["reconcile_s"]
        self.min_conf = occ_cfg["min_confidence"]
        self.classifier = classifier
        self.buffer: deque = deque()            # (monotonic_t, distance|None)
        self.occupied: bool | None = None       # confirmed state
        self.occupied_since: float | None = None  # wall clock
        self.capture_at: float | None = None    # monotonic time of crossing
        self.opposite_since: float | None = None
        self.last_distance: float | None = None
        self.stats = {"captures": 0, "suppressed": 0, "reconciled": 0}

    # ------------------------------------------------------------------
    def capturing(self) -> bool:
        return self.capture_at is not None

    def set_threshold(self, occupied_below: float, vacant_above: float | None = None):
        vac = vacant_above if vacant_above is not None else occupied_below * 1.2
        if occupied_below <= 0 or vac <= occupied_below:
            raise ValueError("invalid thresholds")
        self.occ, self.vac = occupied_below, vac
        self.classifier.occ = self.classifier.rules.occ = occupied_below
        self.classifier.vac = self.classifier.rules.vac = vac

    def _is_opposite(self, d: float) -> bool:
        return d < self.occ if not self.occupied else d > self.vac

    def feed(self, d: float | None, t: float | None = None) -> list[BayEvent]:
        t = time.monotonic() if t is None else t
        self.buffer.append((t, d))
        horizon = max(self.pre + self.post, self.reconcile_s) + 2.0
        while self.buffer and self.buffer[0][0] < t - horizon:
            self.buffer.popleft()
        if d is not None:
            self.last_distance = d
        events: list[BayEvent] = []

        # 1. establish the initial state from the first few valid samples
        if self.occupied is None:
            valid = [x for _, x in self.buffer if x is not None]
            if len(valid) >= 3:
                self.occupied = statistics.median(valid[-3:]) < self.occ
                self.occupied_since = time.time() if self.occupied else None
            return events

        if d is not None:
            # 2. track how long readings have disagreed with the confirmed state
            if self._is_opposite(d):
                self.opposite_since = self.opposite_since or t
            elif (not self.occupied and d > self.vac) or (self.occupied and d < self.occ):
                self.opposite_since = None
            # 3. start a capture on a hysteresis crossing
            if self.capture_at is None and self._is_opposite(d):
                self.capture_at = t
                self.stats["captures"] += 1

        # 4. capture complete -> classify
        if self.capture_at is not None and t >= self.capture_at + self.post:
            events.append(self._classify_capture(t))
            self.capture_at = None

        # 5. reconciliation safety net
        if (self.capture_at is None and self.opposite_since is not None
                and t - self.opposite_since >= self.reconcile_s):
            events.append(self._change_state(not self.occupied, "reconciled", 1.0))
            self.stats["reconciled"] += 1
            self.opposite_since = None
        return events

    def _classify_capture(self, t: float) -> BayEvent:
        c = self.capture_at
        window = [(round(ts - c, 3), d) for ts, d in self.buffer if c - self.pre <= ts <= c + self.post]
        pred = self.classifier.classify(window)
        wants = "arrival" if not self.occupied else "departure"
        if pred.label == wants and pred.confidence >= self.min_conf:
            ev = self._change_state(not self.occupied, pred.source, pred.confidence, pred.label)
        else:
            label = pred.label
            if label in ("arrival", "departure"):  # inconsistent with state or low confidence
                label = "noise" if label != wants else label
            self.stats["suppressed"] += 1
            ev = BayEvent(label, False, self.occupied, pred.confidence, pred.source,
                          distance=self.last_distance)
        ev.feature_ms, ev.infer_ms = pred.feature_ms, pred.infer_ms
        ev.window = window
        return ev

    def _change_state(self, occupied: bool, source: str, conf: float, kind: str | None = None) -> BayEvent:
        now = time.time()
        dwell = None
        if not occupied and self.occupied_since:
            dwell = round(now - self.occupied_since, 1)
        self.occupied = occupied
        self.occupied_since = now if occupied else None
        self.opposite_since = None
        return BayEvent(kind or ("arrival" if occupied else "departure"), True, occupied, conf,
                        source, dwell_s=dwell, distance=self.last_distance)
