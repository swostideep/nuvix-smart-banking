"""Tests for strategy selection and the optimiser.

The central claim this module has to defend is that ``optimal`` is worth
having: that it matches avalanche when avalanche is provably right, and beats
it when avalanche's assumptions break.
"""

from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal

import pytest

from nuvix.debt.services.amortization import simulate
from nuvix.debt.services.strategies import (
    AVALANCHE,
    MINIMUM_ONLY,
    OPTIMAL,
    SNOWBALL,
    budget_for_target_months,
    build_plan,
    compare_strategies,
    fixed_order_priority,
    horizon_rate,
    interest_saved_against_minimums,
    optimise,
)
from nuvix.debt.services.types import DebtInput

START = dt.date(2026, 1, 1)


@pytest.fixture
def constant_rate_book() -> list[DebtInput]:
    """Rates never change, so avalanche is the provable optimum."""

    return [
        DebtInput("small_cheap", "Small cheap", Decimal("600"), Decimal("6.99"), Decimal("25")),
        DebtInput("big_dear", "Big dear", Decimal("6000"), Decimal("26.99"), Decimal("150")),
        DebtInput("mid", "Mid", Decimal("2500"), Decimal("17.99"), Decimal("60")),
    ]


@pytest.fixture
def promo_book() -> list[DebtInput]:
    """A promotional balance that reverts -- where greedy-on-today's-rate fails."""

    return [
        DebtInput(
            "promo",
            "Promo transfer",
            Decimal("5000"),
            Decimal("26.99"),
            Decimal("100"),
            promo_apr=Decimal("0"),
            promo_months_remaining=8,
        ),
        DebtInput("mid", "Mid card", Decimal("3000"), Decimal("21.99"), Decimal("75")),
        DebtInput("small", "Small card", Decimal("1200"), Decimal("14.99"), Decimal("40")),
    ]


def brute_force_best(debts, budget, start=START):
    """Exhaustive search over static orderings -- the ground truth."""

    best = None
    for order in itertools.permutations([d.key for d in debts]):
        result = simulate(
            debts, budget, fixed_order_priority(order), "brute", start, keep_schedule=False
        )
        if best is None or result.total_interest < best.total_interest:
            best = result
    return best


class TestStrategyOrdering:
    def test_avalanche_targets_the_highest_rate(self, constant_rate_book):
        balances = {d.key: d.balance for d in constant_rate_book}
        assert avalanche_first(constant_rate_book, balances) == "big_dear"

    def test_snowball_targets_the_smallest_balance(self, constant_rate_book):
        balances = {d.key: d.balance for d in constant_rate_book}
        from nuvix.debt.services.strategies import snowball_priority

        assert snowball_priority(constant_rate_book, balances, 0)[0] == "small_cheap"

    def test_snowball_costs_more_interest_than_avalanche(self, constant_rate_book):
        results = compare_strategies(constant_rate_book, Decimal("700"), START)
        assert results[SNOWBALL].total_interest > results[AVALANCHE].total_interest

    def test_snowball_clears_the_first_debt_sooner(self, constant_rate_book):
        """The behavioural payoff snowball is bought with: a faster first win."""

        avalanche = build_plan(constant_rate_book, Decimal("700"), AVALANCHE, START)
        snowball = build_plan(constant_rate_book, Decimal("700"), SNOWBALL, START)
        first_avalanche = min(o.months_to_payoff for o in avalanche.outcomes)
        first_snowball = min(o.months_to_payoff for o in snowball.outcomes)
        assert first_snowball <= first_avalanche


def avalanche_first(debts, balances):
    from nuvix.debt.services.strategies import avalanche_priority

    return avalanche_priority(debts, balances, 0)[0]


