"""Tests for the model registry and default-risk assessment."""

from __future__ import annotations

import importlib.util
from decimal import Decimal

import pytest

from nuvix.credit.engine.registry import (
    FEATURE_NAMES,
    ArtifactModel,
    LogisticScorecard,
    ModelUnavailable,
    get_pd_model,
)
from nuvix.credit.models import RiskDecision
from nuvix.credit.services import risk


def features(**overrides) -> dict[str, float]:
    base = {
        "age": 40.0,
        "annual_income": 62000.0,
        "loan_amount": 15000.0,
        "credit_score": 670.0,
        "interest_rate": 14.0,
        "loan_term_months": 48.0,
        "dti_ratio": 0.30,
        "months_employed": 48.0,
        "has_mortgage": 0.0,
        "has_dependents": 0.0,
        "loan_purpose_code": 3.0,
    }
    base.update(overrides)
    return base


class TestScorecard:
    def test_probability_is_a_probability(self):
        model = LogisticScorecard()
        assert 0.0 <= model.predict_proba(features()) <= 1.0

    def test_a_typical_applicant_sits_near_the_calibration_point(self):
        assert 0.05 < LogisticScorecard().predict_proba(features()) < 0.20

    def test_a_higher_score_lowers_default_risk(self):
        model = LogisticScorecard()
        assert model.predict_proba(features(credit_score=780)) < model.predict_proba(
            features(credit_score=560)
        )

    def test_a_higher_dti_raises_default_risk(self):
        model = LogisticScorecard()
        assert model.predict_proba(features(dti_ratio=0.6)) > model.predict_proba(
            features(dti_ratio=0.1)
        )

    def test_longer_tenure_lowers_default_risk(self):
        model = LogisticScorecard()
        assert model.predict_proba(features(months_employed=180)) < model.predict_proba(
            features(months_employed=2)
        )

    def test_extreme_inputs_do_not_overflow(self):
        model = LogisticScorecard()
        assert model.predict_proba(features(credit_score=1e9, dti_ratio=-1e9)) == 0.0
        assert model.predict_proba(features(credit_score=-1e9, dti_ratio=1e9)) == 1.0

    def test_missing_features_fall_back_to_the_centre(self):
        """A partial vector must not raise; it scores as the median applicant."""

        model = LogisticScorecard()
        at_centres = features(loan_purpose_code=0.0)
        assert model.predict_proba({}) == pytest.approx(
            model.predict_proba(at_centres), abs=1e-9
        )

    def test_loan_purpose_barely_moves_the_prediction(self):
        """Purpose is nominal but encoded ordinally -- its weight is near zero
        precisely so the encoding cannot do damage. See the registry docstring."""

        model = LogisticScorecard()
        spread = [model.predict_proba(features(loan_purpose_code=c)) for c in range(7)]
        assert max(spread) - min(spread) < 0.03

    def test_explanations_cover_every_feature_and_rank_by_risk(self):
        contributions = LogisticScorecard().explain(features(credit_score=540))
        assert {c.name for c in contributions} == set(FEATURE_NAMES)
        values = [c.contribution for c in contributions]
        assert values == sorted(values, reverse=True)
        assert contributions[0].name == "credit_score"


class TestRegistry:
    def test_baseline_version_resolves_to_the_scorecard(self):
        get_pd_model.cache_clear()
        model = get_pd_model("pd-baseline-v1")
        assert isinstance(model, LogisticScorecard)
        assert model.kind == "scorecard"

    def test_a_missing_artifact_falls_back_rather_than_raising(self):
        get_pd_model.cache_clear()
        model = get_pd_model("pd-does-not-exist-v9")
        assert isinstance(model, LogisticScorecard)

    def test_loading_a_missing_artifact_directly_raises(self, tmp_path):
        with pytest.raises(ModelUnavailable):
            ArtifactModel("nope-v1", tmp_path).load()


class TestGrading:
    @pytest.mark.parametrize(
        "probability,grade",
        [
            ("0.01", "A"),
            ("0.04", "B"),
            ("0.08", "C"),
            ("0.15", "D"),
            ("0.25", "E"),
            ("0.60", "F"),
        ],
    )
    def test_grade_bands(self, probability, grade):
        assert risk.grade_for(Decimal(probability)) == grade

    def test_decisions_follow_the_thresholds(self):
        assert risk.decide(Decimal("0.02")) == RiskDecision.APPROVE
        assert risk.decide(Decimal("0.15")) == RiskDecision.REFER
        assert risk.decide(Decimal("0.40")) == RiskDecision.DECLINE


