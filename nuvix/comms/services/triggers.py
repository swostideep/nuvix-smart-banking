"""Trigger evaluators.

Each evaluator inspects one user's state and either returns a context dict --
the facts the template will be rendered with -- or ``None`` when there is
nothing worth saying. The contract is deliberately narrow so that adding a new
nudge is one function plus one row, and so that every trigger can be tested by
constructing a user and calling it.

The bar for returning a context is high. A message that is technically true
but not actionable is the most expensive thing a money app can send: it does
not help, and it spends the attention budget that the genuinely urgent message
will need next week.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal

from django.utils import timezone

from nuvix.accounts.models import User
from nuvix.banking.models import RecurringSeries
from nuvix.comms.models import TriggerType
from nuvix.core.money import ZERO, money
from nuvix.credit.models import CreditProfile, ScoreSnapshot
from nuvix.debt.models import Debt, PayoffPlan

logger = logging.getLogger(__name__)

TriggerFn = Callable[[User, dict], dict | None]

_REGISTRY: dict[str, TriggerFn] = {}


def register(trigger: str) -> Callable[[TriggerFn], TriggerFn]:
    def decorator(function: TriggerFn) -> TriggerFn:
        _REGISTRY[trigger] = function
        return function

    return decorator


def evaluate(trigger: str, user: User, parameters: dict) -> dict | None:
    """Run one trigger, converting any failure into "nothing to say".

    A trigger reaches into the credit, debt, banking and marketplace engines.
    One of them raising for one user must not abort the sweep for everyone
    else, so the failure is logged with enough context to fix and the sweep
    moves on.
    """

    evaluator = _REGISTRY.get(trigger)
    if evaluator is None:
        logger.warning("unknown_trigger", extra={"trigger": trigger})
        return None
    try:
        return evaluator(user, parameters or {})
    except Exception:
        logger.warning(
            "trigger_failed",
            extra={"trigger": trigger, "user_id": str(user.id)},
            exc_info=True,
        )
        return None


def available_triggers() -> list[str]:
    return sorted(_REGISTRY)


@register(TriggerType.UTILIZATION_HIGH)
def utilization_high(user: User, parameters: dict) -> dict | None:
    """Fires when utilisation crosses a scoring band boundary.

    Thresholded at the *band edges* the scoring model uses (30%, 50%), not at
    an arbitrary number, so the message can quantify the points at stake
    rather than vaguely advising the user to "lower their balance".
    """

    threshold = Decimal(str(parameters.get("threshold", "0.30")))
    profile = CreditProfile.objects.filter(user=user).first()
    if profile is None or profile.total_credit_limit <= 0:
        return None

    utilisation = profile.utilization
    if utilisation < threshold:
        return None

    from nuvix.credit.engine.simulator import simulate

    target_balance = profile.total_credit_limit * Decimal("0.29")
    paydown = money(max(profile.total_balance - target_balance, ZERO))
    if paydown <= 0:
        return None

    result = simulate(profile.to_inputs(), "pay_down_balance", amount=paydown)
    if result.score_change < 5:
        # Not enough movement to be worth an interruption.
        return None

    return {
        "utilization_pct": int(utilisation * 100),
        "paydown_amount": str(paydown),
        "points_gain": result.score_change,
        "score": profile.score,
        "first_name": user.first_name or "there",
    }


@register(TriggerType.PAYMENT_DUE_SOON)
def payment_due_soon(user: User, parameters: dict) -> dict | None:
    """Fires a few days before a due date on the highest-APR debt.

    Only the most expensive debt, and only one message: reminding someone
    about six bills individually is how an app gets muted.
    """

    days_ahead = int(parameters.get("days_ahead", 4))
    today = timezone.localdate()
    target_day = (today + dt.timedelta(days=days_ahead)).day

    debt = (
        Debt.objects.alive()
        .filter(user=user, balance__gt=0, due_day=target_day)
        .order_by("-apr")
        .first()
    )
    if debt is None:
        return None

    return {
        "debt_name": debt.name,
        "minimum_payment": str(debt.minimum_payment),
        "due_in_days": days_ahead,
        "apr": str(debt.apr),
        "first_name": user.first_name or "there",
    }


@register(TriggerType.PROJECTED_SHORTFALL)
def projected_shortfall(user: User, parameters: dict) -> dict | None:
    """Fires when the cashflow projection dips below zero before payday.

    The highest-value message the platform sends: an overdraft fee is real
    money, and it is avoidable with two days' notice.
    """

    horizon = int(parameters.get("horizon_days", 21))
    if not RecurringSeries.objects.filter(user=user).exists():
        return None

    from nuvix.banking.services.sync import build_projection

    projection = build_projection(user, horizon_days=horizon)
    if not projection.shortfall_risk or projection.lowest_balance_on is None:
        return None

    days_away = (projection.lowest_balance_on - timezone.localdate()).days
    return {
        "shortfall_amount": str(abs(projection.lowest_balance)),
        "shortfall_date": projection.lowest_balance_on.isoformat(),
        "days_away": days_away,
        "safe_to_spend": str(projection.safe_to_spend),
        "first_name": user.first_name or "there",
    }


@register(TriggerType.SCORE_CHANGED)
def score_changed(user: User, parameters: dict) -> dict | None:
    """Fires on a material score move in either direction.

    A drop is sent as well as a rise. Reporting only good news makes the
    number feel like marketing rather than a measurement.
    """

    minimum_delta = int(parameters.get("min_delta", 8))
    snapshot = ScoreSnapshot.objects.filter(user=user).order_by("-captured_on").first()
    if snapshot is None or abs(snapshot.delta) < minimum_delta:
        return None

    return {
        "score": snapshot.score,
        "delta": abs(snapshot.delta),
        "direction": "up" if snapshot.delta > 0 else "down",
        "band": snapshot.band.replace("_", " "),
        "captured_on": snapshot.captured_on.isoformat(),
        "first_name": user.first_name or "there",
    }


@register(TriggerType.PAYOFF_MILESTONE)
def payoff_milestone(user: User, parameters: dict) -> dict | None:
    """Fires when a quarter of the original balance has been cleared.

    Progress is the mechanism that keeps a payoff plan alive, so milestones
    are treated as a feature rather than a nicety.
    """

    plan = PayoffPlan.objects.filter(user=user, is_active=True).first()
    if plan is None or plan.original_balance <= 0:
        return None

    current = sum(
        (d.balance for d in Debt.objects.alive().filter(user=user, balance__gt=0)), ZERO
    )
    cleared = plan.original_balance - current
    fraction = cleared / plan.original_balance
    milestone = int(fraction * 4) * 25  # 0, 25, 50, 75, 100
    if milestone < 25:
        return None

    return {
        "milestone_pct": milestone,
        "cleared_amount": str(money(cleared)),
        "remaining_amount": str(money(current)),
        "debt_free_date": plan.debt_free_date.isoformat(),
        "first_name": user.first_name or "there",
    }


@register(TriggerType.BETTER_OFFER)
def better_offer(user: User, parameters: dict) -> dict | None:
    """Fires only when a matched offer beats what the user pays today by a
    margin large enough to be worth a hard inquiry."""

    minimum_benefit = Decimal(str(parameters.get("min_benefit", "250")))
    minimum_odds = Decimal(str(parameters.get("min_odds", "0.5")))

    from nuvix.marketplace.services.matching import match

    matches = match(user, limit=1)
    if not matches:
        return None

    best = matches[0]
    if best["estimated_benefit"] < minimum_benefit or best["approval_odds"] < minimum_odds:
        return None

    return {
        "product_name": best["product"].name,
        "partner_name": best["product"].partner.name,
        "estimated_apr": str(best["estimated_apr"]),
        "estimated_benefit": str(best["estimated_benefit"]),
        "approval_odds_pct": int(best["approval_odds"] * 100),
        "first_name": user.first_name or "there",
    }


@register(TriggerType.NO_PLAN)
def no_plan(user: User, parameters: dict) -> dict | None:
    """Fires for a user carrying debt with no active payoff plan.

    Quantified with the real optimiser output, because "you could save $3,140"
    is a reason to act and "make a plan" is not.
    """

    if PayoffPlan.objects.filter(user=user, is_active=True).exists():
        return None

    from nuvix.core.exceptions import DomainError
    from nuvix.debt.services import planner
    from nuvix.debt.services.strategies import interest_saved_against_minimums

    try:
        debts = planner.require_debts(user)
        budget = planner.suggested_budget(user)
        savings = interest_saved_against_minimums(debts, budget, "optimal")
    except DomainError:
        # No debts, or a budget that cannot amortise them. Either way there is
        # no plan to pitch, and that is an ordinary outcome, not an error.
        return None

    if savings["interest_saved"] is None or savings["interest_saved"] <= Decimal("100"):
        return None

    return {
        "interest_saved": str(savings["interest_saved"]),
        "months_saved": savings["months_saved"],
        "monthly_budget": str(budget),
        "first_name": user.first_name or "there",
    }
