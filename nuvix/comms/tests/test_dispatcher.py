"""Tests for the communications sweep."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from nuvix.comms.models import (
    Channel,
    CommunicationRule,
    Message,
    MessageStatus,
    MessageTemplate,
    TriggerType,
)
from nuvix.comms.services import triggers
from nuvix.comms.services.dispatcher import (
    dedup_key,
    dispatch_due,
    next_send_time,
    sweep_user,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def utilisation_rule(db) -> CommunicationRule:
    template = MessageTemplate.objects.create(
        key="utilisation-high",
        channel=Channel.PUSH,
        subject="Your utilisation is {utilization_pct}%",
        body="Hi {first_name}, paying ${paydown_amount} could add {points_gain} points.",
    )
    return CommunicationRule.objects.create(
        key="utilisation-high",
        trigger=TriggerType.UTILIZATION_HIGH,
        template=template,
        priority=80,
        cooldown_days=14,
        max_per_week=1,
        parameters={"threshold": "0.30"},
    )


@pytest.fixture
def low_priority_rule(db) -> CommunicationRule:
    template = MessageTemplate.objects.create(
        key="no-plan",
        channel=Channel.EMAIL,
        subject="Build a payoff plan",
        body="You could save ${interest_saved}.",
    )
    return CommunicationRule.objects.create(
        key="no-plan",
        trigger=TriggerType.NO_PLAN,
        template=template,
        priority=10,
        cooldown_days=30,
        max_per_week=1,
    )


class TestTemplateRendering:
    def test_placeholders_are_filled(self):
        template = MessageTemplate(subject="Hi {name}", body="You saved ${amount}")
        subject, body = template.render({"name": "Ada", "amount": "42.00"})
        assert subject == "Hi Ada"
        assert body == "You saved $42.00"

    def test_a_missing_placeholder_does_not_break_the_sweep(self):
        """One bad template must not take down everyone's messages."""

        template = MessageTemplate(subject="Hi {name}", body="Missing {nope}")
        subject, body = template.render({"name": "Ada"})
        assert subject == "Hi Ada"
        assert body == "Missing {nope}"


class TestDedupKey:
    def test_the_same_facts_give_the_same_key(self, user, utilisation_rule):
        context = {"a": 1, "b": 2}
        assert dedup_key(user, utilisation_rule, context) == dedup_key(
            user, utilisation_rule, context
        )

    def test_key_order_does_not_matter(self, user, utilisation_rule):
        assert dedup_key(user, utilisation_rule, {"a": 1, "b": 2}) == dedup_key(
            user, utilisation_rule, {"b": 2, "a": 1}
        )

    def test_different_facts_give_different_keys(self, user, utilisation_rule):
        assert dedup_key(user, utilisation_rule, {"a": 1}) != dedup_key(
            user, utilisation_rule, {"a": 2}
        )

    def test_different_users_give_different_keys(self, user, other_user, utilisation_rule):
        assert dedup_key(user, utilisation_rule, {"a": 1}) != dedup_key(
            other_user, utilisation_rule, {"a": 1}
        )


