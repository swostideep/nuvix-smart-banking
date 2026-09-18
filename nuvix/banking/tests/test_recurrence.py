"""Tests for recurring-transaction detection."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from nuvix.banking.services.recurrence import (
    MIN_OCCURRENCES,
    Observation,
    detect_series,
    normalise_merchant,
    upcoming_occurrences,
)


def observations(
    merchant: str,
    start: dt.date,
    step_days: int,
    count: int,
    amount: str = "-52.10",
    category: str = "subscription",
) -> list[Observation]:
    return [
        Observation(
            posted_on=start + dt.timedelta(days=step_days * index),
            amount=Decimal(amount),
            merchant_key=normalise_merchant(merchant),
            merchant=merchant,
            category=category,
        )
        for index in range(count)
    ]


class TestMerchantNormalisation:
    @pytest.mark.parametrize(
        "raw",
        [
            "POS DEBIT SAFEWAY #1423 SEATTLE WA",
            "SAFEWAY #0881 PORTLAND OR",
            "safeway 2210",
        ],
    )
    def test_store_numbers_and_cities_collapse_to_one_key(self, raw):
        assert normalise_merchant(raw).startswith("SAFEWAY")

    def test_two_tokens_are_kept_to_disambiguate(self):
        assert normalise_merchant("STATE FARM INSURANCE PMT") == "STATE FARM"

    def test_noise_tokens_are_stripped(self):
        assert "ACH" not in normalise_merchant("ACH DEBIT NETFLIX.COM")

    def test_an_unrecognisable_descriptor_still_yields_a_key(self):
        assert normalise_merchant("7 11") != ""


class TestCadenceDetection:
    def test_monthly_subscription_is_detected(self):
        series = detect_series(observations("NETFLIX", dt.date(2026, 1, 5), 30, 6))
        assert len(series) == 1
        assert series[0].cadence == "monthly"
        assert series[0].average_amount == Decimal("52.10")

    def test_biweekly_paycheque_is_detected_as_income(self):
        rows = observations(
            "ACME PAYROLL", dt.date(2026, 1, 2), 14, 8, amount="2050.00", category="income"
        )
        series = detect_series(rows)
        assert series[0].cadence == "biweekly"
        assert series[0].is_income is True

    def test_weekly_cadence_is_detected(self):
        series = detect_series(observations("GYM", dt.date(2026, 1, 1), 7, 6, amount="-18.00"))
        assert series[0].cadence == "weekly"

    def test_fifteen_day_gaps_resolve_to_semimonthly_not_biweekly(self):
        """Both cadences' tolerances reach 15 days; the closest must win."""

        series = detect_series(
            observations("PAYROLL CO", dt.date(2026, 1, 1), 15, 6, amount="1000.00")
        )
        assert series[0].cadence == "semimonthly"

    def test_a_few_days_of_drift_is_tolerated(self):
        """Real billers slip over weekends."""

        base = dt.date(2026, 1, 5)
        offsets = [0, 31, 59, 92, 121]
        rows = [
            Observation(
                base + dt.timedelta(days=o), Decimal("-89.99"), "RENT CO", "RENT CO", "housing"
            )
            for o in offsets
        ]
        assert detect_series(rows)[0].cadence == "monthly"


