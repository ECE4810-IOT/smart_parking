"""Train, compare and optimise event classifiers (Activity 2).

    python -m smartpark train --data dataset/windows.csv --out models

What it does (all numbers go into models/report.md for the report):
  1. recompute features from the raw windows (so features can evolve)
  2. stratified 75/25 train/test split; 5-fold CV grid search on the train set for
     k-NN, Decision Tree, Random Forest and SVM (Lectures 6b/6c)
  3. evaluate on the held-out test set: accuracy, macro-F1, per-class recall,
     confusion matrix
  4. compare against the rule-based baseline (is ML actually worth it?)
  5. efficiency: single-sample inference latency (median / p95), model size
  6. optimisation: permutation feature importance -> retrain the best model on
     the top features, and a compact depth-limited tree; pick the deployed model
     on F1 first, then latency
  7. leave-one-bay-out test if data comes from several bays (generalisation)
"""

from __future__ import annotations

import csv
import io
import json
import statistics
import time
from pathlib import Path

from . import LABELS
from .classifier import RuleClassifier
from .features import FEATURE_NAMES, extract_features

OCC, VAC = 10.0, 12.0


def load(path: str, allow_synthetic: bool):
    X, y, groups = [], [], []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["bay"] == "SYNTHETIC" and not allow_synthetic:
                raise SystemExit("Refusing SYNTHETIC data - pass --allow-synthetic for pipeline tests only.")
            if not row["label"]:
                continue
            win = [tuple(p) for p in json.loads(row["window"])]
            X.append(extract_features(win, OCC, VAC))
            y.append(row["label"])
            groups.append(row["bay"])
    return X, y, groups


def _latency(model, X, n=300):
    samples = []
    for i in range(n):
        x = [X[i % len(X)]]
        t = time.perf_counter()
        model.predict_proba(x)
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    return round(statistics.median(samples), 4), round(samples[int(0.95 * (n - 1))], 4)


def _size_kb(model) -> float:
    import joblib

    buf = io.BytesIO()
    joblib.dump(model, buf)
    return round(len(buf.getvalue()) / 1024, 1)


class _Subset:
    """Wraps a model trained on a subset of features so it accepts full vectors."""

    def __init__(self, model, idx):
        self.model, self.idx = model, idx
        self.classes_ = model.classes_

    def predict_proba(self, X):
        return self.model.predict_proba([[row[i] for i in self.idx] for row in X])

    def predict(self, X):
        return self.model.predict([[row[i] for i in self.idx] for row in X])


