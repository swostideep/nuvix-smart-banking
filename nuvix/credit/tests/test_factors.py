"""Tests for the credit factor model."""

from __future__ import annotations

from decimal import Decimal

import pytest

from nuvix.credit.engine.factors import (
    SCORE_MAX,
    SCORE_MIN,
    WEIGHTS,
    CreditInputs,
    band_for,
    score_profile,
)


def perfect() -> CreditInputs:
    return CreditInputs(
        on_time_payment_rate=Decimal("1.0"),
        total_balance=Decimal("300"),
        total_credit_limit=Decimal("20000"),
        max_single_card_utilization=Decimal("0.02"),
        oldest_account_months=180,
        average_account_age_months=120,
        credit_mix_types=4,
        open_accounts=6,
        hard_inquiries_12m=0,
    )


class TestModelShape:
    def test_weights_sum_to_one(self):
        assert sum(WEIGHTS.values()) == Decimal("1.00")

    def test_a_file_with_no_history_scores_poorly(self):
        """A brand-new consumer must not inherit a perfect payment record."""

        result = score_profile(CreditInputs(total_credit_limit=Decimal("0")))
        assert SCORE_MIN <= result.score < 580
        assert result.band == "poor"

    def test_no_history_zeroes_the_payment_factor(self):
        result = score_profile(CreditInputs(oldest_account_months=0))
        payment = next(f for f in result.factors if f.key == "payment_history")
        assert payment.points_earned == 0
        assert "No payment history" in payment.reason

    def test_one_on_time_month_beats_an_empty_file(self):
        empty = score_profile(CreditInputs(total_credit_limit=Decimal("2000")))
        started = score_profile(
            CreditInputs(total_credit_limit=Decimal("2000"), oldest_account_months=1)
        )
        assert started.score > empty.score

    def test_perfect_profile_approaches_the_ceiling(self):
        result = score_profile(perfect())
        assert result.score > 800
        assert result.score <= SCORE_MAX

    def test_score_is_always_in_range(self):
        wrecked = CreditInputs(
            on_time_payment_rate=Decimal("0.2"),
            derogatory_marks=9,
            total_balance=Decimal("9999"),
            total_credit_limit=Decimal("1000"),
            hard_inquiries_12m=12,
        )
        assert SCORE_MIN <= score_profile(wrecked).score <= SCORE_MAX

    def test_every_factor_is_reported(self):
        result = score_profile(perfect())
        assert {f.key for f in result.factors} == set(WEIGHTS)

    def test_points_earned_and_available_sum_to_the_factor_weight(self):
        result = score_profile(
            CreditInputs(total_credit_limit=Decimal("5000"), oldest_account_months=24)
        )
        for factor in result.factors:
            total = factor.points_earned + factor.points_available
            assert total == pytest.approx(float(factor.weight) * 550, abs=1)


class TestMonotonicity:
    """Every factor must move the score in the direction a customer expects."""

    def test_lower_utilisation_never_lowers_the_score(self):
        scores = [
            score_profile(
                CreditInputs(
                    total_balance=Decimal(balance),
                    total_credit_limit=Decimal("10000"),
                    oldest_account_months=60,
                )
            ).score
            for balance in ["9000", "6000", "3000", "1000", "500"]
        ]
        assert scores == sorted(scores)

    def test_more_late_payments_never_raise_the_score(self):
        scores = [
            score_profile(
                CreditInputs(
                    on_time_payment_rate=Decimal("0.95"),
                    late_payments_30d=count,
                    total_credit_limit=Decimal("5000"),
                    oldest_account_months=60,
                )
            ).score
            for count in range(5)
        ]
        assert scores == sorted(scores, reverse=True)

    def test_longer_history_never_lowers_the_score(self):
        scores = [
            score_profile(
                CreditInputs(oldest_account_months=months, total_credit_limit=Decimal("5000"))
            ).score
            for months in [0, 6, 24, 60, 120, 240]
        ]
        assert scores == sorted(scores)

    def test_more_inquiries_never_raise_the_score(self):
        scores = [
            score_profile(
                CreditInputs(
                    hard_inquiries_12m=n,
                    total_credit_limit=Decimal("5000"),
                    oldest_account_months=60,
                )
            ).score
            for n in range(6)
        ]
        assert scores == sorted(scores, reverse=True)


class TestUtilizationCurve:
    def test_single_digit_utilisation_is_the_ideal_band(self):
        best = score_profile(
            CreditInputs(total_balance=Decimal("400"), total_credit_limit=Decimal("10000"))
        )
        factor = next(f for f in best.factors if f.key == "utilization")
        assert factor.sub_score == Decimal("1.000")

    def test_zero_usage_scores_below_light_usage(self):
        """A card that is never used reports no activity."""

        unused = score_profile(
            CreditInputs(
                total_balance=Decimal("0"),
                total_credit_limit=Decimal("10000"),
                oldest_account_months=60,
            )
        )
        light = score_profile(
            CreditInputs(
                total_balance=Decimal("400"),
                total_credit_limit=Decimal("10000"),
                oldest_account_months=60,
            )
        )
        assert unused.score < light.score

    def test_one_maxed_card_caps_the_factor_despite_healthy_aggregate(self):
        aggregate_only = CreditInputs(
            total_balance=Decimal("1000"),
            total_credit_limit=Decimal("10000"),
            max_single_card_utilization=Decimal("0.05"),
            oldest_account_months=60,
        )
        one_maxed = CreditInputs(
            total_balance=Decimal("1000"),
            total_credit_limit=Decimal("10000"),
            max_single_card_utilization=Decimal("0.97"),
            oldest_account_months=60,
        )
        assert score_profile(one_maxed).score < score_profile(aggregate_only).score

    def test_no_limit_on_file_is_flagged_not_crashed(self):
        result = score_profile(CreditInputs(total_credit_limit=Decimal("0")))
        factor = next(f for f in result.factors if f.key == "utilization")
        assert "secured card" in factor.reason


class TestExplanations:
    def test_biggest_opportunity_points_at_the_weakest_factor(self):
        inputs = CreditInputs(
            on_time_payment_rate=Decimal("1.0"),
            total_balance=Decimal("9500"),
            total_credit_limit=Decimal("10000"),
            oldest_account_months=180,
            credit_mix_types=4,
            hard_inquiries_12m=0,
        )
        assert score_profile(inputs).biggest_opportunity.key == "utilization"

    def test_a_perfect_profile_has_no_opportunity(self):
        assert score_profile(perfect()).biggest_opportunity is None

    def test_every_factor_carries_a_reason(self):
        for factor in score_profile(CreditInputs(oldest_account_months=36)).factors:
            assert factor.reason.strip()

    def test_derogatory_marks_are_named_in_the_reason(self):
        result = score_profile(
            CreditInputs(
                derogatory_marks=2,
                total_credit_limit=Decimal("5000"),
                oldest_account_months=60,
            )
        )
        factor = next(f for f in result.factors if f.key == "payment_history")
        assert "derogatory" in factor.reason.lower()


class TestBands:
    @pytest.mark.parametrize(
        "score,expected",
        [(810, "excellent"), (750, "very_good"), (700, "good"), (600, "fair"), (450, "poor")],
    )
    def test_band_boundaries(self, score, expected):
        assert band_for(score) == expected
