import random

from smartpark.config import BAY_DEFAULTS
from smartpark.core.occupancy import OccupancyTracker
from smartpark.ml.classifier import EventClassifier
from smartpark.ml.features import FEATURE_NAMES, extract_features
from smartpark.ml.synthetic import generate_window

OCC = BAY_DEFAULTS["occupancy"]


def tracker():
    return OccupancyTracker(dict(OCC), EventClassifier(None, OCC["occupied_below_cm"], OCC["vacant_above_cm"]))


def feed_series(tr, values, t0=0.0, dt=0.1):
    events = []
    t = t0
    for v in values:
        events += tr.feed(v, t)
        t += dt
    return events, t


def test_features_shape():
    rng = random.Random(0)
    for kind in ("arrival", "departure", "transient", "noise"):
        f = extract_features(generate_window(kind, rng), 10, 12)
        assert len(f) == len(FEATURE_NAMES)
    assert extract_features([(0, None)] * 5, 10, 12)[-1] == 1.0


def test_arrival_then_departure():
    tr = tracker()
    evs, t = feed_series(tr, [30] * 30)
    assert tr.occupied is False and not evs
    ramp = [30 - i for i in range(25)] + [6] * 40
    evs, t = feed_series(tr, ramp, t)
    assert tr.occupied is True
    assert any(e.kind == "arrival" and e.state_changed for e in evs)
    evs, t = feed_series(tr, [6 + i for i in range(25)] + [30] * 40, t)
    assert tr.occupied is False
    dep = [e for e in evs if e.kind == "departure"]
    assert dep and dep[0].dwell_s is not None


def test_transient_does_not_flip_state():
    tr = tracker()
    _, t = feed_series(tr, [30] * 30)
    dip = [30, 25, 18, 9, 6, 6, 8, 15, 24, 30] + [30] * 40
    evs, _ = feed_series(tr, dip, t)
    assert tr.occupied is False
    assert any(e.kind == "transient" and not e.state_changed for e in evs)


def test_hysteresis_band_never_triggers():
    tr = tracker()
    _, t = feed_series(tr, [30] * 30)
    evs, _ = feed_series(tr, [11, 11.5, 10.5, 11.8] * 30, t)  # between 10 and 12 cm
    assert not evs and tr.occupied is False


def test_reconciliation_safety_net():
    class AlwaysNoise:
        occ, vac = 10, 12

        def __init__(self):
            from smartpark.ml.classifier import RuleClassifier
            self.rules = RuleClassifier(10, 12)

        def classify(self, w):
            from smartpark.ml.classifier import Prediction
            return Prediction("noise", 0.9, "model", 0, 0)

    tr = OccupancyTracker(dict(OCC), AlwaysNoise())
    _, t = feed_series(tr, [30] * 30)
    evs, _ = feed_series(tr, [5] * 100, t)       # car really parked, classifier wrong
    assert tr.occupied is True
    assert any(e.source == "reconciled" for e in evs)