def train(data: str, out: str, allow_synthetic: bool = False):
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)
    import joblib
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
    from sklearn.model_selection import GridSearchCV, StratifiedKFold, train_test_split
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC
    from sklearn.tree import DecisionTreeClassifier

    X, y, groups = load(data, allow_synthetic)
    synthetic = any(g == "SYNTHETIC" for g in groups)
    counts = {c: y.count(c) for c in sorted(set(y))}
    print(f"Loaded {len(y)} windows: {counts}")
    if min(counts.values()) < 10:
        raise SystemExit("Need at least 10 windows per class (40+ recommended).")
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, stratify=y, random_state=42)
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

    candidates = {
        "k-NN": (make_pipeline(StandardScaler(), KNeighborsClassifier()),
                 {"kneighborsclassifier__n_neighbors": [3, 5, 7, 9],
                  "kneighborsclassifier__weights": ["uniform", "distance"]}),
        "Decision Tree": (DecisionTreeClassifier(random_state=42),
                          {"max_depth": [3, 4, 6, None], "min_samples_leaf": [1, 3, 5]}),
        "Random Forest": (RandomForestClassifier(random_state=42, n_jobs=1),
                          {"n_estimators": [25, 50, 100], "max_depth": [None, 6]}),
        "SVM (RBF)": (make_pipeline(StandardScaler(), SVC(probability=True, random_state=42)),
                      {"svc__C": [0.5, 1, 10], "svc__gamma": ["scale", 0.1]}),
    }
    rows, fitted = [], {}
    for name, (est, grid) in candidates.items():
        t0 = time.perf_counter()
        gs = GridSearchCV(est, grid, cv=cv, scoring="f1_macro", n_jobs=1).fit(Xtr, ytr)
        train_s = time.perf_counter() - t0
        pred = gs.best_estimator_.predict(Xte)
        med, p95 = _latency(gs.best_estimator_, Xte)
        fitted[name] = gs.best_estimator_
        rows.append({"model": name, "cv_f1": round(gs.best_score_, 4), "test_acc": round(accuracy_score(yte, pred), 4),
                     "test_f1": round(f1_score(yte, pred, average="macro"), 4), "infer_ms_median": med,
                     "infer_ms_p95": p95, "size_kb": _size_kb(gs.best_estimator_), "train_s": round(train_s, 2),
                     "params": gs.best_params_})
        print(f"  {name:14s} CV-F1 {gs.best_score_:.3f}  test-F1 {rows[-1]['test_f1']:.3f}  {med} ms")

    # rule-based baseline on the same test set
    rules = RuleClassifier(OCC, VAC)
    t0 = time.perf_counter()
    rpred = [rules.predict(x)[0] for x in Xte]
    rule_ms = (time.perf_counter() - t0) * 1000 / len(Xte)
    rows.append({"model": "Rule baseline", "cv_f1": None, "test_acc": round(accuracy_score(yte, rpred), 4),
                 "test_f1": round(f1_score(yte, rpred, average="macro"), 4), "infer_ms_median": round(rule_ms, 4),
                 "infer_ms_p95": None, "size_kb": 0, "train_s": 0, "params": {}})

    # optimisation 1: feature selection on the best model
    best_name = max((r for r in rows if r["model"] in fitted), key=lambda r: (r["test_f1"], -r["infer_ms_median"]))["model"]
    best = fitted[best_name]
    imp = permutation_importance(best, Xte, yte, n_repeats=10, random_state=42, scoring="f1_macro")
    order = sorted(range(len(FEATURE_NAMES)), key=lambda i: -imp.importances_mean[i])
    importance = [(FEATURE_NAMES[i], round(float(imp.importances_mean[i]), 4)) for i in order]
    top_idx = sorted(order[:6])
    from sklearn.base import clone
    sel = clone(best).fit([[r[i] for i in top_idx] for r in Xtr], ytr)
    sel_wrapped = _Subset(sel, top_idx)
    spred = sel_wrapped.predict(Xte)
    med, p95 = _latency(sel_wrapped, Xte)
    fitted[f"{best_name} (top-6 features)"] = sel_wrapped
    rows.append({"model": f"{best_name} (top-6 features)", "cv_f1": None, "test_acc": round(accuracy_score(yte, spred), 4),
                 "test_f1": round(f1_score(yte, spred, average="macro"), 4), "infer_ms_median": med,
                 "infer_ms_p95": p95, "size_kb": _size_kb(sel), "train_s": None,
                 "params": {"features": [FEATURE_NAMES[i] for i in top_idx]}})

    # optimisation 2: compact tree (smallest / fastest)
    compact = DecisionTreeClassifier(max_depth=4, random_state=42).fit(Xtr, ytr)
    cpred = compact.predict(Xte)
    med, p95 = _latency(compact, Xte)
    fitted["Compact tree (depth 4)"] = compact
    rows.append({"model": "Compact tree (depth 4)", "cv_f1": None, "test_acc": round(accuracy_score(yte, cpred), 4),
                 "test_f1": round(f1_score(yte, cpred, average="macro"), 4), "infer_ms_median": med,
                 "infer_ms_p95": p95, "size_kb": _size_kb(compact), "train_s": None, "params": {"max_depth": 4}})

    # choose deployed model: best F1 (within 1 %), then fastest
    ml_rows = [r for r in rows if r["model"] in fitted]
    top_f1 = max(r["test_f1"] for r in ml_rows)
    chosen = min((r for r in ml_rows if r["test_f1"] >= top_f1 - 0.01), key=lambda r: r["infer_ms_median"])
    model = fitted[chosen["model"]]
    cm = confusion_matrix(yte, model.predict(Xte), labels=[c for c in LABELS if c in counts])
    report_txt = classification_report(yte, model.predict(Xte), digits=3)

    # generalisation: leave-one-bay-out
    lobo = {}
    real_groups = sorted(set(groups))
    if len(real_groups) >= 2:
        for g in real_groups:
            tr = [i for i, gg in enumerate(groups) if gg != g]
            te = [i for i, gg in enumerate(groups) if gg == g]
            if len(set(y[i] for i in tr)) < len(counts):
                continue
            m = clone(best).fit([X[i] for i in tr], [y[i] for i in tr])
            lobo[g] = round(f1_score([y[i] for i in te], m.predict([X[i] for i in te]), average="macro"), 4)

    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    bundle = {"model": model, "features": list(FEATURE_NAMES),
              "meta": {"name": chosen["model"], "test_f1": chosen["test_f1"], "trained": time.time(),
                       "synthetic": synthetic, "n": len(y)}}
    joblib.dump(bundle, outp / "event_model.joblib")
    (outp / "report.json").write_text(json.dumps({"rows": rows, "chosen": chosen["model"], "importance": importance,
                                                  "confusion_matrix": cm.tolist(), "labels": list(counts),
                                                  "leave_one_bay_out_f1": lobo, "synthetic": synthetic}, indent=2))
    _write_markdown(outp / "report.md", rows, chosen["model"], importance, cm, list(counts), report_txt, lobo,
                    synthetic, counts)
    _plot_confusion(outp / "confusion_matrix.png", cm, [c for c in LABELS if c in counts], chosen["model"])
    print(f"\nDeployed model: {chosen['model']}  ->  {outp / 'event_model.joblib'}")
    print(f"Full comparison: {outp / 'report.md'}")
    if synthetic:
        print("WARNING: trained on SYNTHETIC data - do not report these numbers.")


