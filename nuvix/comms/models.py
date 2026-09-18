"""Templates, rules and the messages they produce."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from nuvix.core.models import TimeStampedModel


class Channel(models.TextChoices):
    PUSH = "push", "Push notification"
    EMAIL = "email", "Email"
    SMS = "sms", "SMS"
    IN_APP = "in_app", "In-app"


class MessageTemplate(TimeStampedModel):
    """Copy, separated from the logic that decides when to send it.

    Wording changes weekly; trigger logic changes rarely. Splitting them means
    a copy fix is a row update rather than a deploy, and the same trigger can
    render differently per channel.
    """

    key = models.SlugField(max_length=80, unique=True)
    channel = models.CharField(max_length=20, choices=Channel.choices)
    subject = models.CharField(max_length=200, blank=True)
    #: ``str.format`` placeholders, filled from the trigger's context.
    body = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "comms_message_template"

    def __str__(self) -> str:
        return f"{self.key} ({self.channel})"

    def render(self, context: dict) -> tuple[str, str]:
        """Render subject and body, tolerating a missing placeholder.

        A ``KeyError`` mid-render would take down the whole nightly sweep for
        every user because one template referenced a field one trigger does
        not emit. An unrendered placeholder is visible, contained and fixable;
        a failed sweep is not.
        """

        safe = _ForgivingContext(context)
        return (
            self.subject.format_map(safe),
            self.body.format_map(safe),
        )


class _ForgivingContext(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class TriggerType(models.TextChoices):
    UTILIZATION_HIGH = "utilization_high", "Credit utilisation is high"
    PAYMENT_DUE_SOON = "payment_due_soon", "A payment is due soon"
    PROJECTED_SHORTFALL = "projected_shortfall", "Projected balance goes negative"
    SCORE_CHANGED = "score_changed", "Credit score moved"
    PAYOFF_MILESTONE = "payoff_milestone", "Debt payoff milestone reached"
    BETTER_OFFER = "better_offer", "A materially better offer is available"
    NO_PLAN = "no_plan", "No active payoff plan"


class CommunicationRule(TimeStampedModel):
    """When a template fires, and how often it is allowed to.

    Cooldown and weekly cap live on the rule rather than in code so that
    frequency can be tuned without a deploy -- which is what actually happens
    when a message turns out to annoy people.
    """

    key = models.SlugField(max_length=80, unique=True)
    trigger = models.CharField(max_length=40, choices=TriggerType.choices)
    template = models.ForeignKey(
        MessageTemplate, on_delete=models.PROTECT, related_name="rules"
    )
    #: Higher wins when the weekly cap forces a choice.
    priority = models.PositiveSmallIntegerField(default=50)
    cooldown_days = models.PositiveSmallIntegerField(default=7)
    max_per_week = models.PositiveSmallIntegerField(default=2)
    is_active = models.BooleanField(default=True)
    #: Trigger-specific tuning, e.g. ``{"threshold": 0.3}``.
    parameters = models.JSONField(default=dict, blank=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "comms_rule"
        ordering = ("-priority",)

    def __str__(self) -> str:
        return f"{self.key} (p{self.priority})"


class MessageStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    SENT = "sent", "Sent"
    SUPPRESSED = "suppressed", "Suppressed"
    FAILED = "failed", "Failed"


class Message(TimeStampedModel):
    """One outbound communication, including the ones deliberately withheld.

    Suppressed messages are rows too. Knowing what NuviX chose *not* to send,
    and why, is the only way to tell an over-eager rule from a quiet week.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="messages"
    )
    rule = models.ForeignKey(
        CommunicationRule, on_delete=models.SET_NULL, null=True, related_name="messages"
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    subject = models.CharField(max_length=200, blank=True)
    body = models.TextField()

    status = models.CharField(
        max_length=20, choices=MessageStatus.choices, default=MessageStatus.SCHEDULED
    )
    suppression_reason = models.CharField(max_length=60, blank=True)

    scheduled_for = models.DateTimeField(db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    #: Stable hash of (user, rule, salient facts). The uniqueness constraint
    #: on it is what makes the sweep idempotent: running it twice in a day
    #: cannot produce two copies of the same nudge.
    dedup_key = models.CharField(max_length=120)
    context = models.JSONField(default=dict)

    class Meta(TimeStampedModel.Meta):
        db_table = "comms_message"
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status", "scheduled_for"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["dedup_key"], name="uniq_message_dedup_key")
        ]

    def __str__(self) -> str:
        return f"{self.channel}:{self.status} {self.subject[:40]}"