class TestSweep:
    def test_a_triggered_rule_schedules_a_message(self, user, credit_profile, utilisation_rule):
        stats = sweep_user(user)
        assert stats["scheduled"] == 1
        message = Message.objects.get(user=user)
        assert message.status == MessageStatus.SCHEDULED
        assert "Ada" in message.body

    def test_nothing_is_sent_when_no_trigger_fires(
        self, user, credit_profile, utilisation_rule
    ):
        credit_profile.total_balance = Decimal("50.00")
        credit_profile.save()
        assert sweep_user(user)["scheduled"] == 0
        assert Message.objects.count() == 0

    def test_the_sweep_is_idempotent(self, user, credit_profile, utilisation_rule):
        """Running twice in a day must not double-send."""

        sweep_user(user)
        sweep_user(user)
        assert Message.objects.filter(user=user).count() == 1

    def test_an_inactive_rule_is_skipped(self, user, credit_profile, utilisation_rule):
        utilisation_rule.is_active = False
        utilisation_rule.save()
        assert sweep_user(user)["scheduled"] == 0

    def test_an_inactive_template_is_skipped(self, user, credit_profile, utilisation_rule):
        utilisation_rule.template.is_active = False
        utilisation_rule.template.save()
        assert sweep_user(user)["scheduled"] == 0

    def test_a_cooldown_suppresses_and_records_the_reason(
        self, user, credit_profile, utilisation_rule
    ):
        sweep_user(user)
        sent = Message.objects.get(user=user)
        sent.status = MessageStatus.SENT
        sent.sent_at = timezone.now()
        sent.save()

        # Change the facts so dedup does not mask the cooldown.
        credit_profile.total_balance = Decimal("5200.00")
        credit_profile.save()

        sweep_user(user)
        suppressed = Message.objects.filter(status=MessageStatus.SUPPRESSED)
        assert suppressed.count() == 1
        assert suppressed.first().suppression_reason == "cooldown"

    def test_the_weekly_budget_caps_total_sends(self, user, credit_profile, debts, settings):
        """Priority decides who survives the cap."""

        settings.NUVIX = {**settings.NUVIX, "COMMS_MAX_PER_WEEK": 0}
        template = MessageTemplate.objects.create(
            key="t", channel=Channel.PUSH, subject="s", body="b"
        )
        CommunicationRule.objects.create(
            key="r", trigger=TriggerType.UTILIZATION_HIGH, template=template, priority=50
        )
        stats = sweep_user(user)
        assert stats["scheduled"] == 0
        assert Message.objects.filter(suppression_reason="user_weekly_cap").exists()

    def test_higher_priority_wins_the_last_slot(
        self, user, credit_profile, debts, utilisation_rule, low_priority_rule, settings
    ):
        settings.NUVIX = {**settings.NUVIX, "COMMS_MAX_PER_WEEK": 1}
        sweep_user(user)
        scheduled = Message.objects.filter(status=MessageStatus.SCHEDULED)
        assert scheduled.count() == 1
        assert scheduled.first().rule.key == "utilisation-high"

    def test_suppressed_messages_are_still_recorded(
        self, user, credit_profile, debts, utilisation_rule, low_priority_rule, settings
    ):
        settings.NUVIX = {**settings.NUVIX, "COMMS_MAX_PER_WEEK": 1}
        sweep_user(user)
        assert Message.objects.filter(status=MessageStatus.SUPPRESSED).exists()


class TestQuietHours:
    def test_a_daytime_send_is_immediate(self, settings):
        settings.NUVIX = {**settings.NUVIX, "COMMS_QUIET_HOURS": (21, 8)}
        noon = timezone.make_aware(dt.datetime(2026, 3, 2, 12, 0))
        assert next_send_time(noon) == noon

    def test_a_late_night_send_is_pushed_to_morning(self, settings):
        settings.NUVIX = {**settings.NUVIX, "COMMS_QUIET_HOURS": (21, 8)}
        late = timezone.make_aware(dt.datetime(2026, 3, 2, 23, 30))
        pushed = next_send_time(late)
        assert timezone.localtime(pushed).hour == 8
        assert timezone.localtime(pushed).date() == dt.date(2026, 3, 3)

    def test_an_early_morning_send_waits_for_the_same_day(self, settings):
        settings.NUVIX = {**settings.NUVIX, "COMMS_QUIET_HOURS": (21, 8)}
        early = timezone.make_aware(dt.datetime(2026, 3, 2, 3, 0))
        pushed = next_send_time(early)
        assert timezone.localtime(pushed).hour == 8
        assert timezone.localtime(pushed).date() == dt.date(2026, 3, 2)


class TestDelivery:
    def test_due_messages_are_sent(self, user, credit_profile, utilisation_rule):
        sweep_user(user)
        result = dispatch_due(now=timezone.now() + dt.timedelta(days=1))
        assert result["sent"] == 1
        assert Message.objects.get(user=user).status == MessageStatus.SENT

    def test_future_messages_are_left_alone(self, user, credit_profile, utilisation_rule):
        sweep_user(user)
        Message.objects.update(scheduled_for=timezone.now() + dt.timedelta(days=5))
        assert dispatch_due(now=timezone.now())["sent"] == 0

    def test_suppressed_messages_are_never_delivered(
        self, user, credit_profile, debts, utilisation_rule, settings
    ):
        settings.NUVIX = {**settings.NUVIX, "COMMS_MAX_PER_WEEK": 0}
        sweep_user(user)
        assert dispatch_due(now=timezone.now() + dt.timedelta(days=1))["sent"] == 0


class TestTriggerIsolation:
    def test_a_failing_trigger_returns_nothing_instead_of_raising(self, user, monkeypatch):
        def explode(user, parameters):
            raise RuntimeError("bureau is down")

        monkeypatch.setitem(triggers._REGISTRY, "boom", explode)
        assert triggers.evaluate("boom", user, {}) is None

    def test_an_unknown_trigger_is_ignored(self, user):
        assert triggers.evaluate("does-not-exist", user, {}) is None

    def test_every_registered_trigger_has_a_choice(self):
        registered = set(triggers.available_triggers())
        declared = {value for value, _ in TriggerType.choices}
        assert registered <= declared
