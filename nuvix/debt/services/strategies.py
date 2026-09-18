"""Payoff strategies, and the optimiser that beats the textbook ones.

Three orderings ship as named strategies:

``avalanche``
    Highest effective APR first. Mathematically optimal when every rate is
    constant -- surplus applied to the most expensive dollar always removes
    the most future interest.

``snowball``
    Smallest balance first. Costs more in interest, and is still frequently
    the right recommendation: the behavioural literature is clear that an
    early win keeps people paying, and a plan abandoned in month three is
    worth nothing regardless of its APR arithmetic.

``optimal``
    Local search over orderings, scored by the exact simulator.

Why ``optimal`` is not just avalanche
-------------------------------------
Avalanche's optimality proof assumes a fixed rate per debt. The target market
carries promotional balances -- 0% for nine months, then 26.99%. Greedy on
*today's* rate will happily ignore a 0% balance until the cliff arrives, at
which point the whole balance starts compounding at the standard rate. The
optimiser sees this because it scores candidate orderings on the real
simulator, which knows when each promotion lapses.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

from nuvix.core.money import ZERO, money
from nuvix.debt.services.amortization import MAX_MONTHS, minimum_budget, simulate
from nuvix.debt.services.types import DebtInput, PayoffResult

#: How far ahead the seed ordering looks when averaging a promotional rate
#: against the rate it reverts to. Long enough to price a typical 6-12 month
#: teaser, short enough not to be dominated by balances that will be gone.
RATE_HORIZON_MONTHS = 18

#: Evaluation budget for the local search. Each evaluation is a full
#: simulation, so this bounds worst-case latency at roughly
#: ``MAX_EVALUATIONS * months * debts`` operations.
MAX_EVALUATIONS = 120

AVALANCHE = "avalanche"
SNOWBALL = "snowball"
OPTIMAL = "optimal"
MINIMUM_ONLY = "minimum_only"

STRATEGY_CHOICES = (AVALANCHE, SNOWBALL, OPTIMAL)


def _live(debts: Sequence[DebtInput], balances: dict[str, Decimal]) -> list[DebtInput]:
    return [d for d in debts if balances.get(d.key, ZERO) > ZERO]


def avalanche_priority(
    debts: Sequence[DebtInput], balances: dict[str, Decimal], month_index: int
) -> list[str]:
    """Highest current effective APR first; ties broken by smaller balance."""

    return [
        d.key
        for d in sorted(
            _live(debts, balances),
            key=lambda d: (-d.apr_in_month(month_index), balances[d.key]),
        )
    ]


def snowball_priority(
    debts: Sequence[DebtInput], balances: dict[str, Decimal], month_index: int
) -> list[str]:
    """Smallest remaining balance first; ties broken by higher APR."""

    return [
        d.key
        for d in sorted(
            _live(debts, balances),
            key=lambda d: (balances[d.key], -d.apr_in_month(month_index)),
        )
    ]


def horizon_rate(debt: DebtInput, month_index: int = 0) -> Decimal:
    """Average effective APR over the next :data:`RATE_HORIZON_MONTHS`.

    This is the seed heuristic for the optimiser: a 0% balance with two months
    of promotion left and a 26.99% revert rate prices at roughly 24%, so it
    sorts near the top where greedy-on-today's-rate would bury it.
    """

    total = sum(
        (debt.apr_in_month(month_index + offset) for offset in range(RATE_HORIZON_MONTHS)),
        Decimal(0),
    )
    return (total / Decimal(RATE_HORIZON_MONTHS)).quantize(Decimal("0.000001"))


def horizon_priority(
    debts: Sequence[DebtInput], balances: dict[str, Decimal], month_index: int
) -> list[str]:
    return [
        d.key
        for d in sorted(
            _live(debts, balances),
            key=lambda d: (-horizon_rate(d, month_index), balances[d.key]),
        )
    ]


def fixed_order_priority(order: Sequence[str]):
    """Build a priority function that always follows ``order``.

    Used by the optimiser to score a specific candidate permutation; debts
    already cleared simply fall out because the simulator skips zero balances.
    """

    ranking = {key: index for index, key in enumerate(order)}

    def priority(
        debts: Sequence[DebtInput], balances: dict[str, Decimal], month_index: int
    ) -> list[str]:
        return [
            d.key
            for d in sorted(_live(debts, balances), key=lambda d: ranking.get(d.key, 10**6))
        ]

    return priority


PRIORITY_FUNCTIONS = {
    AVALANCHE: avalanche_priority,
    SNOWBALL: snowball_priority,
}


def build_plan(
    debts: Sequence[DebtInput],
    monthly_budget: Decimal,
    strategy: str = AVALANCHE,
    start_date: dt.date | None = None,
    keep_schedule: bool = True,
) -> PayoffResult:
    """Produce a payoff plan under ``strategy``."""

    if strategy == OPTIMAL:
        return optimise(
            debts, monthly_budget, start_date=start_date, keep_schedule=keep_schedule
        )
    if strategy == MINIMUM_ONLY:
        # The do-nothing baseline must be allowed to fail to amortise --
        # that outcome is the most persuasive thing the comparison can say.
        return simulate(
            debts,
            minimum_budget(debts),
            avalanche_priority,
            MINIMUM_ONLY,
            start_date=start_date,
            keep_schedule=keep_schedule,
            allow_negative_amortisation=True,
        )
    try:
        priority = PRIORITY_FUNCTIONS[strategy]
    except KeyError:
        raise ValueError(f"Unknown strategy {strategy!r}") from None
    return simulate(
        debts,
        monthly_budget,
        priority,
        strategy,
        start_date=start_date,
        keep_schedule=keep_schedule,
    )


def optimise(
    debts: Sequence[DebtInput],
    monthly_budget: Decimal,
    start_date: dt.date | None = None,
    keep_schedule: bool = True,
    max_evaluations: int = MAX_EVALUATIONS,
) -> PayoffResult:
    """Search for the ordering that pays the least total interest.

    Two families of candidate are evaluated on the exact simulator:

    *Dynamic* -- the named strategies, which re-sort the live debts every
    month. Avalanche belongs here, and with constant rates it is provably
    optimal, so it is always in the running.

    *Static* -- a fixed priority order held for the life of the plan. Three
    informed seeds (avalanche order, snowball order, and an order ranked by
    :func:`horizon_rate`) are refined by steepest-descent local search over
    adjacent transpositions: from the best candidate, try every adjacent swap
    and take the first strict improvement, repeating until no swap helps or
    the evaluation budget is spent.

    Adjacent transpositions are the right neighbourhood because the objective
    is close to monotone in the ordering -- swapping two debts that sit far
    apart in priority almost never helps unless the intermediate swaps help
    first.

    Static orders earn their place when rates change over time. Greedy on
    *today's* rate ignores a 0% promotional balance until the cliff arrives;
    committing to clear it during the promotional window can cost less overall
    even though it looks wrong month by month.

    The winner is the cheapest candidate of either family, so the result is
    never worse than plain avalanche. It is a *local* optimum over static
    orders, not a proven global one -- exhaustive search is ``n!`` simulations
    -- and that trade is stated rather than hidden.
    """

    keys = [d.key for d in debts]
    evaluations = 0
    cache: dict[tuple[str, ...], Decimal] = {}

    def cost_of(result: PayoffResult) -> Decimal:
        # Interest is the objective; months break ties so that two equally
        # cheap plans resolve to the one that ends sooner.
        return result.total_interest + Decimal(result.months_to_debt_free) * Decimal("0.001")

    def run(priority, name: str) -> PayoffResult:
        return simulate(
            debts,
            monthly_budget,
            priority,
            name,
            start_date=start_date,
            max_months=MAX_MONTHS,
            keep_schedule=False,
        )

    # --- dynamic candidates ------------------------------------------------
    best_priority = avalanche_priority
    best_cost = cost_of(run(avalanche_priority, AVALANCHE))
    evaluations += 1
    for priority in (snowball_priority, horizon_priority):
        candidate_cost = cost_of(run(priority, OPTIMAL))
        evaluations += 1
        if candidate_cost < best_cost:
            best_priority, best_cost = priority, candidate_cost

    if len(keys) > 1:
        # --- static candidates ---------------------------------------------
        def score(order: Sequence[str]) -> Decimal:
            nonlocal evaluations
            cache_key = tuple(order)
            if cache_key not in cache:
                evaluations += 1
                cache[cache_key] = cost_of(run(fixed_order_priority(order), OPTIMAL))
            return cache[cache_key]

        balances = {d.key: d.balance for d in debts}
        seeds = [
            avalanche_priority(debts, balances, 0),
            snowball_priority(debts, balances, 0),
            horizon_priority(debts, balances, 0),
        ]
        best_order = min(seeds, key=score)
        best_order_cost = score(best_order)

        improved = True
        while improved and evaluations < max_evaluations:
            improved = False
            for index in range(len(best_order) - 1):
                if evaluations >= max_evaluations:
                    break
                candidate = list(best_order)
                candidate[index], candidate[index + 1] = (
                    candidate[index + 1],
                    candidate[index],
                )
                candidate_cost = score(candidate)
                if candidate_cost < best_order_cost:
                    best_order, best_order_cost, improved = candidate, candidate_cost, True
                    break

        if best_order_cost < best_cost:
            best_priority = fixed_order_priority(best_order)

    return simulate(
        debts,
        monthly_budget,
        best_priority,
        OPTIMAL,
        start_date=start_date,
        keep_schedule=keep_schedule,
    )


def compare_strategies(
    debts: Sequence[DebtInput],
    monthly_budget: Decimal,
    start_date: dt.date | None = None,
) -> dict[str, PayoffResult]:
    """Score every strategy plus the minimum-only baseline.

    The baseline is what makes the number meaningful to a customer: "you save
    $4,182 and get out 19 months sooner *than if you keep doing what you are
    doing*" is a far stronger message than an abstract total.
    """

    results = {
        name: build_plan(debts, monthly_budget, name, start_date, keep_schedule=False)
        for name in STRATEGY_CHOICES
    }
    results[MINIMUM_ONLY] = build_plan(
        debts, monthly_budget, MINIMUM_ONLY, start_date, keep_schedule=False
    )
    return results


def interest_saved_against_minimums(
    debts: Sequence[DebtInput],
    monthly_budget: Decimal,
    strategy: str = OPTIMAL,
    start_date: dt.date | None = None,
) -> dict[str, object]:
    """Quantify a plan against the do-nothing baseline."""

    plan = build_plan(debts, monthly_budget, strategy, start_date, keep_schedule=False)
    baseline = build_plan(debts, monthly_budget, MINIMUM_ONLY, start_date, keep_schedule=False)

    # A baseline that never amortises stops early, so its accumulated interest
    # is a partial sum, not a total. Subtracting it would report a *negative*
    # saving for the worst possible starting position -- exactly backwards.
    # Both figures are withheld and the flag carries the real message.
    never_pays_off = baseline.negative_amortisation or baseline.truncated

    return {
        "strategy": strategy,
        "plan_interest": plan.total_interest,
        "plan_months": plan.months_to_debt_free,
        "baseline_interest": baseline.total_interest,
        "baseline_months": None if never_pays_off else baseline.months_to_debt_free,
        "baseline_never_pays_off": never_pays_off,
        "interest_saved": (
            None if never_pays_off else money(baseline.total_interest - plan.total_interest)
        ),
        "months_saved": (
            None if never_pays_off else baseline.months_to_debt_free - plan.months_to_debt_free
        ),
    }


def budget_for_target_months(
    debts: Sequence[DebtInput],
    target_months: int,
    strategy: str = AVALANCHE,
    start_date: dt.date | None = None,
) -> Decimal:
    """Smallest monthly budget that clears every balance within ``target_months``.

    Months-to-payoff is monotonically non-increasing in the budget, so the
    answer is found by binary search on dollars rather than by stepping a
    budget upward one increment at a time. Roughly ``log2(range / $1)``
    simulations -- about 20 for a $0-$50k window -- instead of tens of
    thousands.
    """

    if target_months < 1:
        raise ValueError("target_months must be at least 1")

    low = minimum_budget(debts)
    total = money(sum(d.balance for d in debts))

    # An upper bound that certainly succeeds: clear everything in month one.
    high = money(max(total * Decimal("1.5"), low + Decimal("1")))

    def clears(budget: Decimal) -> bool:
        try:
            plan = build_plan(debts, budget, strategy, start_date, keep_schedule=False)
        except Exception:
            return False
        return not plan.truncated and plan.months_to_debt_free <= target_months

    if clears(low):
        return low
    if not clears(high):
        return high

    # Invariant: ``low`` fails, ``high`` succeeds. Narrow to the cent.
    while high - low > Decimal("1.00"):
        mid = money((low + high) / 2)
        if clears(mid):
            high = mid
        else:
            low = mid
    return high