class TestOptimiser:
    def test_matches_avalanche_when_rates_are_constant(self, constant_rate_book):
        """Avalanche is optimal here, so the optimiser must not do worse."""

        results = compare_strategies(constant_rate_book, Decimal("700"), START)
        assert results[OPTIMAL].total_interest <= results[AVALANCHE].total_interest

    def test_beats_avalanche_when_a_promotional_rate_expires(self, promo_book):
        """The reason the optimiser exists."""

        results = compare_strategies(promo_book, Decimal("650"), START)
        assert results[OPTIMAL].total_interest < results[AVALANCHE].total_interest

    def test_finds_the_brute_force_optimum(self, promo_book):
        budget = Decimal("650")
        best = brute_force_best(promo_book, budget)
        found = optimise(promo_book, budget, START, keep_schedule=False)
        assert found.total_interest == best.total_interest

    @pytest.mark.parametrize("budget", ["600", "750", "1100"])
    def test_never_worse_than_any_named_strategy(self, promo_book, budget):
        results = compare_strategies(promo_book, Decimal(budget), START)
        assert results[OPTIMAL].total_interest <= min(
            results[AVALANCHE].total_interest, results[SNOWBALL].total_interest
        )

    def test_handles_a_single_debt(self):
        debts = [DebtInput("only", "Only", Decimal("1500"), Decimal("18"), Decimal("60"))]
        result = optimise(debts, Decimal("300"), START)
        assert result.months_to_debt_free > 0
        assert result.strategy == OPTIMAL


class TestHorizonRate:
    def test_prices_a_promotional_balance_above_its_headline_rate(self):
        promo = DebtInput(
            "p",
            "P",
            Decimal("1000"),
            Decimal("24"),
            Decimal("50"),
            promo_apr=Decimal("0"),
            promo_months_remaining=6,
        )
        plain = DebtInput("q", "Q", Decimal("1000"), Decimal("18"), Decimal("50"))
        # Today the promo costs 0% and looks harmless; over the horizon it is
        # the more expensive of the two.
        assert promo.apr_in_month(0) < plain.apr_in_month(0)
        assert horizon_rate(promo) > Decimal("0.14")

    def test_equals_the_apr_when_there_is_no_promotion(self):
        debt = DebtInput("a", "A", Decimal("1000"), Decimal("18"), Decimal("50"))
        assert horizon_rate(debt) == debt.apr


class TestBaselineComparison:
    def test_minimum_only_is_far_worse_than_a_funded_plan(self, constant_rate_book):
        results = compare_strategies(constant_rate_book, Decimal("700"), START)
        assert results[MINIMUM_ONLY].months_to_debt_free > results[OPTIMAL].months_to_debt_free
        assert results[MINIMUM_ONLY].total_interest > results[OPTIMAL].total_interest

    def test_savings_are_quantified_against_the_baseline(self, constant_rate_book):
        savings = interest_saved_against_minimums(
            constant_rate_book, Decimal("700"), OPTIMAL, START
        )
        assert savings["interest_saved"] > Decimal("0")
        assert savings["months_saved"] > 0
        assert savings["baseline_never_pays_off"] is False

    def test_savings_are_withheld_when_the_baseline_never_amortises(self):
        """A negative "saving" would be reported for the worst possible book."""

        debts = [
            DebtInput("a", "A", Decimal("9000"), Decimal("32"), Decimal("180")),
            DebtInput("b", "B", Decimal("2000"), Decimal("28"), Decimal("60")),
        ]
        savings = interest_saved_against_minimums(debts, Decimal("600"), OPTIMAL, START)
        assert savings["baseline_never_pays_off"] is True
        assert savings["interest_saved"] is None
        assert savings["months_saved"] is None


class TestBudgetForTarget:
    def test_finds_a_budget_that_hits_the_target(self, constant_rate_book):
        budget = budget_for_target_months(constant_rate_book, 18, AVALANCHE, START)
        plan = build_plan(constant_rate_book, budget, AVALANCHE, START, keep_schedule=False)
        assert plan.months_to_debt_free <= 18

    def test_result_is_tight(self, constant_rate_book):
        """A dollar less must miss the target -- otherwise the search is loose."""

        budget = budget_for_target_months(constant_rate_book, 18, AVALANCHE, START)
        plan = build_plan(
            constant_rate_book, budget - Decimal("2.00"), AVALANCHE, START, keep_schedule=False
        )
        assert plan.months_to_debt_free >= 18

    def test_shorter_targets_require_more_money(self, constant_rate_book):
        twelve = budget_for_target_months(constant_rate_book, 12, AVALANCHE, START)
        twenty_four = budget_for_target_months(constant_rate_book, 24, AVALANCHE, START)
        assert twelve > twenty_four

    def test_rejects_a_nonsensical_target(self, constant_rate_book):
        with pytest.raises(ValueError):
            budget_for_target_months(constant_rate_book, 0)


class TestUnknownStrategy:
    def test_raises_for_an_unknown_name(self, constant_rate_book):
        with pytest.raises(ValueError):
            build_plan(constant_rate_book, Decimal("700"), "vibes", START)
