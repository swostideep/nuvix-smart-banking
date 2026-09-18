"""Scoring orchestration: derive inputs, score, persist history."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from nuvix.accounts.models import User
from nuvix.banking.models import AccountType, LinkedAccount
from nuvix.core.money import ZERO
from nuvix.credit.engine.factors import ScoreBreakdown, score_profile
from nuvix.credit.models import CreditProfile, ScoreSnapshot


def factor_to_json(factor) -> dict:
    """Make a :class:`FactorScore` safe for a ``JSONField``.

    ``Decimal`` is not JSON-serialisable, and the weights and sub-scores are
    Decimals by design -- the factor model refuses to do money or ratios in
    binary floating point. They are stored as strings rather than coerced to
    float so that a stored breakdown round-trips to the exact value that was
    shown to the customer.
    """

    payload = asdict(factor)
    return {
        key: (str(value) if isinstance(value, Decimal) else value)
        for key, value in payload.items()
    }


def get_or_create_profile(user: User) -> CreditProfile:
    profile, _ = CreditProfile.objects.get_or_create(user=user)
    return profile


def derive_from_linked_accounts(user: User) -> CreditProfile:
    """Refresh the utilisation and mix facts from linked accounts.

    Payment history and inquiries cannot be derived this way -- they need a
    bureau -- so those fields are deliberately left untouched rather than
    being guessed at from transaction data.
    """

    profile = get_or_create_profile(user)
    accounts = list(LinkedAccount.objects.alive().filter(user=user))

    cards = [a for a in accounts if a.account_type == AccountType.CREDIT_CARD]
    total_limit = sum((a.credit_limit or ZERO) for a in cards)
    total_balance = sum(abs(min(a.current_balance, ZERO)) for a in cards)

    worst = ZERO
    for card in cards:
        utilisation = card.utilization
        if utilisation is not None:
            worst = max(worst, utilisation)

    profile.total_credit_limit = total_limit
    profile.total_balance = total_balance
    profile.max_single_card_utilization = worst
    profile.credit_mix_types = len({a.account_type for a in accounts}) or 1
    profile.open_accounts = max(len(accounts), 1)

    if accounts:
        ages = [max((timezone.now() - a.created_at).days // 30, 0) for a in accounts]
        profile.oldest_account_months = max(max(ages), profile.oldest_account_months)
        profile.average_account_age_months = max(
            sum(ages) // len(ages), profile.average_account_age_months
        )

    profile.save()
    return profile


@transaction.atomic
def rescore(user: User, captured_on: dt.date | None = None) -> ScoreBreakdown:
    """Recompute the score and record a snapshot when it moves.

    Snapshots are written on change only. A flat daily series would make the
    history chart useless and the table enormous, and neither helps anyone.
    """

    profile = get_or_create_profile(user)
    breakdown = score_profile(profile.to_inputs())

    # A profile that has never been scored carries the model floor (300) as a
    # placeholder, not a previous score. Treating it as one reports the very
    # first scoring as a ~350-point jump, and the communications engine
    # cheerfully tells a brand-new user their score "moved up 347 points".
    first_scoring = profile.last_scored_at is None
    previous_score = breakdown.score if first_scoring else profile.score
    profile.score = breakdown.score
    profile.band = breakdown.band
    profile.factors = [factor_to_json(factor) for factor in breakdown.factors]
    profile.last_scored_at = timezone.now()
    profile.save(update_fields=["score", "band", "factors", "last_scored_at", "updated_at"])

    day = captured_on or timezone.localdate()
    latest = ScoreSnapshot.objects.filter(user=user).order_by("-captured_on").first()
    if latest is None or latest.score != breakdown.score:
        ScoreSnapshot.objects.update_or_create(
            user=user,
            captured_on=day,
            defaults={
                "score": breakdown.score,
                "band": breakdown.band,
                "delta": breakdown.score - previous_score,
                "utilization": profile.utilization,
                "factors": profile.factors,
            },
        )
    return breakdown


def score_history(user: User, days: int = 365) -> list[ScoreSnapshot]:
    since = timezone.localdate() - dt.timedelta(days=days)
    return list(
        ScoreSnapshot.objects.filter(user=user, captured_on__gte=since).order_by("captured_on")
    )


def default_action_candidates(profile: CreditProfile) -> list[dict]:
    """Realistic next actions for this specific profile.

    Generated from the profile rather than a fixed list: suggesting a
    limit increase to someone with no revolving credit is noise, and noise is
    how a money app trains its users to ignore it.
    """

    candidates: list[dict] = [{"action": "on_time_months", "months": 6}]

    if profile.total_balance > 0:
        for fraction in (Decimal("0.25"), Decimal("0.5"), Decimal("1.0")):
            candidates.append(
                {
                    "action": "pay_down_balance",
                    "amount": (profile.total_balance * fraction).quantize(Decimal("0.01")),
                }
            )
    if profile.total_credit_limit > 0:
        candidates.append(
            {
                "action": "increase_limit",
                "amount": (profile.total_credit_limit * Decimal("0.3")).quantize(
                    Decimal("0.01")
                ),
            }
        )
    else:
        candidates.append({"action": "open_new_card", "credit_limit": Decimal("500.00")})
    return candidates
