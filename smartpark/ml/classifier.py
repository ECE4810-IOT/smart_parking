"""Event classifier used on each bay node (edge inference).

Loads a trained scikit-learn model if one exists (created by ml/train.py);
otherwise falls back to a transparent rule-based classifier. The rule
classifier is also the BASELINE the trained models are compared against in
the report, which is how we justify using ML at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .features import FEATURE_NAMES, extract_features

log = logging.getLogger(__name__)


@dataclass
class Prediction:
    label: str
    confidence: float
    source: str          # "model" or "rules"
    feature_ms: float
    infer_ms: float


class RuleClassifier:
    """Hand-written thresholds - the 'no ML' baseline."""

    def __init__(self, occupied_below: float, vacant_above: float):
        self.occ = occupied_below
        self.vac = vacant_above
        self.idx = {n: i for i, n in enumerate(FEATURE_NAMES)}

    def predict(self, f: list[float]) -> tuple[str, float]:
        g = lambda name: f[self.idx[name]]  # noqa: E731
        if g("dropout_frac") > 0.5:
            return "noise", 0.7
        if g("n_crossings") >= 4 or (g("roughness") > 4.0 and g("time_below_s") < 1.0):
            return "noise", 0.65
        start_below = g("d_start") < self.occ
        end_below = g("frac_below_last") >= 0.7
        end_above = g("frac_below_last") <= 0.3 and g("d_end") > self.vac
        if not start_below and end_below:
            return "arrival", 0.9 if g("delta") < -(self.vac - self.occ) else 0.7
        if start_below and end_above:
            return "departure", 0.9 if g("delta") > (self.vac - self.occ) else 0.7
        if not start_below and end_above and g("d_min") < self.occ:
            return "transient", 0.85 if g("time_below_s") < 2.0 else 0.6
        return "noise", 0.5


class EventClassifier:
    def __init__(self, model_path: str | Path | None, occupied_below: float, vacant_above: float):
        self.occ, self.vac = occupied_below, vacant_above
        self.rules = RuleClassifier(occupied_below, vacant_above)
        self.model = None
        self.model_meta: dict = {}
        if model_path and Path(model_path).exists():
            try:
                import joblib  # type: ignore

                bundle = joblib.load(model_path)
                if list(bundle.get("features", [])) != list(FEATURE_NAMES):
                    raise ValueError("model was trained with different features")
                self.model = bundle["model"]
                self.model_meta = bundle.get("meta", {})
                log.info("Loaded ML model %s (%s)", model_path, self.model_meta.get("name", "?"))
            except Exception as e:  # missing sklearn/joblib or bad file
                log.warning("Could not load model %s (%s) - using rule-based classifier", model_path, e)
        else:
            log.info("No trained model found - using rule-based classifier")

    @property
    def source(self) -> str:
        return "model" if self.model is not None else "rules"

    def classify(self, window) -> Prediction:
        t0 = time.perf_counter()
        f = extract_features(window, self.occ, self.vac)
        t1 = time.perf_counter()
        if self.model is not None:
            proba = self.model.predict_proba([f])[0]
            i = max(range(len(proba)), key=proba.__getitem__)
            label, conf = str(self.model.classes_[i]), float(proba[i])
        else:
            label, conf = self.rules.predict(f)
        t2 = time.perf_counter()
        return Prediction(label, round(conf, 3), self.source,
                          round((t1 - t0) * 1000, 3), round((t2 - t1) * 1000, 3))
