"""Tests for the payoff simulator.

These are the closest thing the platform has to safety-critical code: the
numbers here go on a customer's dashboard and shape a real repayment decision.
So the assertions check arithmetic against independently computed values, not
just that the function returns something.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from nuvix.core.exceptions import InsufficientBudgetError
from nuvix.core.money import money, monthly_rate
from nuvix.debt.services.amortization import minimum_budget, simulate
from nuvix.debt.services.strategies import avalanche_priority, snowball_priority
from nuvix.debt.services.types import DebtInput

START = dt.date(2026, 1, 1)


def single(balance="1000", apr="12", minimum="100", **kwargs) -> list[DebtInput]:
    return [DebtInput("a", "Card", Decimal(balance), Decimal(apr), Decimal(minimum), **kwargs)]


class TestInterestArithmetic:
    def test_first_month_interest_matches_hand_calculation(self):
        """A 12% APR on $1,000 must charge exactly $10.00 in month one."""

        debts = single(balance="1000", apr="12", minimum="100")
        result = simulate(debts, Decimal("100"), avalanche_priority, "t", START)

        first = result.schedule[0]
        assert first.lines[0].interest_charged == Decimal("10.00")
        # $10 interest, $100 paid -> $910 remaining.
        assert first.lines[0].closing_balance == Decimal("910.00")

    def test_monthly_rate_is_apr_over_twelve(self):
        assert monthly_rate(Decimal("0.12")) == Decimal("0.01000000")

    def test_zero_apr_pays_off_in_exact_whole_months(self):
        debts = single(balance="1200", apr="0", minimum="100")
        result = simulate(debts, Decimal("100"), avalanche_priority, "t", START)
        assert result.months_to_debt_free == 12
        assert result.total_interest == Decimal("0.00")

    def test_total_paid_equals_principal_plus_interest(self):
        """The fundamental accounting identity of any payoff schedule."""

        debts = [
            DebtInput("a", "A", Decimal("2400"), Decimal("19.99"), Decimal("60")),
            DebtInput("b", "B", Decimal("1100"), Decimal("24.99"), Decimal("40")),
        ]
        result = simulate(debts, Decimal("400"), avalanche_priority, "t", START)
        assert result.total_paid == money(result.original_balance + result.total_interest)


class TestBudgetGuards:
    def test_budget_below_minimums_is_rejected(self):
        debts = [
            DebtInput("a", "A", Decimal("2000"), Decimal("20"), Decimal("80")),
            DebtInput("b", "B", Decimal("2000"), Decimal("20"), Decimal("80")),
        ]
        with pytest.raises(InsufficientBudgetError) as exc:
            simulate(debts, Decimal("150"), avalanche_priority, "t", START)
        assert exc.value.details["minimum_required"] == "160.00"

    def test_minimum_budget_sums_contractual_minimums(self):
        debts = [
            DebtInput("a", "A", Decimal("2000"), Decimal("20"), Decimal("80")),
            DebtInput("b", "B", Decimal("500"), Decimal("20"), Decimal("40")),
        ]
        assert minimum_budget(debts) == Decimal("120.00")

    def test_minimum_is_capped_by_a_small_remaining_balance(self):
        """A $30 balance with a $50 minimum owes $30, not $50."""

        debts = single(balance="30", apr="24", minimum="50")
        assert minimum_budget(debts) == Decimal("30.00")

    def test_interest_outrunning_payment_raises_rather_than_looping(self):
        # $10,000 at 36% accrues $300/month against a $120 payment.
        debts = single(balance="10000", apr="36", minimum="120")
        with pytest.raises(InsufficientBudgetError):
            simulate(debts, Decimal("120"), avalanche_priority, "t", START)

    def test_negative_amortisation_is_reported_when_allowed(self):
        debts = single(balance="10000", apr="36", minimum="120")
        result = simulate(
            debts,
            Decimal("120"),
            avalanche_priority,
            "t",
            START,
            allow_negative_amortisation=True,
        )
        assert result.negative_amortisation is True
        assert result.truncated is True


class TestSurplusCascade:
    def test_surplus_goes_to_the_priority_debt(self):
        debts = [
            DebtInput("cheap", "Cheap", Decimal("1000"), Decimal("5"), Decimal("50")),
            DebtInput("dear", "Dear", Decimal("1000"), Decimal("29"), Decimal("50")),
        ]
        result = simulate(debts, Decimal("400"), avalanche_priority, "t", START)
        first = {line.key: line.payment for line in result.schedule[0].lines}
        # Both minimums paid, and the entire $300 surplus goes to the 29% debt.
        assert first["cheap"] == Decimal("50.00")
        assert first["dear"] == Decimal("350.00")

    def test_overpayment_is_trimmed_and_cascades_to_the_next_debt(self):
        """Paying off one debt must roll the leftover into the next, not vanish."""

        debts = [
            DebtInput("small", "Small", Decimal("100"), Decimal("29"), Decimal("25")),
            DebtInput("big", "Big", Decimal("5000"), Decimal("10"), Decimal("100")),
        ]
        result = simulate(debts, Decimal("600"), avalanche_priority, "t", START)
        month = result.schedule[0]
        assert month.total_payment == Decimal("600.00")
        paid = {line.key: line.payment for line in month.lines}
        # The small debt takes only what it owes; the rest lands on the big one.
        assert paid["small"] < Decimal("130.00")
        assert paid["small"] + paid["big"] == Decimal("600.00")

    def test_a_cleared_debt_frees_its_minimum_for_the_others(self):
        debts = [
            DebtInput("small", "Small", Decimal("200"), Decimal("20"), Decimal("100")),
            DebtInput("big", "Big", Decimal("3000"), Decimal("20"), Decimal("100")),
        ]
        result = simulate(debts, Decimal("400"), snowball_priority, "t", START)
        # Once "small" is gone the whole $400 must be going to "big".
        later = result.schedule[4]
        assert later.total_payment == Decimal("400.00")


class TestPromotionalRates:
    def test_promo_rate_applies_then_reverts(self):
        debts = single(
            balance="6000",
            apr="24",
            minimum="150",
            promo_apr=Decimal("0"),
            promo_months_remaining=3,
        )
        result = simulate(debts, Decimal("150"), avalanche_priority, "t", START)

        # No interest while the promotion runs...
        assert all(m.total_interest == Decimal("0.00") for m in result.schedule[:3])
        # ...and interest from the month it lapses.
        assert result.schedule[3].total_interest > Decimal("0.00")

    def test_apr_in_month_reports_the_cliff(self):
        debt = DebtInput(
            "a",
            "A",
            Decimal("1000"),
            Decimal("24"),
            Decimal("50"),
            promo_apr=Decimal("0"),
            promo_months_remaining=2,
        )
        assert debt.apr_in_month(0) == Decimal("0.000000")
        assert debt.apr_in_month(1) == Decimal("0.000000")
        assert debt.apr_in_month(2) == Decimal("0.240000")


class TestScheduleShape:
    def test_debt_free_date_advances_by_calendar_month(self):
        debts = single(balance="1200", apr="0", minimum="100")
        result = simulate(debts, Decimal("100"), avalanche_priority, "t", dt.date(2026, 1, 31))
        # Stepping by calendar month from the 31st must clamp, never overflow.
        assert result.schedule[1].date == dt.date(2026, 2, 28)

    def test_outcomes_cover_every_debt_exactly_once(
        self,
    ):
        debts = [
            DebtInput("a", "A", Decimal("1000"), Decimal("10"), Decimal("50")),
            DebtInput("b", "B", Decimal("2000"), Decimal("20"), Decimal("60")),
        ]
        result = simulate(debts, Decimal("500"), avalanche_priority, "t", START)
        assert {o.key for o in result.outcomes} == {"a", "b"}
        assert sum(o.interest_paid for o in result.outcomes) == result.total_interest

    def test_schedule_can_be_omitted_for_speed(self):
        debts = single()
        result = simulate(
            debts, Decimal("100"), avalanche_priority, "t", START, keep_schedule=False
        )
        assert result.schedule == []
        assert result.months_to_debt_free > 0
