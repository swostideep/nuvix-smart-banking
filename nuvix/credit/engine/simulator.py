"""What-if credit simulation.

The factor model is cheap and pure, so the honest way to answer "what happens
to my score if I do X" is to apply X to the inputs and score them again --
no separate estimate that can drift out of agreement with the real model.

Every action is expressed as a transformation on :class:`CreditInputs`, which
keeps the simulator's surface tiny and makes each action independently
testable.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal

from nuvix.core.money import ZERO
from nuvix.credit.engine.factors import CreditInputs, ScoreBreakdown, score_profile


class UnknownAction(ValueError):
    """Raised for an action the simulator does not implement."""


@dataclass(frozen=True, slots=True)
class FactorDelta:
    key: str
    label: str
    points_before: int
    points_after: int

    @property
    def change(self) -> int:
        return self.points_after - self.points_before


@dataclass(frozen=True, slots=True)
class SimulationResult:
    action: str
    parameters: dict[str, object]
    score_before: int
    score_after: int
    band_before: str
    band_after: str
    factor_deltas: tuple[FactorDelta, ...]
    explanation: str

    @property
    def score_change(self) -> int:
        return self.score_after - self.score_before


def _pay_down_balance(inputs: CreditInputs, amount: Decimal, **_) -> tuple[CreditInputs, str]:
    """Reduce revolving balance by ``amount``.

    The per-card worst utilisation is reduced proportionally rather than
    assumed to land on the worst card: without knowing which card the customer
    will pay, the proportional assumption is the one that does not overpromise.
    """

    amount = max(Decimal(amount), ZERO)
    new_balance = max(Decimal(inputs.total_balance) - amount, ZERO)
    if inputs.total_balance > 0:
        shrink = new_balance / Decimal(inputs.total_balance)
    else:
        shrink = Decimal("1")
    return (
        dataclasses.replace(
            inputs,
            total_balance=new_balance,
            max_single_card_utilization=(Decimal(inputs.max_single_card_utilization) * shrink),
        ),
        f"Paying down ${amount} of revolving balance",
    )


def _open_new_card(
    inputs: CreditInputs, credit_limit: Decimal, **_
) -> tuple[CreditInputs, str]:
    """Open a card: more limit (helps utilisation), plus an inquiry and a
    younger average age (both hurt). The net is frequently negative in the
    short run, which is precisely why this is worth simulating before acting.
    """

    limit = max(Decimal(credit_limit), ZERO)
    return (
        dataclasses.replace(
            inputs,
            total_credit_limit=Decimal(inputs.total_credit_limit) + limit,
            hard_inquiries_12m=inputs.hard_inquiries_12m + 1,
            accounts_opened_24m=inputs.accounts_opened_24m + 1,
            open_accounts=inputs.open_accounts + 1,
            average_account_age_months=int(
                inputs.average_account_age_months
                * inputs.open_accounts
                / max(inputs.open_accounts + 1, 1)
            ),
        ),
        f"Opening a new card with a ${limit} limit",
    )


def _close_card(
    inputs: CreditInputs, credit_limit: Decimal, is_oldest: bool = False, **_
) -> tuple[CreditInputs, str]:
    """Close a card: loses limit, and loses history if it was the oldest.

    Closing the oldest account is the most common self-inflicted score wound
    NuviX sees, so the simulator models it explicitly.
    """

    limit = max(Decimal(credit_limit), ZERO)
    updated = dataclasses.replace(
        inputs,
        total_credit_limit=max(Decimal(inputs.total_credit_limit) - limit, ZERO),
        open_accounts=max(inputs.open_accounts - 1, 0),
    )
    if is_oldest:
        updated = dataclasses.replace(
            updated, oldest_account_months=inputs.average_account_age_months
        )
    return updated, f"Closing a card with a ${limit} limit"


def _perfect_months(inputs: CreditInputs, months: int, **_) -> tuple[CreditInputs, str]:
    """Pay everything on time for ``months``.

    Ageing is the only lever that works while the customer does nothing else,
    and it compounds: history lengthens, inquiries roll off, and the on-time
    rate converges upward.
    """

    months = max(int(months), 0)
    total_payments_seen = max(inputs.open_accounts * 12, 12)
    improved_rate = min(
        Decimal("1.0"),
        (
            Decimal(inputs.on_time_payment_rate) * Decimal(total_payments_seen)
            + Decimal(inputs.open_accounts * months)
        )
        / Decimal(total_payments_seen + inputs.open_accounts * months),
    )
    return (
        dataclasses.replace(
            inputs,
            oldest_account_months=inputs.oldest_account_months + months,
            average_account_age_months=inputs.average_account_age_months + months,
            on_time_payment_rate=improved_rate,
            hard_inquiries_12m=max(inputs.hard_inquiries_12m - months // 12, 0),
            accounts_opened_24m=max(inputs.accounts_opened_24m - months // 24, 0),
            late_payments_30d=(
                inputs.late_payments_30d
                if months < 12
                else max(inputs.late_payments_30d - 1, 0)
            ),
        ),
        f"{months} months of on-time payments",
    )


def _increase_limit(inputs: CreditInputs, amount: Decimal, **_) -> tuple[CreditInputs, str]:
    """A credit-limit increase: utilisation falls with no new inquiry.

    Often the single highest-leverage action available, and one most customers
    do not know they can simply ask for.
    """

    increase = max(Decimal(amount), ZERO)
    return (
        dataclasses.replace(
            inputs, total_credit_limit=Decimal(inputs.total_credit_limit) + increase
        ),
        f"A ${increase} credit-limit increase",
    )


ACTIONS = {
    "pay_down_balance": _pay_down_balance,
    "open_new_card": _open_new_card,
    "close_card": _close_card,
    "on_time_months": _perfect_months,
    "increase_limit": _increase_limit,
}


def simulate(inputs: CreditInputs, action: str, **parameters) -> SimulationResult:
    """Apply ``action`` to ``inputs`` and report the score movement."""

    try:
        transform = ACTIONS[action]
    except KeyError:
        raise UnknownAction(
            f"Unknown action {action!r}. Available: {sorted(ACTIONS)}"
        ) from None

    before = score_profile(inputs)
    updated, description = transform(inputs, **parameters)
    after = score_profile(updated)

    deltas = tuple(
        FactorDelta(
            key=b.key,
            label=b.label,
            points_before=b.points_earned,
            points_after=a.points_earned,
        )
        for b, a in zip(before.factors, after.factors, strict=True)
    )

    change = after.score - before.score
    if change > 0:
        verdict = f"{description} would add about {change} points."
    elif change < 0:
        verdict = f"{description} would cost about {abs(change)} points."
    else:
        verdict = f"{description} would not move your score much."

    return SimulationResult(
        action=action,
        parameters=parameters,
        score_before=before.score,
        score_after=after.score,
        band_before=before.band,
        band_after=after.band,
        factor_deltas=deltas,
        explanation=verdict,
    )


def rank_actions(inputs: CreditInputs, candidates: list[dict]) -> list[SimulationResult]:
    """Score several candidate actions and return them best-first.

    Powers the "what should I do next" panel: rather than listing generic
    advice, NuviX simulates each realistic option against this specific
    profile and ranks by the points it would actually produce.
    """

    results = []
    for candidate in candidates:
        payload = dict(candidate)
        action = payload.pop("action")
        try:
            results.append(simulate(inputs, action, **payload))
        except (UnknownAction, TypeError):
            continue
    return sorted(results, key=lambda r: -r.score_change)


def current_breakdown(inputs: CreditInputs) -> ScoreBreakdown:
    return score_profile(inputs)
