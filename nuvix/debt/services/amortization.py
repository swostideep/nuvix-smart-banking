"""The amortisation simulator.

Every strategy in NuviX is scored by this one function. Keeping a single
simulator -- rather than a closed-form estimate per strategy -- means
avalanche, snowball and the optimiser are always compared on identical
arithmetic, so a difference in the output is a real difference in the plan.

Monthly mechanics, in the order a card issuer applies them:

1. Interest accrues on the opening balance at that month's effective rate.
2. Contractual minimums are paid on every debt.
3. Whatever is left of the budget cascades down the priority order, and any
   payment that would overshoot a balance is trimmed and passed on -- the
   "avalanche" effect that makes each payoff accelerate the next.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from decimal import Decimal

from nuvix.core.exceptions import DomainError, InsufficientBudgetError
from nuvix.core.money import ZERO, money, monthly_rate
from nuvix.debt.services.types import (
    DebtInput,
    DebtMonth,
    DebtOutcome,
    MonthSnapshot,
    PayoffResult,
)

#: Hard stop on the simulation. A plan that takes fifty years is not a plan;
#: the guard also makes a pathological input fail fast instead of hanging a
#: worker.
MAX_MONTHS = 600

#: Ordering callable: given the live balances and the month index, return the
#: debt keys in the order surplus should be applied.
PriorityFn = Callable[[Sequence[DebtInput], dict[str, Decimal], int], list[str]]


def minimum_budget(debts: Sequence[DebtInput]) -> Decimal:
    """Sum of contractual minimums -- the floor on any viable budget."""

    return money(sum((d.required_payment(d.balance) for d in debts), ZERO))


def _add_months(start: dt.date, months: int) -> dt.date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    last_day = (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(start.day, last_day))


def simulate(
    debts: Sequence[DebtInput],
    monthly_budget: Decimal,
    priority: PriorityFn,
    strategy_name: str,
    start_date: dt.date | None = None,
    max_months: int = MAX_MONTHS,
    keep_schedule: bool = True,
    allow_negative_amortisation: bool = False,
) -> PayoffResult:
    """Run the payoff month by month until every balance reaches zero.

    Complexity is ``O(m * n log n)`` for ``m`` months and ``n`` debts -- the
    ``log n`` is the per-month re-prioritisation, which cannot be hoisted out
    of the loop because a promotional rate expiring, or a debt clearing, can
    legitimately change the order mid-plan.

    :param allow_negative_amortisation: when ``True``, a budget under which
        balances grow runs to ``max_months`` and returns ``truncated=True``
        instead of raising. Required for the minimum-payment baseline: a card
        whose minimum no longer covers its interest is a real and common
        situation, and reporting "this never pays off" is the whole point of
        the comparison.

    :raises InsufficientBudgetError: if the budget cannot cover the minimums,
        or -- unless ``allow_negative_amortisation`` is set -- if it covers
        them but balances still grow, which is the more insidious failure of
        the two.
    """

    if not debts:
        raise DomainError("No debts to plan.", code="no_debts")

    monthly_budget = money(monthly_budget)
    floor = minimum_budget(debts)
    if monthly_budget < floor:
        raise InsufficientBudgetError(
            f"A budget of at least {floor} is required to cover minimum payments.",
            details={"minimum_required": str(floor), "provided": str(monthly_budget)},
        )

    start_date = start_date or dt.date.today()
    balances: dict[str, Decimal] = {d.key: d.balance for d in debts}
    by_key: dict[str, DebtInput] = {d.key: d for d in debts}
    interest_paid: dict[str, Decimal] = {d.key: ZERO for d in debts}
    total_paid_per_debt: dict[str, Decimal] = {d.key: ZERO for d in debts}
    payoff_month: dict[str, int] = {}

    schedule: list[MonthSnapshot] = []
    payoff_order: list[str] = []
    total_interest = ZERO
    total_paid = ZERO
    original_balance = money(sum(balances.values(), ZERO))

    month_index = 0
    truncated = False
    negative_amortisation = False

    while any(balance > ZERO for balance in balances.values()):
        if month_index >= max_months:
            truncated = True
            break

        opening = dict(balances)

        # --- 1. Accrue interest -------------------------------------------
        interest_this_month: dict[str, Decimal] = {}
        for debt in debts:
            balance = balances[debt.key]
            if balance <= ZERO:
                interest_this_month[debt.key] = ZERO
                continue
            charge = money(balance * monthly_rate(debt.apr_in_month(month_index)))
            interest_this_month[debt.key] = charge
            balances[debt.key] = balance + charge
            interest_paid[debt.key] += charge
            total_interest += charge

        # --- 2. Contractual minimums --------------------------------------
        budget_left = monthly_budget
        payments: dict[str, Decimal] = {d.key: ZERO for d in debts}
        for debt in debts:
            balance = balances[debt.key]
            if balance <= ZERO:
                continue
            due = min(debt.required_payment(balance), balance, budget_left)
            payments[debt.key] = due
            balances[debt.key] = balance - due
            budget_left -= due

        # --- 3. Cascade the surplus ---------------------------------------
        if budget_left > ZERO:
            for key in priority(debts, balances, month_index):
                if budget_left <= ZERO:
                    break
                balance = balances[key]
                if balance <= ZERO:
                    continue
                extra = min(balance, budget_left)
                payments[key] += extra
                balances[key] = balance - extra
                budget_left -= extra

        month_payment = money(sum(payments.values(), ZERO))
        total_paid += month_payment
        for key, amount in payments.items():
            total_paid_per_debt[key] += amount

        # A month in which nothing was repaid means interest is outrunning the
        # budget; continuing would loop until MAX_MONTHS and hand the customer
        # a nonsense date.
        not_amortising = month_payment <= ZERO or money(sum(balances.values(), ZERO)) >= money(
            sum(opening.values(), ZERO)
        )
        if not_amortising:
            if not allow_negative_amortisation:
                raise InsufficientBudgetError(
                    "Interest exceeds the payment budget; balances would never fall.",
                    details={
                        "monthly_budget": str(monthly_budget),
                        "month_index": month_index,
                    },
                )
            # Balances are growing. Continuing would burn MAX_MONTHS of
            # iterations to reach the same conclusion, so stop here and let
            # ``truncated`` carry the message.
            negative_amortisation = True
            month_index += 1
            truncated = True
            break

        for key in balances:
            if balances[key] <= ZERO and key not in payoff_month:
                payoff_month[key] = month_index + 1
                payoff_order.append(key)

        if keep_schedule:
            schedule.append(
                MonthSnapshot(
                    month_index=month_index,
                    date=_add_months(start_date, month_index),
                    total_payment=month_payment,
                    total_interest=money(sum(interest_this_month.values(), ZERO)),
                    total_balance=money(sum(balances.values(), ZERO)),
                    lines=tuple(
                        DebtMonth(
                            key=d.key,
                            opening_balance=opening[d.key],
                            interest_charged=interest_this_month[d.key],
                            payment=payments[d.key],
                            closing_balance=balances[d.key],
                        )
                        for d in debts
                    ),
                )
            )

        month_index += 1

    months = month_index
    outcomes = tuple(
        DebtOutcome(
            key=key,
            name=by_key[key].name,
            original_balance=by_key[key].balance,
            interest_paid=money(interest_paid[key]),
            total_paid=money(total_paid_per_debt[key]),
            months_to_payoff=payoff_month.get(key, months),
            payoff_date=_add_months(start_date, max(payoff_month.get(key, months) - 1, 0)),
        )
        for key in by_key
    )

    return PayoffResult(
        strategy=strategy_name,
        monthly_budget=monthly_budget,
        months_to_debt_free=months,
        debt_free_date=_add_months(start_date, max(months - 1, 0)),
        total_interest=money(total_interest),
        total_paid=money(total_paid),
        original_balance=original_balance,
        order=tuple(payoff_order),
        outcomes=outcomes,
        schedule=schedule,
        truncated=truncated,
        negative_amortisation=negative_amortisation,
    )
