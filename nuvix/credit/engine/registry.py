"""Versioned model registry for default-risk inference.

Two implementations satisfy one interface:

:class:`LogisticScorecard`
    The always-available default. A transparent logistic scorecard whose
    coefficients are **hand-calibrated from published underwriting
    relationships, not fitted to data**. It is stated plainly because the
    distinction matters: it gives sane, monotone, explainable risk ordering
    out of the box, and it must not be described as a trained model.

:class:`ArtifactModel`
    A real fitted estimator loaded from ``models/``, produced by
    ``scripts/train_models.py``. Drop the artifact in, point
    ``ACTIVE_PD_MODEL`` at its version, and inference switches over with no
    code change.

Why a registry at all
---------------------
A risk model is not a library import. It has a version that must appear on
every decision it makes, it is retrained on a different cadence to the code
that calls it, and the previous version has to stay loadable to explain a
decision made last quarter. The registry is what makes those three things
true, and it is why ``scikit-learn`` is an optional dependency rather than a
hard one -- the API boots and serves with no ML stack installed at all.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from django.conf import settings

logger = logging.getLogger(__name__)


class ModelUnavailable(RuntimeError):
    """The requested model version could not be loaded."""


@dataclass(frozen=True, slots=True)
class FeatureContribution:
    name: str
    value: float
    contribution: float


class PDModel(Protocol):
    """Probability-of-default estimator."""

    version: str
    kind: str

    def predict_proba(self, features: dict[str, float]) -> float: ...

    def explain(self, features: dict[str, float]) -> list[FeatureContribution]: ...


#: Feature contract. Every model in the registry consumes exactly these keys,
#: so a new model version can never silently change what the caller must
#: supply. Values are raw (un-scaled) -- scaling belongs inside the model.
FEATURE_NAMES: tuple[str, ...] = (
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
)

#: Reference points used to centre each feature before applying a weight.
#: Chosen as plausible population medians for the middle-income US consumer
#: the product serves, so a typical applicant sits near zero on every term.
_CENTRES: dict[str, float] = {
    "age": 40.0,
    "annual_income": 62_000.0,
    "loan_amount": 15_000.0,
    "credit_score": 670.0,
    "interest_rate": 14.0,
    "loan_term_months": 48.0,
    "dti_ratio": 0.30,
    "months_employed": 48.0,
    "has_mortgage": 0.0,
    "has_dependents": 0.0,
    "loan_purpose_code": 0.0,
}

#: Scales that turn each centred feature into roughly one "unit of risk", so
#: the weights below are directly comparable to one another.
_SCALES: dict[str, float] = {
    "age": 12.0,
    "annual_income": 30_000.0,
    "loan_amount": 12_000.0,
    "credit_score": 80.0,
    "interest_rate": 6.0,
    "loan_term_months": 24.0,
    "dti_ratio": 0.15,
    "months_employed": 36.0,
    "has_mortgage": 1.0,
    "has_dependents": 1.0,
    "loan_purpose_code": 1.0,
}

#: Log-odds weights. Signs encode the direction every underwriter agrees on:
#: higher score and longer tenure reduce default odds; higher DTI, rate and
#: loan size raise them.
#:
#: A known limitation, stated rather than buried: ``loan_purpose_code`` is a
#: *nominal* variable (business, home, auto...) carried here as an ordinal, so
#: a linear weight on it implies an ordering the categories do not have. It is
#: kept because the fitted models in the registry consume the same feature
#: contract, and it is given a deliberately negligible weight so the bad
#: encoding cannot meaningfully move a decision. The correct fix is one-hot
#: encoding, which belongs with the first genuinely fitted model rather than
#: in a hand-calibrated fallback.
_WEIGHTS: dict[str, float] = {
    "credit_score": -0.95,
    "dti_ratio": 0.70,
    "interest_rate": 0.55,
    "loan_amount": 0.30,
    "months_employed": -0.30,
    "age": -0.18,
    "annual_income": -0.35,
    "loan_term_months": 0.12,
    "has_mortgage": -0.15,
    "has_dependents": 0.10,
    "loan_purpose_code": 0.02,
}

#: Intercept set so that an applicant sitting at every centre lands near an
#: 11% probability of default -- the order of magnitude reported for
#: near-prime unsecured consumer lending.
_INTERCEPT = -2.10


def _sigmoid(x: float) -> float:
    # Clamped to avoid overflow on extreme inputs; beyond ±35 the sigmoid is
    # already indistinguishable from 0 or 1.
    if x < -35:
        return 0.0
    if x > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


class LogisticScorecard:
    """Transparent, dependency-free default-risk scorecard.

    Not a fitted model. Every coefficient is visible above and every
    prediction decomposes exactly into per-feature contributions, which is
    what makes adverse-action reason codes possible.
    """

    kind = "scorecard"

    def __init__(self, version: str = "pd-baseline-v1") -> None:
        self.version = version

    def _terms(self, features: dict[str, float]) -> dict[str, float]:
        terms = {}
        for name in FEATURE_NAMES:
            raw = float(features.get(name, _CENTRES[name]))
            standardised = (raw - _CENTRES[name]) / _SCALES[name]
            terms[name] = standardised * _WEIGHTS[name]
        return terms

    def predict_proba(self, features: dict[str, float]) -> float:
        return _sigmoid(_INTERCEPT + sum(self._terms(features).values()))

    def explain(self, features: dict[str, float]) -> list[FeatureContribution]:
        """Per-feature log-odds contributions, largest risk-increasing first."""

        terms = self._terms(features)
        contributions = [
            FeatureContribution(
                name=name,
                value=float(features.get(name, _CENTRES[name])),
                contribution=round(value, 4),
            )
            for name, value in terms.items()
        ]
        return sorted(contributions, key=lambda c: -c.contribution)


class ArtifactModel:
    """A fitted estimator persisted by ``scripts/train_models.py``.

    Loading is lazy and failure is loud: a corrupt or missing artifact raises
    :class:`ModelUnavailable` so the caller can fall back deliberately rather
    than serving predictions from a model that silently is not there.
    """

    kind = "fitted"

    def __init__(self, version: str, model_dir: Path) -> None:
        self.version = version
        self._path = Path(model_dir) / f"{version}.joblib"
        self._meta_path = Path(model_dir) / f"{version}.json"
        self._estimator = None
        self._metadata: dict = {}

    def load(self) -> ArtifactModel:
        try:
            import joblib
        except ImportError as exc:
            raise ModelUnavailable(
                "joblib is not installed; install requirements-ml.txt to serve "
                "fitted models."
            ) from exc
        if not self._path.exists():
            raise ModelUnavailable(f"No artifact at {self._path}")
        self._estimator = joblib.load(self._path)
        if self._meta_path.exists():
            self._metadata = json.loads(self._meta_path.read_text())
        return self

    def _vector(self, features: dict[str, float]):
        """Build the input row in the order the artifact was trained on.

        The column order comes from the artifact's own metadata, not from this
        module's constant, so a model trained on an older feature order still
        scores correctly instead of silently reading the wrong values into the
        wrong coefficients.

        A named DataFrame is used when pandas is available: scikit-learn
        pipelines fitted on a DataFrame match columns by name and warn when
        handed a bare array. The plain-list path is the fallback for an
        artifact served without pandas installed.
        """

        order = self._metadata.get("feature_names", list(FEATURE_NAMES))
        row = [float(features.get(name, _CENTRES.get(name, 0.0))) for name in order]

        try:
            import pandas as pd
        except ImportError:
            return [row]
        return pd.DataFrame([row], columns=order)

    def predict_proba(self, features: dict[str, float]) -> float:
        if self._estimator is None:
            self.load()
        return float(self._estimator.predict_proba(self._vector(features))[0][1])

    def explain(self, features: dict[str, float]) -> list[FeatureContribution]:
        """Contributions from the fitted model where it exposes them.

        Linear models give exact coefficients; tree ensembles expose only
        global importances, which are reported as such rather than dressed up
        as per-prediction attributions.
        """

        if self._estimator is None:
            self.load()
        order = self._metadata.get("feature_names", list(FEATURE_NAMES))
        weights = self._metadata.get("feature_importances")
        if not weights:
            return []
        return sorted(
            [
                FeatureContribution(
                    name=name,
                    value=float(features.get(name, 0.0)),
                    contribution=round(float(weight), 4),
                )
                for name, weight in zip(order, weights, strict=False)
            ],
            key=lambda c: -abs(c.contribution),
        )


@lru_cache(maxsize=8)
def get_pd_model(version: str | None = None) -> PDModel:
    """Return the active probability-of-default model.

    Cached because loading an artifact costs tens of milliseconds and the
    model is stateless once loaded. Falls back to the scorecard -- with a
    warning, never silently -- when a fitted artifact is configured but
    unavailable, so a bad deploy degrades instead of 500-ing.
    """

    version = version or settings.NUVIX["ACTIVE_PD_MODEL"]
    if version.startswith("pd-baseline"):
        return LogisticScorecard(version=version)

    try:
        return ArtifactModel(version, settings.NUVIX["MODEL_DIR"]).load()
    except ModelUnavailable as exc:
        logger.warning(
            "pd_model_fallback",
            extra={"requested_version": version, "reason": str(exc)},
        )
        return LogisticScorecard(version="pd-baseline-v1")
