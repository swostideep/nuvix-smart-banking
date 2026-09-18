"""Tests for cashflow projection and safe-to-spend."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from nuvix.banking.services.cashflow import DEFAULT_BUFFER, expand_events, project
from nuvix.banking.services.recurrence import DetectedSeries

TODAY = dt.date(2026, 3, 1)


def series(
    label: str, amount: str, cadence: str, next_on: dt.date, is_income: bool = False
) -> DetectedSeries:
    return DetectedSeries(
        merchant_key=label,
        label=label,
        category="other",
        cadence=cadence,
        average_amount=Decimal(amount),
        is_income=is_income,
        occurrences=6,
        confidence=Decimal("0.9"),
        last_seen_on=next_on - dt.timedelta(days=14),
        next_expected_on=next_on,
    )


class TestEventExpansion:
    def test_events_come_out_in_date_order(self):
        events = expand_events(
            [
                series("Rent", "1400", "monthly", dt.date(2026, 3, 3)),
                series("Pay", "2000", "biweekly", dt.date(2026, 3, 6), is_income=True),
                series("Gym", "18", "weekly", dt.date(2026, 3, 2)),
            ],
            TODAY,
            TODAY + dt.timedelta(days=30),
        )
        assert [e.date for e in events] == sorted(e.date for e in events)

    def test_income_is_positive_and_bills_are_negative(self):
        events = expand_events(
            [
                series("Pay", "2000", "monthly", dt.date(2026, 3, 5), is_income=True),
                series("Rent", "1400", "monthly", dt.date(2026, 3, 3)),
            ],
            TODAY,
            TODAY + dt.timedelta(days=10),
        )
        by_label = {e.label: e.amount for e in events}
        assert by_label["Pay"] > 0
        assert by_label["Rent"] < 0

    def test_a_series_repeats_across_the_window(self):
        events = expand_events(
            [series("Gym", "18", "weekly", dt.date(2026, 3, 2))],
            TODAY,
            TODAY + dt.timedelta(days=28),
        )
        assert len(events) == 4

    def test_an_overdue_series_is_pulled_to_the_window_start(self):
        """A late bill is still owed; dropping it would overstate safe-to-spend."""

        overdue = series("Rent", "1400", "monthly", dt.date(2026, 2, 20))
        events = expand_events([overdue], TODAY, TODAY + dt.timedelta(days=10))
        assert events[0].date == TODAY

    def test_nothing_is_emitted_beyond_the_horizon(self):
        events = expand_events(
            [series("Rent", "1400", "monthly", dt.date(2026, 6, 1))],
            TODAY,
            TODAY + dt.timedelta(days=30),
        )
        assert events == []


class TestProjection:
    def test_balance_follows_the_events(self):
        projection = project(
            Decimal("1000"),
            [
                series("Rent", "400", "monthly", dt.date(2026, 3, 5)),
                series("Pay", "900", "monthly", dt.date(2026, 3, 10), is_income=True),
            ],
            TODAY,
            horizon_days=20,
        )
        assert projection.opening_balance == Decimal("1000.00")
        assert projection.closing_balance == Decimal("1500.00")
        assert projection.total_inflow == Decimal("900.00")
        assert projection.total_outflow == Decimal("400.00")

    def test_the_trough_is_found_not_the_endpoint(self):
        """A month that ends healthy can still dip below zero mid-way."""

        projection = project(
            Decimal("500"),
            [
                series("Rent", "1200", "monthly", dt.date(2026, 3, 4)),
                series("Pay", "2000", "monthly", dt.date(2026, 3, 20), is_income=True),
            ],
            TODAY,
            horizon_days=30,
        )
        assert projection.closing_balance > Decimal("0")
        assert projection.lowest_balance < Decimal("0")
        assert projection.lowest_balance_on == dt.date(2026, 3, 4)
        assert projection.shortfall_risk is True

    def test_safe_to_spend_reserves_money_needed_before_payday(self):
        projection = project(
            Decimal("1000"),
            [
                series("Rent", "600", "monthly", dt.date(2026, 3, 4)),
                series("Pay", "2000", "monthly", dt.date(2026, 3, 20), is_income=True),
            ],
            TODAY,
            horizon_days=30,
        )
        # $1000 - $600 rent = $400 trough, less the buffer.
        assert projection.safe_to_spend == Decimal("400.00") - DEFAULT_BUFFER

    def test_safe_to_spend_is_never_negative(self):
        projection = project(
            Decimal("100"),
            [series("Rent", "900", "monthly", dt.date(2026, 3, 4))],
            TODAY,
            horizon_days=30,
        )
        assert projection.safe_to_spend == Decimal("0.00")

    def test_next_income_is_the_earliest_inflow(self):
        projection = project(
            Decimal("1000"),
            [
                series("Bonus", "500", "monthly", dt.date(2026, 3, 25), is_income=True),
                series("Pay", "2000", "biweekly", dt.date(2026, 3, 6), is_income=True),
            ],
            TODAY,
            horizon_days=30,
        )
        assert projection.next_income_on == dt.date(2026, 3, 6)
        assert projection.next_income_amount == Decimal("2000.00")

    def test_no_series_means_a_flat_projection(self):
        projection = project(Decimal("250"), [], TODAY, horizon_days=14)
        assert projection.closing_balance == Decimal("250.00")
        assert projection.shortfall_risk is False
        assert len(projection.daily_balances) == 15

    @pytest.mark.parametrize("horizon", [1, 7, 45, 180])
    def test_daily_balances_cover_the_whole_horizon_inclusive(self, horizon):
        projection = project(Decimal("100"), [], TODAY, horizon_days=horizon)
        assert len(projection.daily_balances) == horizon + 1
        assert projection.daily_balances[0][0] == TODAY
        assert projection.daily_balances[-1][0] == TODAY + dt.timedelta(days=horizon)
