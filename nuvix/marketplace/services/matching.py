"""Matching and ranking partner products.

Three stages, deliberately separated:

1. **Eligibility** -- a hard filter in SQL. A product whose box the user does
   not fit is not an offer; showing it produces a decline, and a decline costs
   the user a hard inquiry.
2. **Pricing** -- where inside the product's APR band this user lands, driven
   by their credit score.
3. **Ranking** -- expected value to the *user*: modelled dollar benefit
   multiplied by the odds of actually being approved.

On the conflict of interest
---------------------------
NuviX earns a payout when a user funds through a partner. That payout is
recorded on :class:`~nuvix.marketplace.models.Partner` and is deliberately
**absent from the ranking function**. Ranking by revenue is the failure mode
that turns a recommendation engine into an ad network, and the whole product
premise -- that the ranking is on the customer's side -- does not survive it.
Ordering is by user benefit; commercial terms affect which partners are in the
catalogue, never the order they appear in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import QuerySet

from nuvix.accounts.models import User
from nuvix.core.money import ZERO, money
from nuvix.credit.models import CreditProfile
from nuvix.marketplace.models import Offer, Product, ProductType

#: Assumed monthly spend on a rewards card, used to value rewards. A modest
#: figure on purpose: valuing rewards off aspirational spend is how card
#: comparison sites end up recommending fee-bearing cards to people who will
#: never earn the fee back.
ASSUMED_MONTHLY_SPEND = Decimal("900")

#: Horizon over which card benefits are valued.
CARD_BENEFIT_MONTHS = 24

#: Logistic steepness for approval odds. Tuned so that sitting exactly on a
#: product's minimum score gives roughly even odds, and 60 points above it
#: gives high confidence.
_SCORE_STEEPNESS = 0.035
_DTI_STEEPNESS = 4.0


@dataclass(frozen=True, slots=True)
class MatchContext:
    """Everything the matcher knows about the user."""

    credit_score: int
    annual_income: Decimal
    dti_ratio: Decimal
    months_employed: int
    state: str
    #: Total revolving balance -- the size of the consolidation opportunity.
    revolving_balance: Decimal
    #: Weighted average APR the user pays today, used as the comparison point.
    current_weighted_apr: Decimal


def build_context(user: User) -> MatchContext:
    from nuvix.credit.services.risk import monthly_debt_service
    from nuvix.debt.models import Debt

    profile = getattr(user, "profile", None)
    credit, _ = CreditProfile.objects.get_or_create(user=user)

    debts = list(Debt.objects.alive().filter(user=user, balance__gt=0))
    revolving = sum((d.balance for d in debts), ZERO)
    if revolving > 0:
        weighted_apr = sum((d.balance * d.apr for d in debts), ZERO) / revolving
    else:
        weighted_apr = ZERO

    monthly_income = Decimal(getattr(profile, "monthly_gross_income", ZERO) or ZERO)
    obligations = monthly_debt_service(user)
    dti = (obligations / monthly_income) if monthly_income > 0 else Decimal("1")

    return MatchContext(
        credit_score=credit.score,
        annual_income=Decimal(getattr(profile, "annual_income", ZERO) or ZERO),
        dti_ratio=min(dti, Decimal("2")),
        months_employed=int(getattr(profile, "months_employed", 0) or 0),
        state=(getattr(profile, "state", "") or "").upper(),
        revolving_balance=money(revolving),
        current_weighted_apr=Decimal(weighted_apr).quantize(Decimal("0.01")),
    )


def eligible_products(
    context: MatchContext, product_type: str | None = None
) -> QuerySet[Product]:
    """Hard filter, evaluated in the database.

    Everything here is a criterion a partner would decline on outright, so it
    belongs in the ``WHERE`` clause rather than in a Python loop over the
    whole catalogue.
    """

    queryset = Product.objects.filter(
        is_active=True,
        partner__is_active=True,
        min_credit_score__lte=context.credit_score,
        max_dti_ratio__gte=context.dti_ratio,
        min_annual_income__lte=context.annual_income,
        min_months_employed__lte=context.months_employed,
    ).select_related("partner")

    if product_type:
        queryset = queryset.filter(product_type=product_type)
    if context.state:
        # Delimiters on both sides so that "CA" cannot match inside another
        # code. See Product.excluded_states for why this is a string.
        queryset = queryset.exclude(excluded_states__contains=f",{context.state},")
    return queryset


def estimate_apr(product: Product, context: MatchContext) -> Decimal:
    """Place the user inside the product's advertised APR band.

    Linear interpolation between the band's ends over a 300-point score range,
    anchored at the product's own minimum score. A borrower at the floor gets
    the ceiling rate, which is what actually happens.
    """

    span = Decimal(product.apr_max) - Decimal(product.apr_min)
    if span <= 0:
        return Decimal(product.apr_min)

    headroom = Decimal(max(context.credit_score - product.min_credit_score, 0))
    # 300 points above a product's floor is effectively best-in-band.
    position = min(headroom / Decimal(300), Decimal(1))
    return (Decimal(product.apr_max) - span * position).quantize(Decimal("0.01"))


def approval_odds(product: Product, context: MatchContext) -> Decimal:
    """Probability this application is approved.

    A logistic on how far inside the box the user sits on the two criteria
    that drive most declines -- score and DTI -- scaled by the partner's own
    historical approval rate. Sitting exactly on the minimum is a coin flip,
    not a yes, which is the point: a marketplace that implies certainty burns
    the user's credit file.
    """

    score_margin = context.credit_score - product.min_credit_score
    dti_margin = float(Decimal(product.max_dti_ratio) - context.dti_ratio)

    score_term = 1 / (1 + math.exp(-_SCORE_STEEPNESS * score_margin))
    dti_term = 1 / (1 + math.exp(-_DTI_STEEPNESS * dti_margin))

    # Geometric mean: a product is not "half approvable" because the user is
    # excellent on score and hopeless on DTI. The weaker criterion dominates.
    combined = math.sqrt(score_term * dti_term)
    odds = combined * float(product.historical_approval_rate)
    return Decimal(str(round(min(max(odds, 0.01), 0.99), 3)))


def _loan_benefit(
    product: Product, context: MatchContext, apr: Decimal, amount: Decimal, term: int
) -> tuple[Decimal, list[str]]:
    """Interest saved by refinancing existing balances into this loan.

    Compared against what the user pays today, not against zero -- a 19% loan
    is a terrible product in the abstract and an excellent one for someone
    servicing 27% cards.
    """

    reasons: list[str] = []
    if context.revolving_balance <= 0 or context.current_weighted_apr <= 0:
        return ZERO, ["No existing balances to consolidate."]

    refinanced = min(amount, context.revolving_balance)
    years = Decimal(term) / Decimal(12)

    # Average outstanding principal over a level-payment schedule is close to
    # half the original, which is accurate enough for a comparison figure and
    # far more legible than a full amortisation here.
    average_principal = refinanced / Decimal(2)
    current_cost = average_principal * (context.current_weighted_apr / Decimal(100)) * years
    new_cost = average_principal * (apr / Decimal(100)) * years
    origination = refinanced * Decimal(product.origination_fee_rate)

    benefit = current_cost - new_cost - origination

    if apr < context.current_weighted_apr:
        reasons.append(
            f"Rate of {apr}% is below the {context.current_weighted_apr}% you pay today."
        )
    else:
        reasons.append(
            f"Rate of {apr}% is not better than your current {context.current_weighted_apr}%."
        )
    if origination > 0:
        reasons.append(f"Origination fee of about ${money(origination)} is priced in.")
    return money(benefit), reasons


def _card_benefit(
    product: Product, context: MatchContext, apr: Decimal
) -> tuple[Decimal, list[str]]:
    """Value of a card over :data:`CARD_BENEFIT_MONTHS`: rewards plus intro
    APR savings, less fees."""

    reasons: list[str] = []
    rewards = (
        ASSUMED_MONTHLY_SPEND * Decimal(product.rewards_rate) * Decimal(CARD_BENEFIT_MONTHS)
    )
    fees = Decimal(product.annual_fee) * Decimal(CARD_BENEFIT_MONTHS) / Decimal(12)

    intro_saving = ZERO
    if product.intro_apr is not None and product.intro_apr_months > 0:
        transferable = min(context.revolving_balance, Decimal(product.amount_max or 0))
        months = Decimal(min(product.intro_apr_months, CARD_BENEFIT_MONTHS))
        rate_gap = max(context.current_weighted_apr - Decimal(product.intro_apr), ZERO)
        intro_saving = transferable * (rate_gap / Decimal(100)) * (months / Decimal(12))
        if intro_saving > 0:
            reasons.append(
                f"{product.intro_apr_months} months at {product.intro_apr}% could save "
                f"about ${money(intro_saving)} on transferred balances."
            )

    if product.rewards_rate > 0:
        reasons.append(
            f"Rewards worth roughly ${money(rewards)} over two years at typical spend."
        )
    if product.annual_fee > 0:
        reasons.append(f"Annual fee of ${product.annual_fee}.")
    if product.product_type == ProductType.SECURED_CARD:
        reasons.append("Reports to all three bureaus -- built for establishing history.")

    return money(rewards + intro_saving - fees), reasons


def score_product(product: Product, context: MatchContext) -> dict:
    """Price, value and rank one product for this user."""

    apr = estimate_apr(product, context)
    odds = approval_odds(product, context)

    amount = min(
        max(context.revolving_balance, Decimal(product.amount_min)),
        Decimal(product.amount_max) or Decimal(product.amount_min),
    )
    term = product.term_months_max

    if product.is_revolving:
        benefit, reasons = _card_benefit(product, context, apr)
        amount = Decimal(product.amount_max or 0)
        term = CARD_BENEFIT_MONTHS
    else:
        benefit, reasons = _loan_benefit(product, context, apr, amount, term)

    # Expected value to the user. Multiplying by approval odds is what stops
    # an unreachable headline rate from outranking an attainable good one.
    rank_score = (benefit * odds).quantize(Decimal("0.0001"))

    reasons.insert(0, f"Estimated approval odds {int(odds * 100)}%.")

    return {
        "product": product,
        "estimated_apr": apr,
        "estimated_amount": money(amount),
        "estimated_term_months": term,
        "approval_odds": odds,
        "estimated_benefit": benefit,
        "rank_score": rank_score,
        "reasons": reasons,
    }


def match(user: User, product_type: str | None = None, limit: int = 10) -> list[dict]:
    """Return this user's best offers, best first."""

    context = build_context(user)
    scored = [
        score_product(product, context) for product in eligible_products(context, product_type)
    ]
    scored.sort(key=lambda item: -item["rank_score"])
    return scored[:limit]


def persist_offers(user: User, scored: list[dict]) -> list[Offer]:
    """Record what was shown, so the quote can be reproduced later."""

    Offer.objects.filter(user=user, clicked_at__isnull=True).delete()
    return Offer.objects.bulk_create(
        [
            Offer(
                user=user,
                product=item["product"],
                estimated_apr=item["estimated_apr"],
                estimated_amount=item["estimated_amount"],
                estimated_term_months=item["estimated_term_months"],
                approval_odds=item["approval_odds"],
                estimated_benefit=item["estimated_benefit"],
                rank_score=item["rank_score"],
                reasons=item["reasons"],
            )
            for item in scored
        ]
    )
