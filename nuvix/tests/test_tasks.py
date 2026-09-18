"""Tests for the scheduled work.

Celery runs eagerly under the test settings, so these exercise the real task
bodies rather than asserting that ``.delay`` was called. The behaviours that
matter are batching, isolation of one user's failure from the rest, and
idempotency -- a nightly job that corrupts state on its second run is worse
than one that never runs.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from nuvix.accounts.models import User
from nuvix.comms.models import (
    Channel,
    CommunicationRule,
    Message,
    MessageStatus,
    MessageTemplate,
    TriggerType,
)
from nuvix.comms.tasks import dispatch_due_messages, run_daily_sweep, sweep_single_user
from nuvix.credit.models import CreditProfile
from nuvix.credit.tasks import refresh_stale_credit_profiles
from nuvix.debt.models import PayoffPlan
from nuvix.debt.services import planner
from nuvix.debt.tasks import recompute_active_plans

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


class TestCreditRefresh:
    def test_a_stale_profile_is_rescored(self, user, credit_profile):
        CreditProfile.objects.filter(pk=credit_profile.pk).update(
            last_scored_at=timezone.now() - dt.timedelta(days=30)
        )
        assert refresh_stale_credit_profiles() == {"rescored": 1, "failed": 0}
        credit_profile.refresh_from_db()
        assert credit_profile.last_scored_at > timezone.now() - dt.timedelta(minutes=1)

    def test_a_fresh_profile_is_left_alone(self, user, credit_profile):
        CreditProfile.objects.filter(pk=credit_profile.pk).update(last_scored_at=timezone.now())
        assert refresh_stale_credit_profiles()["rescored"] == 0

    def test_a_never_scored_profile_is_skipped_not_crashed(self, user, credit_profile):
        """``last_scored_at`` is NULL until the first score; NULL is not stale."""

        CreditProfile.objects.filter(pk=credit_profile.pk).update(last_scored_at=None)
        assert refresh_stale_credit_profiles()["rescored"] == 0

    def test_the_batch_is_capped(self, db):
        for index in range(5):
            account = User.objects.create_user(
                email=f"batch{index}@example.com", password="a-long-enough-password"
            )
            CreditProfile.objects.create(
                user=account, last_scored_at=timezone.now() - dt.timedelta(days=30)
            )
        assert refresh_stale_credit_profiles(limit=2)["rescored"] == 2

    def test_one_failure_does_not_abort_the_batch(self, db, monkeypatch):
        for index in range(3):
            account = User.objects.create_user(
                email=f"iso{index}@example.com", password="a-long-enough-password"
            )
            CreditProfile.objects.create(
                user=account, last_scored_at=timezone.now() - dt.timedelta(days=30)
            )

        calls = {"n": 0}
        from nuvix.credit.services import scoring

        real_rescore = scoring.rescore

        def flaky(user, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("bureau timeout")
            return real_rescore(user, *args, **kwargs)

        monkeypatch.setattr("nuvix.credit.tasks.scoring.rescore", flaky)
        result = refresh_stale_credit_profiles()
        assert result == {"rescored": 2, "failed": 1}


class TestPlanRecompute:
    def test_an_active_plan_is_refreshed_against_current_balances(self, user, debts):
        planner.save_plan(user, "avalanche", Decimal("700"))
        original = PayoffPlan.objects.get(user=user, is_active=True)

        # The user pays a chunk off outside the app.
        debts[1].balance = Decimal("100.00")
        debts[1].save()

        assert recompute_active_plans() == {"refreshed": 1, "failed": 0}
        refreshed = PayoffPlan.objects.get(user=user, is_active=True)
        assert refreshed.pk != original.pk
        assert refreshed.original_balance < original.original_balance

    def test_still_exactly_one_active_plan_afterwards(self, user, debts):
        planner.save_plan(user, "optimal", Decimal("700"))
        recompute_active_plans()
        recompute_active_plans()
        assert PayoffPlan.objects.filter(user=user, is_active=True).count() == 1

    def test_a_plan_that_can_no_longer_be_funded_is_counted_as_failed(self, user, debts):
        """Balances grew; the stored budget no longer covers the minimums."""

        planner.save_plan(user, "avalanche", Decimal("700"))
        for debt in debts:
            debt.minimum_payment = Decimal("500.00")
            debt.save()

        assert recompute_active_plans() == {"refreshed": 0, "failed": 1}
        # The old plan survives rather than the user being left with none.
        assert PayoffPlan.objects.filter(user=user, is_active=True).exists()

    def test_nothing_to_do_is_not_an_error(self, db):
        assert recompute_active_plans() == {"refreshed": 0, "failed": 0}


class TestCommsTasks:
    @pytest.fixture
    def rule(self, db) -> CommunicationRule:
        template = MessageTemplate.objects.create(
            key="util",
            channel=Channel.PUSH,
            subject="Utilisation {utilization_pct}%",
            body="Pay ${paydown_amount} for {points_gain} points.",
        )
        return CommunicationRule.objects.create(
            key="util", trigger=TriggerType.UTILIZATION_HIGH, template=template, priority=90
        )

    def test_sweeping_one_user_schedules_a_message(self, user, credit_profile, rule):
        assert sweep_single_user(str(user.id))["scheduled"] == 1

    def test_sweeping_an_unknown_user_is_a_no_op(self, db, rule):
        import uuid

        assert sweep_single_user(str(uuid.uuid4()))["scheduled"] == 0

    def test_an_inactive_user_is_not_swept(self, user, credit_profile, rule):
        user.is_active = False
        user.save()
        assert sweep_single_user(str(user.id))["scheduled"] == 0

    def test_the_daily_sweep_fans_out_over_active_users(self, user, credit_profile, rule):
        result = run_daily_sweep()
        assert result["users"] == 1
        assert Message.objects.filter(user=user).exists()

    def test_the_daily_sweep_respects_its_batch_size(self, db, rule):
        for index in range(4):
            User.objects.create_user(
                email=f"sweep{index}@example.com", password="a-long-enough-password"
            )
        assert run_daily_sweep(batch_size=2)["users"] == 2

    def test_due_messages_are_dispatched(self, user, credit_profile, rule):
        sweep_single_user(str(user.id))
        Message.objects.update(scheduled_for=timezone.now() - dt.timedelta(minutes=1))
        assert dispatch_due_messages() == {"sent": 1, "failed": 0}
        assert Message.objects.get(user=user).status == MessageStatus.SENT

    def test_dispatching_twice_does_not_resend(self, user, credit_profile, rule):
        sweep_single_user(str(user.id))
        Message.objects.update(scheduled_for=timezone.now() - dt.timedelta(minutes=1))
        dispatch_due_messages()
        assert dispatch_due_messages() == {"sent": 0, "failed": 0}


class TestDeliveryFailure:
    def test_a_provider_failure_marks_the_message_failed(self, user, credit_profile):
        from nuvix.comms.services import channels
        from nuvix.comms.services.channels import DeliveryError
        from nuvix.comms.services.dispatcher import sweep_user

        template = MessageTemplate.objects.create(
            key="util", channel=Channel.PUSH, subject="s", body="b"
        )
        CommunicationRule.objects.create(
            key="util", trigger=TriggerType.UTILIZATION_HIGH, template=template, priority=90
        )
        assert sweep_user(user)["scheduled"] == 1
        # Pull the send time into the past so the dispatcher picks it up
        # regardless of when the suite happens to run relative to quiet hours.
        Message.objects.update(scheduled_for=timezone.now() - dt.timedelta(minutes=1))

        class Broken:
            name = "broken"

            def send(self, message):
                raise DeliveryError("provider is down")

        original = channels.backend_for(Channel.PUSH)
        channels.register_backend(Channel.PUSH, Broken())
        try:
            result = dispatch_due_messages()
        finally:
            channels.register_backend(Channel.PUSH, original)

        assert result == {"sent": 0, "failed": 1}
        assert Message.objects.get(user=user).status == MessageStatus.FAILED
