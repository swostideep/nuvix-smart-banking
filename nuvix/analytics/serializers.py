from __future__ import annotations

import datetime as dt

from rest_framework import serializers

from nuvix.analytics.models import (
    Channel,
    FunnelEvent,
    MarketingSpend,
    Subscription,
    TouchPoint,
)


class DateWindowSerializer(serializers.Serializer):
    """Shared query-parameter contract for every report.

    Defaults to the last 90 days because a report with no window is the most
    common way to accidentally table-scan a fact table in production.
    """

    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)

    DEFAULT_WINDOW_DAYS = 90
    MAX_WINDOW_DAYS = 1095

    def validate(self, attrs):
        end = attrs.get("end") or dt.date.today() + dt.timedelta(days=1)
        start = attrs.get("start") or end - dt.timedelta(days=self.DEFAULT_WINDOW_DAYS)
        if start >= end:
            raise serializers.ValidationError({"start": "start must be before end."})
        if (end - start).days > self.MAX_WINDOW_DAYS:
            raise serializers.ValidationError(
                {"start": f"Window may not exceed {self.MAX_WINDOW_DAYS} days."}
            )
        attrs["start"], attrs["end"] = start, end
        return attrs


class TouchPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = TouchPoint
        fields = (
            "id",
            "anonymous_id",
            "channel",
            "campaign",
            "utm_source",
            "utm_medium",
            "utm_content",
            "utm_term",
            "landing_path",
            "occurred_at",
        )
        read_only_fields = ("id",)

    def create(self, validated_data):
        request = self.context["request"]
        if request.user.is_authenticated:
            validated_data["user"] = request.user
        return super().create(validated_data)


class FunnelEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = FunnelEvent
        fields = ("id", "anonymous_id", "step", "occurred_at")
        read_only_fields = ("id",)

    def create(self, validated_data):
        request = self.context["request"]
        if request.user.is_authenticated:
            validated_data["user"] = request.user
        return super().create(validated_data)


class MarketingSpendSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketingSpend
        fields = ("id", "channel", "campaign", "spend_on", "amount", "impressions", "clicks")
        read_only_fields = ("id",)


class SubscriptionSerializer(serializers.ModelSerializer):
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = Subscription
        fields = ("id", "plan", "monthly_price", "started_on", "cancelled_on", "is_active")
        read_only_fields = ("id",)


class ChannelPerformanceSerializer(serializers.Serializer):
    channel = serializers.CharField()
    acquired_users = serializers.IntegerField()
    linked_users = serializers.IntegerField()
    subscribed_users = serializers.IntegerField()
    spend = serializers.DecimalField(max_digits=14, decimal_places=2)
    clicks = serializers.IntegerField()
    revenue = serializers.DecimalField(max_digits=14, decimal_places=2)
    cac = serializers.FloatField(allow_null=True)
    activation_rate = serializers.FloatField(allow_null=True)
    subscribe_rate = serializers.FloatField(allow_null=True)
    roas = serializers.FloatField(allow_null=True)


class FunnelStepSerializer(serializers.Serializer):
    step = serializers.CharField()
    #: Users who reached this step or any later one -- the funnel stage.
    users = serializers.IntegerField()
    #: Users with an event recorded for exactly this step. A zero here beside
    #: a non-zero ``users`` means the step is not instrumented.
    users_recorded = serializers.IntegerField()
    step_conversion = serializers.FloatField(allow_null=True)
    overall_conversion = serializers.FloatField(allow_null=True)
    dropped = serializers.IntegerField(allow_null=True)


class AttributionSerializer(serializers.Serializer):
    channel = serializers.CharField()
    first_touch_conversions = serializers.IntegerField()
    last_touch_conversions = serializers.IntegerField()
    linear_conversions = serializers.FloatField()
    total_touches = serializers.IntegerField()
    attribution_gap = serializers.IntegerField()


class ChannelChoiceSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()

    @staticmethod
    def all_channels() -> list[dict]:
        return [{"value": value, "label": label} for value, label in Channel.choices]
