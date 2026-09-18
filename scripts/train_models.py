#!/usr/bin/env python
"""Train and persist the default-risk model.

This is the offline half of the credit-intelligence story. It reproduces --
as a reviewable, re-runnable script rather than a notebook -- the two models
the original NuviX prototype explored in Jupyter:

* a gradient-boosted tree on the tabular features, and
* a small neural network, the scikit-learn equivalent of the PyTorch MLP.

Both are trained, both are scored on a held-out split, and the better one by
ROC-AUC is written to ``models/`` alongside a metadata file. The API picks it
up through :mod:`nuvix.credit.engine.registry` when ``ACTIVE_PD_MODEL`` names
its version -- no code change, and the previous version stays loadable so a
decision made under it can still be explained.

Why a script and not the notebook
---------------------------------
A notebook cannot be code-reviewed, cannot run in CI, and its output depends
on the order someone happened to click cells in. The modelling here is the
same; what changes is that a second engineer can read it, run it, and get the
same numbers.

Usage::

    pip install -r requirements-ml.txt
    python scripts/train_models.py --data data/Loan_default.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

#: Must match nuvix.credit.engine.registry.FEATURE_NAMES exactly, and in the
#: same order. A model trained on a different vector than the one served is
#: the classic silent-wrong-answer failure in production ML, so the contract
#: is asserted at the top of ``main`` rather than trusted.
FEATURE_NAMES = [
    "age",
    "annual_income",
    "loan_amount",
    "credit_score",
    "interest_rate",
    "loan_term_months",
    "dti_ratio",
    "months_employed",
    "has_mortgage",
    "has_dependents",
    "loan_purpose_code",
]

#: Maps the public dataset's column names onto the feature contract.
COLUMN_MAP = {
    "Age": "age",
    "Income": "annual_income",
    "LoanAmount": "loan_amount",
    "CreditScore": "credit_score",
    "InterestRate": "interest_rate",
    "LoanTerm": "loan_term_months",
    "DTIRatio": "dti_ratio",
    "MonthsEmployed": "months_employed",
    "HasMortgage": "has_mortgage",
    "HasDependents": "has_dependents",
    "LoanPurpose": "loan_purpose_code",
}

PURPOSE_CODES = {
    "Business": 0,
    "Home": 1,
    "Education": 2,
    "Other": 3,
    "Auto": 4,
    "Debt Consolidation": 5,
    "Medical": 6,
}


def require_dependencies() -> None:
    """Fail early, and with an instruction rather than a traceback.

    The ML stack is an *optional* dependency of this project: the API serves
    predictions without it. Someone running this script for the first time
    should be told that in one line, not shown an ImportError from deep
    inside a helper.
    """

    missing = []
    for module in ("numpy", "pandas", "sklearn", "joblib"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        raise SystemExit(
            f"Missing ML dependencies: {', '.join(missing)}\n"
            "Install them with:\n"
            "    pip install -r requirements-ml.txt"
        )


def load_dataset(path: Path):
    """Read the public dataset and map it onto the served feature contract."""

    import pandas as pd

    if not path.exists():
        raise SystemExit(
            f"No dataset at {path}.\n\n"
            "This script expects the public 'Loan Default Prediction' CSV, the\n"
            "same data the original NuviX notebooks used. Download it and place\n"
            "it at that path, or pass --data.\n\n"
            "Without it the API still works: the registry falls back to the\n"
            "transparent scorecard in nuvix/credit/engine/registry.py."
        )

    frame = pd.read_csv(path)

    missing = set(COLUMN_MAP) - set(frame.columns)
    if missing:
        raise SystemExit(f"Dataset is missing expected columns: {sorted(missing)}")
    if "Default" not in frame.columns:
        raise SystemExit("Dataset has no 'Default' target column.")

    frame = frame.rename(columns=COLUMN_MAP)
    frame["has_mortgage"] = frame["has_mortgage"].map({"Yes": 1, "No": 0}).fillna(0)
    frame["has_dependents"] = frame["has_dependents"].map({"Yes": 1, "No": 0}).fillna(0)
    frame["loan_purpose_code"] = frame["loan_purpose_code"].map(PURPOSE_CODES).fillna(3)

    features = frame[FEATURE_NAMES].astype(float)
    target = frame["Default"].astype(int)
    return features, target


def build_candidates() -> dict:
    """Two models, deliberately different in kind.

    The boosted tree is the strong tabular baseline. The MLP stands in for the
    PyTorch network the prototype used -- the same architecture idea (two
    hidden layers, ReLU, a sigmoid output), expressed in the library the rest
    of the stack already depends on rather than pulling a deep-learning
    runtime into a service that makes one prediction per request.
    """

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return {
        "pd-gbdt-v1": Pipeline(
            [
                ("impute", SimpleImputer(strategy="median")),
                (
                    "model",
                    HistGradientBoostingClassifier(
                        max_iter=300,
                        learning_rate=0.06,
                        max_depth=6,
                        l2_regularization=0.1,
                        random_state=42,
                    ),
                ),
            ]
        ),
        "pd-mlp-v1": Pipeline(
            [
                ("impute", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
                (
                    "model",
                    MLPClassifier(
                        hidden_layer_sizes=(64, 32),
                        activation="relu",
                        solver="adam",
                        alpha=1e-3,
                        learning_rate_init=1e-3,
                        max_iter=500,
                        early_stopping=True,
                        random_state=42,
                    ),
                ),
            ]
        ),
    }


def permutation_importance_for(pipeline, x_test, y_test) -> list[float]:
    """Model-agnostic feature importance.

    Permutation importance rather than a tree's built-in
    ``feature_importances_`` so the number means the same thing for the boosted
    tree and the neural network, and the registry can report it uniformly. It
    is measured on the held-out split: importance computed on training data
    measures what the model memorised, not what it uses.
    """

    from sklearn.inspection import permutation_importance

    result = permutation_importance(
        pipeline, x_test, y_test, n_repeats=5, random_state=42, scoring="roc_auc"
    )
    return [float(value) for value in result.importances_mean]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=BASE_DIR / "data" / "Loan_default.csv")
    parser.add_argument("--out", type=Path, default=BASE_DIR / "models")
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    # Fail before doing any work if the served contract has drifted.
    from nuvix.credit.engine.registry import FEATURE_NAMES as SERVED_FEATURES

    if list(SERVED_FEATURES) != FEATURE_NAMES:
        raise SystemExit(
            "Feature contract mismatch between this script and the registry.\n"
            f"  script:   {FEATURE_NAMES}\n"
            f"  registry: {list(SERVED_FEATURES)}\n"
            "A model trained on a different vector than the one served will "
            "produce confident, silently wrong predictions."
        )

    require_dependencies()

    import joblib
    from sklearn.metrics import brier_score_loss, roc_auc_score
    from sklearn.model_selection import train_test_split

    features, target = load_dataset(args.data)
    print(f"Loaded {len(features):,} rows, {target.mean():.2%} default rate")

    x_train, x_test, y_train, y_test = train_test_split(
        features, target, test_size=args.test_size, random_state=args.seed, stratify=target
    )

    args.out.mkdir(parents=True, exist_ok=True)
    results = {}

    for version, pipeline in build_candidates().items():
        print(f"\nTraining {version} ...")
        pipeline.fit(x_train, y_train)
        probabilities = pipeline.predict_proba(x_test)[:, 1]

        auc = roc_auc_score(y_test, probabilities)
        # Brier score alongside AUC on purpose. AUC only measures ranking; a
        # lending decision uses the probability itself against a threshold, so
        # calibration matters as much as discrimination.
        brier = brier_score_loss(y_test, probabilities)
        print(f"  ROC-AUC {auc:.4f}   Brier {brier:.4f}")
        results[version] = {"pipeline": pipeline, "auc": auc, "brier": brier}

    best_version = max(results, key=lambda name: results[name]["auc"])
    print(f"\nBest by ROC-AUC: {best_version}")

    for version, result in results.items():
        artifact = args.out / f"{version}.joblib"
        joblib.dump(result["pipeline"], artifact)

        importances = permutation_importance_for(result["pipeline"], x_test, y_test)
        metadata = {
            "version": version,
            "trained_on": date.today().isoformat(),
            "dataset": str(args.data.name),
            "rows": int(len(features)),
            "default_rate": float(target.mean()),
            "feature_names": FEATURE_NAMES,
            "feature_importances": importances,
            "metrics": {"roc_auc": float(result["auc"]), "brier": float(result["brier"])},
            "is_best": version == best_version,
        }
        (args.out / f"{version}.json").write_text(json.dumps(metadata, indent=2))
        print(f"  wrote {artifact.name} and {version}.json")

    print(f"\nActivate with:\n    export ACTIVE_PD_MODEL={best_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
