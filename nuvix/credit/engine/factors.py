"""The credit-score factor model.

NuviX scores a user from the same five factor families the US bureaus use, at
their published weights. The point is not to replicate a bureau score to the
point -- that is not possible without the bureau's data -- but to produce a
score that *moves the way a real one moves*, so that the simulator can answer
"if I pay this card down, what happens?" with a number the customer will
recognise when their real score updates.

Every factor returns a sub-score in ``[0, 1]`` together with a plain-English
reason. Keeping the model additive and transparent is a deliberate constraint:
under US fair-lending rules an adverse decision must come with specific reason
codes, and a gradient-boosted ensemble cannot produce them honestly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

#: Bureau-published factor weights. They sum to 1.0.
WEIGHTS: dict[str, Decimal] = {
    "payment_history": Decimal("0.35"),
    "utilization": Decimal("0.30"),
    "credit_age": Decimal("0.15"),
    "credit_mix": Decimal("0.10"),
    "new_credit": Decimal("0.10"),
}

SCORE_MIN = 300
SCORE_MAX = 850
SCORE_RANGE = SCORE_MAX - SCORE_MIN

#: Ten years of history is where the age factor saturates.
AGE_SATURATION_MONTHS = 120


@dataclass(frozen=True, slots=True)
class CreditInputs:
    """Everything the factor model reads.

    Deliberately flat and primitive: these values come from a bureau pull, a
    linked-account sweep or a simulation, and the model must not care which.
    """

    on_time_payment_rate: Decimal = Decimal("1.0")
    late_payments_30d: int = 0
    late_payments_90d: int = 0
    derogatory_marks: int = 0

    total_balance: Decimal = Decimal("0")
    total_credit_limit: Decimal = Decimal("0")
    #: Highest utilisation on any single card. Bureaus penalise one maxed card
    #: even when the aggregate looks healthy, so it is scored separately.
    max_single_card_utilization: Decimal = Decimal("0")

    oldest_account_months: int = 0
    average_account_age_months: int = 0

    #: Distinct account *types* (card, auto, student, mortgage...), not count.
    credit_mix_types: int = 1
    open_accounts: int = 1

    hard_inquiries_12m: int = 0
    accounts_opened_24m: int = 0


@dataclass(frozen=True, slots=True)
class FactorScore:
    key: str
    label: str
    weight: Decimal
    sub_score: Decimal
    points_earned: int
    points_available: int
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    score: int
    band: str
    factors: tuple[FactorScore, ...]

    @property
    def biggest_opportunity(self) -> FactorScore | None:
        """The factor with the most points left on the table.

        This is what the app leads with: telling someone their score is 640 is
        information; telling them 74 points sit in their utilisation is advice.
        """

        ranked = sorted(self.factors, key=lambda f: -f.points_available)
        return ranked[0] if ranked and ranked[0].points_available > 0 else None


def band_for(score: int) -> str:
    if score >= 800:
        return "excellent"
    if score >= 740:
        return "very_good"
    if score >= 670:
        return "good"
    if score >= 580:
        return "fair"
    return "poor"


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))


def _payment_history(inputs: CreditInputs) -> tuple[Decimal, str]:
    """Squared on-time rate, then explicit penalties for real delinquencies.

    Squaring matters: 95% on-time sounds excellent and is not -- it is roughly
    one missed payment a year, which a bureau treats far more harshly than the
    linear reading suggests.

    A file with no history scores zero here rather than inheriting the
    default on-time rate of 1.0. Awarding a perfect payment record to someone
    who has never made a payment is how a scoring model hands a brand-new
    consumer a mid-600s score, and every downstream decision built on that
    number is then wrong.
    """

    if inputs.oldest_account_months <= 0:
        return Decimal("0"), (
            "No payment history on file yet. This factor is worth more than any "
            "other -- it starts building with your first on-time payment."
        )

    base = _clamp(Decimal(inputs.on_time_payment_rate)) ** 2
    penalty = (
        Decimal("0.10") * inputs.late_payments_30d
        + Decimal("0.20") * inputs.late_payments_90d
        + Decimal("0.30") * inputs.derogatory_marks
    )
    score = _clamp(base - penalty)

    if inputs.derogatory_marks:
        reason = (
            f"{inputs.derogatory_marks} derogatory mark(s) are the single largest "
            "drag on your score."
        )
    elif inputs.late_payments_90d or inputs.late_payments_30d:
        reason = (
            f"{inputs.late_payments_30d + inputs.late_payments_90d} late payment(s) "
            "are holding this factor down. They fade with every on-time month."
        )
    elif score >= Decimal("0.98"):
        reason = "A spotless payment record -- your strongest factor."
    else:
        reason = "Your payment record is good; keep every account current."
    return score, reason


def _utilization(inputs: CreditInputs) -> tuple[Decimal, str]:
    """Piecewise on aggregate utilisation, then capped by the worst card.

    The curve is stepped rather than linear because scoring models are: the
    difference between 29% and 31% is worth far more than the difference
    between 60% and 62%.
    """

    if inputs.total_credit_limit <= 0:
        return Decimal("0.35"), (
            "No revolving credit limit on file. A secured card is usually the "
            "fastest way to start building this factor."
        )

    ratio = Decimal(inputs.total_balance) / Decimal(inputs.total_credit_limit)

    if ratio <= Decimal("0.01"):
        # Near-zero usage scores well but not perfectly: a card that is never
        # used reports no activity, and no activity builds nothing.
        base = Decimal("0.90")
    elif ratio <= Decimal("0.09"):
        base = Decimal("1.00")
    elif ratio <= Decimal("0.29"):
        base = Decimal("0.85")
    elif ratio <= Decimal("0.49"):
        base = Decimal("0.60")
    elif ratio <= Decimal("0.74"):
        base = Decimal("0.35")
    elif ratio <= Decimal("0.90"):
        base = Decimal("0.18")
    else:
        base = Decimal("0.05")

    worst = Decimal(inputs.max_single_card_utilization)
    if worst > Decimal("0.90"):
        base = min(base, Decimal("0.25"))
    elif worst > Decimal("0.70"):
        base = min(base, Decimal("0.50"))

    percentage = (ratio * 100).quantize(Decimal("1"))
    if ratio <= Decimal("0.09"):
        reason = f"Utilisation of {percentage}% is in the ideal band (under 10%)."
    elif ratio <= Decimal("0.29"):
        reason = f"Utilisation of {percentage}%. Getting under 10% is worth real points."
    else:
        reason = (
            f"Utilisation of {percentage}% is high. This is the fastest factor to "
            "move -- it updates as soon as your statement posts."
        )
    return _clamp(base), reason


def _credit_age(inputs: CreditInputs) -> tuple[Decimal, str]:
    """Log-scaled age, saturating at ten years.

    Logarithmic because the first two years of history are worth far more than
    the eleventh, and because it makes the factor's growth feel fair to a thin
    file rather than hopeless.
    """

    months = max(inputs.oldest_account_months, 0)
    if months == 0:
        return Decimal(
            "0"
        ), "No credit history yet. Age builds on its own -- keep your oldest account open."

    score = Decimal(str(min(1.0, math.log1p(months) / math.log1p(AGE_SATURATION_MONTHS))))
    years = months // 12
    if months >= AGE_SATURATION_MONTHS:
        reason = f"{years} years of history -- this factor is maxed out."
    else:
        reason = (
            f"{years} year(s) of history. This grows automatically; closing your "
            "oldest card would set it back."
        )
    return _clamp(score), reason


def _credit_mix(inputs: CreditInputs) -> tuple[Decimal, str]:
    table = {
        0: Decimal("0.0"),
        1: Decimal("0.35"),
        2: Decimal("0.65"),
        3: Decimal("0.85"),
        4: Decimal("1.0"),
    }
    score = table.get(min(inputs.credit_mix_types, 4), Decimal("1.0"))
    if inputs.credit_mix_types <= 1:
        reason = (
            "Only one type of credit on file. Mix improves naturally over time; "
            "it is not worth opening an account purely to chase it."
        )
    else:
        reason = f"{inputs.credit_mix_types} types of credit -- a healthy mix."
    return score, reason


def _new_credit(inputs: CreditInputs) -> tuple[Decimal, str]:
    inquiry_table = {
        0: Decimal("1.0"),
        1: Decimal("0.90"),
        2: Decimal("0.75"),
        3: Decimal("0.55"),
        4: Decimal("0.35"),
    }
    score = inquiry_table.get(inputs.hard_inquiries_12m, Decimal("0.20"))
    if inputs.accounts_opened_24m >= 3:
        score = _clamp(score - Decimal("0.15"))

    if inputs.oldest_account_months <= 0:
        # "No inquiries" on an empty file is the absence of evidence, not a
        # clean record, and full marks here would partly refill the hole that
        # the payment-history factor correctly leaves.
        return Decimal("0.5"), (
            "No recent applications on file. This factor becomes meaningful "
            "once you have open accounts."
        )

    if inputs.hard_inquiries_12m == 0:
        reason = "No hard inquiries in the last year."
    else:
        reason = (
            f"{inputs.hard_inquiries_12m} hard inquiry(s) in the last 12 months. "
            "They stop counting after a year."
        )
    return score, reason


_FACTOR_FUNCTIONS = {
    "payment_history": ("Payment history", _payment_history),
    "utilization": ("Credit utilisation", _utilization),
    "credit_age": ("Length of history", _credit_age),
    "credit_mix": ("Credit mix", _credit_mix),
    "new_credit": ("New credit", _new_credit),
}


def _status_for(sub_score: Decimal) -> str:
    if sub_score >= Decimal("0.85"):
        return "excellent"
    if sub_score >= Decimal("0.65"):
        return "good"
    if sub_score >= Decimal("0.40"):
        return "fair"
    return "needs_work"


def score_profile(inputs: CreditInputs) -> ScoreBreakdown:
    """Compute a score and its full factor breakdown.

    The score is ``300 + 550 * Σ(weight_i * sub_score_i)``: a perfect profile
    lands at 850, an empty one at 300, and every point in between is
    attributable to a named factor.
    """

    factors: list[FactorScore] = []
    weighted_total = Decimal("0")

    for key, (label, function) in _FACTOR_FUNCTIONS.items():
        sub_score, reason = function(inputs)
        weight = WEIGHTS[key]
        weighted_total += weight * sub_score

        factor_points = weight * Decimal(SCORE_RANGE)
        earned = int((factor_points * sub_score).to_integral_value())
        factors.append(
            FactorScore(
                key=key,
                label=label,
                weight=weight,
                sub_score=sub_score.quantize(Decimal("0.001")),
                points_earned=earned,
                points_available=int(factor_points.to_integral_value()) - earned,
                status=_status_for(sub_score),
                reason=reason,
            )
        )

    score = int(
        (Decimal(SCORE_MIN) + Decimal(SCORE_RANGE) * weighted_total).to_integral_value()
    )
    score = max(SCORE_MIN, min(SCORE_MAX, score))
    return ScoreBreakdown(score=score, band=band_for(score), factors=tuple(factors))