@pytest.mark.django_db
class TestAssessment:
    def test_assessment_is_persisted_with_its_model_version(self, user, credit_profile):
        assessment = risk.assess(
            user,
            loan_amount=Decimal("8000"),
            interest_rate=Decimal("12.5"),
            loan_term_months=36,
            loan_purpose="debt_consolidation",
        )
        assert assessment.pk is not None
        assert assessment.model_version
        assert set(assessment.features) == set(FEATURE_NAMES)

    def test_declines_carry_reason_codes(self, user, credit_profile):
        credit_profile.score = 500
        credit_profile.save()
        assessment = risk.assess(
            user,
            loan_amount=Decimal("30000"),
            interest_rate=Decimal("32"),
            loan_term_months=72,
        )
        if assessment.decision != RiskDecision.APPROVE:
            assert assessment.reason_codes
            assert all("message" in r for r in assessment.reason_codes)

    def test_approvals_carry_no_reason_codes(self, user, credit_profile):
        credit_profile.score = 810
        credit_profile.save()
        assessment = risk.assess(
            user, loan_amount=Decimal("3000"), interest_rate=Decimal("7"), loan_term_months=24
        )
        if assessment.decision == RiskDecision.APPROVE:
            assert assessment.reason_codes == []

    def test_dti_is_computed_from_live_debts_not_declared(self, user, credit_profile, debts):
        built = risk.build_features(
            user, Decimal("5000"), Decimal("12"), 36, "other", credit_profile
        )
        # Minimums are 110 + 35 + 310 = 455 against $5,166.67 monthly gross.
        assert built["dti_ratio"] == pytest.approx(455 / (62000 / 12), abs=1e-4)

    def test_persist_false_leaves_no_row(self, user, credit_profile):
        from nuvix.credit.models import RiskAssessment

        risk.assess(
            user,
            loan_amount=Decimal("5000"),
            interest_rate=Decimal("10"),
            loan_term_months=24,
            persist=False,
        )
        assert RiskAssessment.objects.count() == 0


#: Scoped to the class that needs it, NOT applied at module level.
#: ``pytest.importorskip`` at module scope skips the *entire module* -- which
#: silently dropped all 28 tests in this file on any runner without the
#: optional ML stack, including the scorecard and decision tests that need no
#: ML at all. A test suite that quietly shrinks is worse than one that fails.
requires_sklearn = pytest.mark.skipif(
    importlib.util.find_spec("sklearn") is None,
    reason="fitted-model tests need requirements-ml.txt",
)


@requires_sklearn
class TestArtifactModel:
    """Round-trip a real fitted artifact through the registry.

    Skipped when the optional ML stack is absent, which is the point of the
    registry: the API is fully testable and fully servable without it.
    """

    @pytest.fixture
    def artifact_dir(self, tmp_path):
        import json

        import joblib
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler

        rng = np.random.default_rng(0)
        rows = rng.normal(size=(400, len(FEATURE_NAMES)))
        # Make one feature genuinely predictive so the model is not degenerate.
        labels = (rows[:, FEATURE_NAMES.index("dti_ratio")] > 0).astype(int)

        pipeline = Pipeline(
            [("scale", StandardScaler()), ("model", LogisticRegression(max_iter=500))]
        )
        pipeline.fit(rows, labels)

        joblib.dump(pipeline, tmp_path / "pd-test-v1.joblib")
        (tmp_path / "pd-test-v1.json").write_text(
            json.dumps(
                {
                    "version": "pd-test-v1",
                    "feature_names": list(FEATURE_NAMES),
                    "feature_importances": [0.1] * len(FEATURE_NAMES),
                }
            )
        )
        return tmp_path

    def test_a_fitted_artifact_is_loaded_and_served(self, artifact_dir):
        model = ArtifactModel("pd-test-v1", artifact_dir).load()
        assert model.kind == "fitted"
        assert 0.0 <= model.predict_proba(features()) <= 1.0

    def test_the_registry_picks_up_a_configured_artifact(self, artifact_dir, settings):
        settings.NUVIX = {**settings.NUVIX, "MODEL_DIR": artifact_dir}
        get_pd_model.cache_clear()
        model = get_pd_model("pd-test-v1")
        assert model.kind == "fitted"
        assert model.version == "pd-test-v1"
        get_pd_model.cache_clear()

    def test_importances_are_reported_from_metadata(self, artifact_dir):
        model = ArtifactModel("pd-test-v1", artifact_dir).load()
        contributions = model.explain(features())
        assert {c.name for c in contributions} == set(FEATURE_NAMES)

    def test_the_feature_order_comes_from_the_artifact_not_the_module(self, artifact_dir):
        """A model trained on an older order must still read its own columns."""

        import json

        reversed_order = list(reversed(FEATURE_NAMES))
        (artifact_dir / "pd-test-v1.json").write_text(
            json.dumps({"version": "pd-test-v1", "feature_names": reversed_order})
        )
        model = ArtifactModel("pd-test-v1", artifact_dir).load()
        frame = model._vector(features())
        assert list(frame.columns) == reversed_order