def _write_markdown(path, rows, chosen, importance, cm, labels, report_txt, lobo, synthetic, counts):
    L = []
    if synthetic:
        L.append("> **SYNTHETIC DATA - pipeline test only. Do not use these numbers in the report.**\n")
    L.append(f"# Event classifier comparison\n\nDataset: {sum(counts.values())} windows {counts}\n")
    L.append("| Model | CV macro-F1 | Test accuracy | Test macro-F1 | Inference median (ms) | p95 (ms) | Size (KB) | Train (s) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        mark = " **(deployed)**" if r["model"] == chosen else ""
        L.append(f"| {r['model']}{mark} | {r['cv_f1'] if r['cv_f1'] is not None else '-'} | {r['test_acc']} | "
                 f"{r['test_f1']} | {r['infer_ms_median']} | {r['infer_ms_p95'] if r['infer_ms_p95'] is not None else '-'} | "
                 f"{r['size_kb']} | {r['train_s'] if r['train_s'] is not None else '-'} |")
    L.append("\n## Best hyper-parameters\n")
    for r in rows:
        if r["params"]:
            L.append(f"- **{r['model']}**: `{r['params']}`")
    L.append("\n## Permutation feature importance (best model)\n")
    L.append("| Feature | Importance (drop in macro-F1) |\n|---|---|")
    L += [f"| {n} | {v} |" for n, v in importance]
    L.append(f"\n## Confusion matrix - {chosen}\n")
    L.append("| actual \\ predicted | " + " | ".join(labels) + " |")
    L.append("|---" * (len(labels) + 1) + "|")
    for lab, row in zip(labels, cm):
        L.append(f"| {lab} | " + " | ".join(str(v) for v in row) + " |")
    L.append(f"\n```\n{report_txt}\n```")
    if lobo:
        L.append("\n## Leave-one-bay-out macro-F1 (generalisation to an unseen bay)\n")
        L += [f"- {g}: {v}" for g, v in lobo.items()]
    Path(path).write_text("\n".join(L) + "\n")


def _plot_confusion(path, cm, labels, title):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, ax = plt.subplots(figsize=(5, 4.2), dpi=150)
    ax.imshow(cm, cmap="Blues")
    ax.set_xticks(range(len(labels)), labels, rotation=30, ha="right")
    ax.set_yticks(range(len(labels)), labels)
    for i in range(len(labels)):
        for j in range(len(labels)):
            ax.text(j, i, int(cm[i][j]), ha="center", va="center",
                    color="white" if cm[i][j] > cm.max() / 2 else "black")
    ax.set_xlabel("Predicted")
    ax.set_ylabel("Actual")
    ax.set_title(f"Confusion matrix - {title}", fontsize=10)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
