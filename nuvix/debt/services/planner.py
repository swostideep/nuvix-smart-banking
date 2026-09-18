"""Orchestration between the ORM and the pure payoff engine."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from nuvix.accounts.models import User
from nuvix.core.exceptions import DomainError
from nuvix.core.money import money
from nuvix.debt.models import Debt, PayoffPlan
from nuvix.debt.services.amortization import minimum_budget
from nuvix.debt.services.strategies import (
    build_plan,
    compare_strategies,
    interest_saved_against_minimums,
)
from nuvix.debt.services.types import DebtInput, PayoffResult


def active_debt_inputs(user: User) -> list[DebtInput]:
    return [
        debt.to_input()
        for debt in Debt.objects.alive().filter(user=user, balance__gt=0).order_by("created_at")
    ]


def require_debts(user: User) -> list[DebtInput]:
    debts = active_debt_inputs(user)
    if not debts:
        raise DomainError("Add at least one debt before building a plan.", code="no_debts")
    return debts


def suggested_budget(user: User) -> Decimal:
    """A budget the user can plausibly sustain.

    The minimums are the floor. Above that, three quarters of the declared
    monthly surplus is directed at debt -- deliberately not all of it, because
    a plan that consumes every spare dollar breaks the first time a tyre goes
    flat, and a broken plan is abandoned.
    """

    debts = require_debts(user)
    floor = minimum_budget(debts)
    profile = getattr(user, "profile", None)
    surplus = getattr(profile, "monthly_surplus", Decimal(0)) or Decimal(0)
    return money(max(floor, floor + max(surplus, Decimal(0)) * Decimal("0.75")))


def build(
    user: User,
    strategy: str,
    monthly_budget: Decimal | None = None,
    start_date: dt.date | None = None,
    keep_schedule: bool = True,
) -> PayoffResult:
    debts = require_debts(user)
    budget = money(monthly_budget) if monthly_budget is not None else suggested_budget(user)
    return build_plan(
        debts, budget, strategy, start_date or timezone.localdate(), keep_schedule
    )


def compare(user: User, monthly_budget: Decimal | None = None) -> dict[str, PayoffResult]:
    debts = require_debts(user)
    budget = money(monthly_budget) if monthly_budget is not None else suggested_budget(user)
    return compare_strategies(debts, budget, timezone.localdate())


@transaction.atomic
def save_plan(
    user: User, strategy: str, monthly_budget: Decimal | None = None
) -> tuple[PayoffPlan, PayoffResult]:
    """Compute a plan and make it the user's active one.

    Previous plans are deactivated rather than deleted: the history of what a
    customer was promised is part of the audit trail.
    """

    debts = require_debts(user)
    budget = money(monthly_budget) if monthly_budget is not None else suggested_budget(user)
    today = timezone.localdate()

    result = build_plan(debts, budget, strategy, today, keep_schedule=True)
    savings = interest_saved_against_minimums(debts, budget, strategy, today)

    PayoffPlan.objects.filter(user=user, is_active=True).update(is_active=False)
    plan = PayoffPlan.objects.create(
        user=user,
        strategy=strategy,
        monthly_budget=budget,
        months_to_debt_free=result.months_to_debt_free,
        debt_free_date=result.debt_free_date,
        total_interest=result.total_interest,
        total_paid=result.total_paid,
        original_balance=result.original_balance,
        interest_saved_vs_minimums=savings["interest_saved"],
        months_saved_vs_minimums=savings["months_saved"],
        summary=result.summary(),
        inputs=[
            {
                "key": d.key,
                "name": d.name,
                "balance": str(d.balance),
                "apr": str(d.apr),
                "minimum_payment": str(d.minimum_payment),
                "promo_apr": str(d.promo_apr) if d.promo_apr is not None else None,
                "promo_months_remaining": d.promo_months_remaining,
            }
            for d in debts
        ],
        is_active=True,
    )
    return plan, result
