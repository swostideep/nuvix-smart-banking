"""Tests for the what-if credit simulator."""

from __future__ import annotations

from decimal import Decimal

import pytest

from nuvix.credit.engine.factors import CreditInputs, score_profile
from nuvix.credit.engine.simulator import UnknownAction, rank_actions, simulate


@pytest.fixture
def stressed() -> CreditInputs:
    """A realistic struggling profile: high utilisation, some history."""

    return CreditInputs(
        on_time_payment_rate=Decimal("0.96"),
        late_payments_30d=1,
        total_balance=Decimal("6400"),
        total_credit_limit=Decimal("8000"),
        max_single_card_utilization=Decimal("0.90"),
        oldest_account_months=72,
        average_account_age_months=36,
        credit_mix_types=2,
        open_accounts=4,
        hard_inquiries_12m=1,
    )


class TestPayDown:
    def test_paying_down_raises_the_score(self, stressed):
        result = simulate(stressed, "pay_down_balance", amount=Decimal("4000"))
        assert result.score_change > 0
        assert result.score_after == result.score_before + result.score_change

    def test_paying_more_helps_more(self, stressed):
        small = simulate(stressed, "pay_down_balance", amount=Decimal("1000"))
        large = simulate(stressed, "pay_down_balance", amount=Decimal("5000"))
        assert large.score_change >= small.score_change

    def test_overpaying_cannot_drive_the_balance_negative(self, stressed):
        result = simulate(stressed, "pay_down_balance", amount=Decimal("99999"))
        assert result.score_after <= 850

    def test_the_utilisation_factor_is_the_one_that_moves(self, stressed):
        result = simulate(stressed, "pay_down_balance", amount=Decimal("4000"))
        moved = {d.key: d.change for d in result.factor_deltas}
        assert moved["utilization"] > 0
        assert moved["credit_age"] == 0


class TestOpenAndClose:
    def test_opening_a_card_costs_an_inquiry(self, stressed):
        """More limit helps utilisation; the inquiry and younger file do not."""

        result = simulate(stressed, "open_new_card", credit_limit=Decimal("2000"))
        moved = {d.key: d.change for d in result.factor_deltas}
        assert moved["new_credit"] < 0

    def test_closing_the_oldest_card_hurts_history(self, stressed):
        result = simulate(stressed, "close_card", credit_limit=Decimal("2000"), is_oldest=True)
        moved = {d.key: d.change for d in result.factor_deltas}
        assert moved["credit_age"] < 0
        assert result.score_change < 0

    def test_closing_a_card_raises_utilisation(self, stressed):
        result = simulate(stressed, "close_card", credit_limit=Decimal("3000"))
        moved = {d.key: d.change for d in result.factor_deltas}
        assert moved["utilization"] <= 0

    def test_a_limit_increase_helps_without_an_inquiry(self, stressed):
        result = simulate(stressed, "increase_limit", amount=Decimal("4000"))
        moved = {d.key: d.change for d in result.factor_deltas}
        assert moved["utilization"] > 0
        assert moved["new_credit"] == 0
        assert result.score_change > 0


class TestTimePassing:
    def test_on_time_months_improve_the_score(self, stressed):
        assert simulate(stressed, "on_time_months", months=12).score_change > 0

    def test_longer_is_better(self, stressed):
        six = simulate(stressed, "on_time_months", months=6)
        thirty_six = simulate(stressed, "on_time_months", months=36)
        assert thirty_six.score_change >= six.score_change

    def test_inquiries_roll_off_after_a_year(self, stressed):
        result = simulate(stressed, "on_time_months", months=12)
        moved = {d.key: d.change for d in result.factor_deltas}
        assert moved["new_credit"] > 0

    def test_zero_months_changes_nothing(self, stressed):
        assert simulate(stressed, "on_time_months", months=0).score_change == 0


class TestConsistency:
    def test_score_before_matches_the_direct_model(self, stressed):
        """The simulator must not have its own idea of the current score."""

        result = simulate(stressed, "on_time_months", months=1)
        assert result.score_before == score_profile(stressed).score

    def test_the_input_is_not_mutated(self, stressed):
        before = score_profile(stressed).score
        simulate(stressed, "pay_down_balance", amount=Decimal("5000"))
        assert score_profile(stressed).score == before

    def test_an_unknown_action_is_rejected(self, stressed):
        with pytest.raises(UnknownAction):
            simulate(stressed, "win_the_lottery", amount=Decimal("1"))


class TestRanking:
    def test_actions_come_back_best_first(self, stressed):
        results = rank_actions(
            stressed,
            [
                {"action": "on_time_months", "months": 3},
                {"action": "pay_down_balance", "amount": Decimal("5000")},
                {"action": "close_card", "credit_limit": Decimal("3000"), "is_oldest": True},
            ],
        )
        changes = [r.score_change for r in results]
        assert changes == sorted(changes, reverse=True)

    def test_malformed_candidates_are_skipped_not_fatal(self, stressed):
        results = rank_actions(
            stressed,
            [
                {"action": "pay_down_balance", "amount": Decimal("1000")},
                {"action": "nonsense"},
                {"action": "pay_down_balance"},  # missing required parameter
            ],
        )
        assert len(results) == 1
