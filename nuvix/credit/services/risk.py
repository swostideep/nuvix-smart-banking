"""Probability-of-default assessment and the lending decision it drives."""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from nuvix.accounts.models import User
from nuvix.core.money import ZERO
from nuvix.credit.engine.registry import get_pd_model
from nuvix.credit.models import CreditProfile, RiskAssessment, RiskDecision
from nuvix.credit.services.scoring import get_or_create_profile

#: Decision thresholds on probability of default. Policy, not modelling:
#: they live here (and are overridable per environment) because risk appetite
#: changes far more often than the model does.
APPROVE_BELOW = Decimal("0.08")
DECLINE_ABOVE = Decimal("0.25")

#: Risk grades. Bands are wider at the safe end because the difference between
#: a 1% and a 2% PD barely changes pricing, whereas 20% versus 30% does.
_GRADE_BANDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("0.02"), "A"),
    (Decimal("0.05"), "B"),
    (Decimal("0.10"), "C"),
    (Decimal("0.18"), "D"),
    (Decimal("0.30"), "E"),
    (Decimal("1.01"), "F"),
)

_PURPOSE_CODES = {
    "business": 0,
    "home": 1,
    "education": 2,
    "other": 3,
    "auto": 4,
    "debt_consolidation": 5,
    "medical": 6,
}

#: Human-readable adverse-action reasons, keyed by feature.
_REASON_TEXT = {
    "credit_score": "Credit score is below the level this product requires",
    "dti_ratio": "Debt-to-income ratio is too high",
    "interest_rate": "Requested rate implies higher risk pricing",
    "loan_amount": "Requested amount is large relative to income",
    "months_employed": "Length of employment is short",
    "annual_income": "Reported income is low for the requested amount",
    "age": "Limited credit-relevant history",
    "loan_term_months": "Requested term extends the risk window",
    "has_mortgage": "No secured credit on file",
    "has_dependents": "Household obligations reduce disposable income",
    "loan_purpose_code": "Stated purpose carries higher observed loss rates",
}


def grade_for(probability: Decimal) -> str:
    for ceiling, grade in _GRADE_BANDS:
        if probability < ceiling:
            return grade
    return "F"


def decide(probability: Decimal) -> str:
    if probability < APPROVE_BELOW:
        return RiskDecision.APPROVE
    if probability > DECLINE_ABOVE:
        return RiskDecision.DECLINE
    return RiskDecision.REFER


def monthly_debt_service(user: User) -> Decimal:
    """Sum of contractual minimums across the user's debts."""

    from nuvix.debt.models import Debt

    total = ZERO
    for debt in Debt.objects.alive().filter(user=user, balance__gt=0):
        total += debt.minimum_payment
    return total


def build_features(
    user: User,
    loan_amount: Decimal,
    interest_rate: Decimal,
    loan_term_months: int,
    loan_purpose: str = "other",
    credit_profile: CreditProfile | None = None,
) -> dict[str, float]:
    """Assemble the model's feature vector from stored state.

    DTI is computed from live debt minimums rather than taken as a declared
    field: a self-reported ratio is the number most likely to be wrong, and it
    is the second-heaviest feature in the model.
    """

    profile = getattr(user, "profile", None)
    credit = credit_profile or get_or_create_profile(user)

    monthly_income = Decimal(getattr(profile, "monthly_gross_income", ZERO) or ZERO)
    obligations = monthly_debt_service(user)
    dti = (obligations / monthly_income) if monthly_income > 0 else Decimal("0.6")

    return {
        "age": float(getattr(profile, "age", None) or 35),
        "annual_income": float(getattr(profile, "annual_income", ZERO) or 0),
        "loan_amount": float(loan_amount),
        "credit_score": float(credit.score),
        "interest_rate": float(interest_rate),
        "loan_term_months": float(loan_term_months),
        "dti_ratio": float(min(dti, Decimal("2"))),
        "months_employed": float(getattr(profile, "months_employed", 0) or 0),
        "has_mortgage": 1.0 if getattr(profile, "has_mortgage", False) else 0.0,
        "has_dependents": 1.0 if (getattr(profile, "dependents", 0) or 0) > 0 else 0.0,
        "loan_purpose_code": float(_PURPOSE_CODES.get(loan_purpose, 3)),
    }


def assess(
    user: User,
    loan_amount: Decimal,
    interest_rate: Decimal,
    loan_term_months: int,
    loan_purpose: str = "other",
    persist: bool = True,
    model_version: str | None = None,
) -> RiskAssessment:
    """Run PD inference and record the decision with its reasons."""

    model = get_pd_model(model_version)
    features = build_features(user, loan_amount, interest_rate, loan_term_months, loan_purpose)
    probability = Decimal(str(model.predict_proba(features))).quantize(Decimal("0.00001"))
    decision = decide(probability)

    # Reason codes are only meaningful on a non-approval, and only the
    # features that actually pushed risk *up* belong in them.
    reasons: list[dict] = []
    if decision != RiskDecision.APPROVE:
        for contribution in model.explain(features)[:4]:
            if contribution.contribution <= 0:
                continue
            reasons.append(
                {
                    "feature": contribution.name,
                    "value": contribution.value,
                    "impact": contribution.contribution,
                    "message": _REASON_TEXT.get(contribution.name, contribution.name),
                }
            )

    assessment = RiskAssessment(
        user=user,
        model_version=model.version,
        model_kind=model.kind,
        probability_of_default=probability,
        risk_grade=grade_for(probability),
        decision=decision,
        requested_amount=loan_amount,
        features=features,
        reason_codes=reasons,
    )
    if persist:
        assessment.save()
    else:
        assessment.created_at = timezone.now()
    return assessment