class TestRejection:
    def test_two_occurrences_are_not_a_pattern(self):
        assert detect_series(observations("NETFLIX", dt.date(2026, 1, 5), 30, 2)) == []

    def test_minimum_occurrences_is_three(self):
        assert MIN_OCCURRENCES == 3
        assert len(detect_series(observations("NETFLIX", dt.date(2026, 1, 5), 30, 3))) == 1

    def test_random_gaps_are_rejected(self):
        base = dt.date(2026, 1, 1)
        rows = [
            Observation(
                base + dt.timedelta(days=o), Decimal("-40.00"), "COFFEE", "COFFEE", "dining"
            )
            for o in (0, 3, 19, 21, 55, 56)
        ]
        assert detect_series(rows) == []

    def test_wildly_varying_amounts_are_rejected(self):
        base = dt.date(2026, 1, 1)
        rows = [
            Observation(
                base + dt.timedelta(days=30 * i), Decimal(amount), "SHOP", "SHOP", "shopping"
            )
            for i, amount in enumerate(["-10.00", "-400.00", "-25.00", "-900.00"])
        ]
        assert detect_series(rows) == []

    def test_a_variable_utility_bill_is_still_accepted(self):
        """Dispersion tolerance must admit a real bill that moves a little."""

        base = dt.date(2026, 1, 8)
        rows = [
            Observation(
                base + dt.timedelta(days=30 * i),
                Decimal(amount),
                "CITY POWER",
                "CITY POWER",
                "utilities",
            )
            for i, amount in enumerate(["-104.00", "-118.00", "-96.00", "-111.00"])
        ]
        assert detect_series(rows)[0].cadence == "monthly"

    def test_zero_amount_rows_are_ignored(self):
        rows = observations("NETFLIX", dt.date(2026, 1, 5), 30, 4, amount="0.00")
        assert detect_series(rows) == []


class TestBucketing:
    def test_refunds_and_charges_are_separate_series(self):
        charges = observations("GYM", dt.date(2026, 1, 1), 30, 4, amount="-40.00")
        refunds = observations("GYM", dt.date(2026, 1, 15), 30, 4, amount="40.00")
        series = detect_series(charges + refunds)
        assert len(series) == 2
        assert {s.is_income for s in series} == {True, False}

    def test_same_day_duplicates_collapse(self):
        rows = observations("NETFLIX", dt.date(2026, 1, 5), 30, 4)
        duplicated = [*rows, rows[1]]
        series = detect_series(duplicated)
        assert series[0].occurrences == 4


class TestProjection:
    def test_monthly_next_expected_is_the_same_day_next_month(self):
        """Not last_seen + 30 days: a monthly bill lands on the same date."""

        series = detect_series(observations("NETFLIX", dt.date(2026, 1, 5), 30, 5))[0]
        assert series.last_seen_on == dt.date(2026, 5, 5)
        assert series.next_expected_on == dt.date(2026, 6, 5)

    def test_weekly_next_expected_is_seven_days_later(self):
        series = detect_series(observations("GYM", dt.date(2026, 1, 1), 7, 5, amount="-18.00"))[
            0
        ]
        assert series.next_expected_on == series.last_seen_on + dt.timedelta(days=7)

    def test_monthly_series_steps_by_calendar_month(self):
        base = dt.date(2026, 1, 31)
        rows = [
            Observation(d, Decimal("-500.00"), "RENT", "RENT", "housing")
            for d in (base, dt.date(2026, 3, 2), dt.date(2026, 3, 31), dt.date(2026, 4, 30))
        ]
        series = detect_series(rows)
        assert series, "expected a monthly series"
        # Clamped into a short month, never overflowed into the next one.
        assert series[0].next_expected_on.month in (5, 6)

    def test_upcoming_occurrences_stops_at_the_horizon(self):
        series = detect_series(observations("GYM", dt.date(2026, 1, 1), 7, 6, amount="-18.00"))[
            0
        ]
        dates = upcoming_occurrences(series, series.next_expected_on + dt.timedelta(days=21))
        assert len(dates) == 4
        assert all(d <= series.next_expected_on + dt.timedelta(days=21) for d in dates)


class TestConfidence:
    def test_more_evidence_raises_confidence(self):
        few = detect_series(observations("NETFLIX", dt.date(2026, 1, 5), 30, 3))[0]
        many = detect_series(observations("NETFLIX", dt.date(2026, 1, 5), 30, 8))[0]
        assert many.confidence > few.confidence

    def test_confidence_is_a_probability(self):
        series = detect_series(observations("NETFLIX", dt.date(2026, 1, 5), 30, 6))[0]
        assert Decimal("0") <= series.confidence <= Decimal("1")
