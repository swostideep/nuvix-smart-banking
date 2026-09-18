"""Tests for each communications trigger.

The bar every trigger has to clear is not "did it fire?" but "was it worth
interrupting someone for?". Most of these tests assert the *negative* case --
that a trigger stays quiet when the message would be true but useless.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from nuvix.banking.models import RecurringSeries
from nuvix.comms.models import TriggerType
from nuvix.comms.services import triggers
from nuvix.credit.models import ScoreSnapshot
from nuvix.debt.models import Debt
from nuvix.debt.services import planner

pytestmark = pytest.mark.django_db


class TestUtilizationHigh:
    def test_fires_above_the_threshold(self, user, credit_profile):
        context = triggers.evaluate(TriggerType.UTILIZATION_HIGH, user, {"threshold": "0.30"})
        assert context is not None
        assert context["utilization_pct"] == 49
        assert context["points_gain"] > 0

    def test_stays_quiet_below_the_threshold(self, user, credit_profile):
        credit_profile.total_balance = Decimal("500.00")
        credit_profile.save()
        assert triggers.evaluate(TriggerType.UTILIZATION_HIGH, user, {}) is None

    def test_stays_quiet_when_no_paydown_is_needed(self, user, credit_profile):
        """Already inside the target band: there is nothing to ask for."""

        credit_profile.total_credit_limit = Decimal("6500.00")
        credit_profile.total_balance = Decimal("1800.00")  # ~27.7%
        credit_profile.max_single_card_utilization = Decimal("0.2770")
        credit_profile.save()
        assert (
            triggers.evaluate(TriggerType.UTILIZATION_HIGH, user, {"threshold": "0.25"}) is None
        )

    def test_a_small_payment_that_crosses_a_band_edge_is_worth_sending(
        self, user, credit_profile
    ):
        """The most valuable nudge the product has: utilisation is stepped, so
        $85 at the right moment is worth far more than $85 at the wrong one."""

        credit_profile.total_credit_limit = Decimal("6500.00")
        credit_profile.total_balance = Decimal("1970.00")  # just over 30%
        credit_profile.max_single_card_utilization = Decimal("0.3030")
        credit_profile.save()

        context = triggers.evaluate(TriggerType.UTILIZATION_HIGH, user, {"threshold": "0.30"})
        assert context is not None
        assert Decimal(context["paydown_amount"]) < Decimal("100")
        assert context["points_gain"] > 20

    def test_stays_quiet_with_no_revolving_credit(self, user, credit_profile):
        credit_profile.total_credit_limit = Decimal("0")
        credit_profile.save()
        assert triggers.evaluate(TriggerType.UTILIZATION_HIGH, user, {}) is None


class TestPaymentDueSoon:
    def test_fires_for_the_highest_apr_debt_due_in_the_window(self, user):
        target_day = (timezone.localdate() + dt.timedelta(days=4)).day
        Debt.objects.create(
            user=user,
            name="Cheap loan",
            balance=Decimal("5000"),
            apr=Decimal("6"),
            minimum_payment=Decimal("100"),
            due_day=target_day,
        )
        Debt.objects.create(
            user=user,
            name="Expensive card",
            balance=Decimal("1000"),
            apr=Decimal("29"),
            minimum_payment=Decimal("40"),
            due_day=target_day,
        )
        context = triggers.evaluate(TriggerType.PAYMENT_DUE_SOON, user, {"days_ahead": 4})
        assert context["debt_name"] == "Expensive card"

    def test_stays_quiet_when_nothing_is_due(self, user, debts):
        assert triggers.evaluate(TriggerType.PAYMENT_DUE_SOON, user, {"days_ahead": 4}) is None

    def test_ignores_a_cleared_debt(self, user):
        target_day = (timezone.localdate() + dt.timedelta(days=4)).day
        Debt.objects.create(
            user=user,
            name="Paid off",
            balance=Decimal("0"),
            apr=Decimal("29"),
            minimum_payment=Decimal("0"),
            due_day=target_day,
        )
        assert triggers.evaluate(TriggerType.PAYMENT_DUE_SOON, user, {"days_ahead": 4}) is None


class TestProjectedShortfall:
    def _series(self, user, label, amount, cadence, next_on, is_income):
        return RecurringSeries.objects.create(
            user=user,
            merchant_key=label,
            label=label,
            cadence=cadence,
            average_amount=Decimal(amount),
            is_income=is_income,
            occurrences=6,
            confidence=Decimal("0.9"),
            last_seen_on=next_on - dt.timedelta(days=30),
            next_expected_on=next_on,
        )

    def test_fires_when_the_projection_dips_negative(self, user, checking):
        checking.available_balance = Decimal("200.00")
        checking.current_balance = Decimal("200.00")
        checking.save()
        self._series(
            user, "RENT", "1400", "monthly", timezone.localdate() + dt.timedelta(days=3), False
        )
        context = triggers.evaluate(TriggerType.PROJECTED_SHORTFALL, user, {})
        assert context is not None
        assert Decimal(context["shortfall_amount"]) > 0

    def test_stays_quiet_when_the_balance_holds(self, user, checking):
        self._series(
            user, "GYM", "20", "monthly", timezone.localdate() + dt.timedelta(days=3), False
        )
        assert triggers.evaluate(TriggerType.PROJECTED_SHORTFALL, user, {}) is None

    def test_stays_quiet_with_no_detected_series(self, user, checking):
        assert triggers.evaluate(TriggerType.PROJECTED_SHORTFALL, user, {}) is None


class TestScoreChanged:
    def test_fires_on_a_material_rise(self, user):
        ScoreSnapshot.objects.create(
            user=user,
            score=712,
            band="good",
            delta=24,
            captured_on=timezone.localdate(),
        )
        context = triggers.evaluate(TriggerType.SCORE_CHANGED, user, {"min_delta": 8})
        assert context["direction"] == "up"
        assert context["delta"] == 24

    def test_fires_on_a_drop_too(self, user):
        """Reporting only good news makes the number look like marketing."""

        ScoreSnapshot.objects.create(
            user=user,
            score=640,
            band="fair",
            delta=-31,
            captured_on=timezone.localdate(),
        )
        context = triggers.evaluate(TriggerType.SCORE_CHANGED, user, {"min_delta": 8})
        assert context["direction"] == "down"
        assert context["delta"] == 31

    def test_stays_quiet_on_noise(self, user):
        ScoreSnapshot.objects.create(
            user=user, score=700, band="good", delta=2, captured_on=timezone.localdate()
        )
        assert triggers.evaluate(TriggerType.SCORE_CHANGED, user, {"min_delta": 8}) is None

    def test_stays_quiet_with_no_history(self, user):
        assert triggers.evaluate(TriggerType.SCORE_CHANGED, user, {}) is None


class TestPayoffMilestone:
    def test_fires_at_a_quarter_cleared(self, user, debts):
        planner.save_plan(user, "avalanche", Decimal("800"))
        # Clear roughly a third of the book.
        debts[2].balance = Decimal("4000.00")
        debts[2].save()

        context = triggers.evaluate(TriggerType.PAYOFF_MILESTONE, user, {})
        assert context["milestone_pct"] >= 25

    def test_stays_quiet_before_the_first_milestone(self, user, debts):
        planner.save_plan(user, "avalanche", Decimal("800"))
        assert triggers.evaluate(TriggerType.PAYOFF_MILESTONE, user, {}) is None

    def test_stays_quiet_with_no_active_plan(self, user, debts):
        assert triggers.evaluate(TriggerType.PAYOFF_MILESTONE, user, {}) is None


class TestBetterOffer:
    def test_fires_on_a_materially_better_offer(self, user, credit_profile, debts, products):
        credit_profile.score = 760
        credit_profile.save()
        context = triggers.evaluate(
            TriggerType.BETTER_OFFER, user, {"min_benefit": "50", "min_odds": "0.2"}
        )
        assert context is not None
        assert Decimal(context["estimated_benefit"]) >= Decimal("50")

    def test_stays_quiet_when_the_benefit_is_too_small(
        self, user, credit_profile, debts, products
    ):
        credit_profile.score = 760
        credit_profile.save()
        assert (
            triggers.evaluate(TriggerType.BETTER_OFFER, user, {"min_benefit": "999999"}) is None
        )

    def test_stays_quiet_with_no_eligible_products(self, user, credit_profile, debts):
        assert triggers.evaluate(TriggerType.BETTER_OFFER, user, {}) is None


class TestNoPlan:
    def test_fires_with_a_quantified_saving(self, user, debts):
        context = triggers.evaluate(TriggerType.NO_PLAN, user, {})
        assert context is not None
        assert Decimal(context["interest_saved"]) > Decimal("100")
        assert context["months_saved"] > 0

    def test_stays_quiet_once_a_plan_exists(self, user, debts):
        planner.save_plan(user, "optimal", Decimal("800"))
        assert triggers.evaluate(TriggerType.NO_PLAN, user, {}) is None

    def test_stays_quiet_with_no_debts(self, user):
        assert triggers.evaluate(TriggerType.NO_PLAN, user, {}) is None
