"""Marketing, subscription and funnel facts.

These are the tables the SQL in :mod:`nuvix.analytics.queries` reads. They are
modelled as an append-only event log plus two small dimension tables, which is
what makes attribution answerable: you cannot recompute first-touch versus
last-touch from a single "channel" column on the user row.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from nuvix.core.models import TimeStampedModel
from nuvix.core.money import ZERO


class Channel(models.TextChoices):
    PAID_SEARCH = "paid_search", "Paid search"
    PAID_SOCIAL = "paid_social", "Paid social"
    ORGANIC_SEARCH = "organic_search", "Organic search"
    REFERRAL = "referral", "Referral"
    AFFILIATE = "affiliate", "Affiliate"
    EMAIL = "email", "Email"
    DIRECT = "direct", "Direct"
    CONTENT = "content", "Content"


class TouchPoint(TimeStampedModel):
    """One marketing interaction.

    Anonymous touches are captured against ``anonymous_id`` and stitched to a
    user at signup, so the pre-signup half of the journey -- which is most of
    it -- is not lost.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="touch_points",
    )
    anonymous_id = models.CharField(max_length=64, db_index=True, blank=True)

    channel = models.CharField(max_length=30, choices=Channel.choices, db_index=True)
    campaign = models.CharField(max_length=120, blank=True, db_index=True)
    utm_source = models.CharField(max_length=80, blank=True)
    utm_medium = models.CharField(max_length=80, blank=True)
    utm_content = models.CharField(max_length=120, blank=True)
    utm_term = models.CharField(max_length=120, blank=True)
    landing_path = models.CharField(max_length=200, blank=True)

    occurred_at = models.DateTimeField(db_index=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "analytics_touch_point"
        indexes = [
            models.Index(fields=["user", "occurred_at"]),
            models.Index(fields=["channel", "occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.channel}/{self.campaign} @ {self.occurred_at:%Y-%m-%d}"


class MarketingSpend(TimeStampedModel):
    """Daily spend by channel and campaign -- the denominator of every CAC."""

    channel = models.CharField(max_length=30, choices=Channel.choices, db_index=True)
    campaign = models.CharField(max_length=120, blank=True)
    spend_on = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    impressions = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)

    class Meta(TimeStampedModel.Meta):
        db_table = "analytics_marketing_spend"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "campaign", "spend_on"], name="uniq_spend_per_day"
            )
        ]
        indexes = [models.Index(fields=["spend_on", "channel"])]

    def __str__(self) -> str:
        return f"{self.spend_on} {self.channel} {self.amount}"


class FunnelStep(models.TextChoices):
    VISIT = "visit", "Landing page visit"
    SIGNUP = "signup", "Account created"
    PROFILE_COMPLETE = "profile_complete", "Profile completed"
    ACCOUNT_LINKED = "account_linked", "Bank account linked"
    PLAN_CREATED = "plan_created", "Payoff plan created"
    SUBSCRIBED = "subscribed", "Subscription started"


class FunnelEvent(TimeStampedModel):
    """A step reached in the activation funnel."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="funnel_events",
    )
    anonymous_id = models.CharField(max_length=64, blank=True, db_index=True)
    step = models.CharField(max_length=30, choices=FunnelStep.choices, db_index=True)
    occurred_at = models.DateTimeField(db_index=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "analytics_funnel_event"
        indexes = [models.Index(fields=["step", "occurred_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "step"],
                condition=models.Q(user__isnull=False),
                name="uniq_funnel_step_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.step} @ {self.occurred_at:%Y-%m-%d}"


class Subscription(TimeStampedModel):
    """The subscription half of the revenue model."""

    class Plan(models.TextChoices):
        STARTER = "starter", "Starter"
        PLUS = "plus", "Plus"
        PREMIUM = "premium", "Premium"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions"
    )
    plan = models.CharField(max_length=20, choices=Plan.choices)
    monthly_price = models.DecimalField(max_digits=8, decimal_places=2)
    started_on = models.DateField(db_index=True)
    cancelled_on = models.DateField(null=True, blank=True, db_index=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "analytics_subscription"
        indexes = [models.Index(fields=["user", "started_on"])]

    def __str__(self) -> str:
        return f"{self.plan} from {self.started_on}"

    @property
    def is_active(self) -> bool:
        return self.cancelled_on is None


class MarketplaceRevenue(TimeStampedModel):
    """The marketplace half: a payout earned when a user funds with a partner."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="marketplace_revenue"
    )
    offer = models.ForeignKey(
        "marketplace.Offer", on_delete=models.SET_NULL, null=True, blank=True
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    earned_on = models.DateField(db_index=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "analytics_marketplace_revenue"

    def __str__(self) -> str:
        return f"{self.amount} on {self.earned_on}"
