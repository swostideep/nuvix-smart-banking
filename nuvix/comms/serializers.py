from __future__ import annotations

from rest_framework import serializers

from nuvix.comms.models import CommunicationRule, Message, MessageTemplate


class MessageTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = MessageTemplate
        fields = ("id", "key", "channel", "subject", "body", "is_active")


class CommunicationRuleSerializer(serializers.ModelSerializer):
    template = MessageTemplateSerializer(read_only=True)

    class Meta:
        model = CommunicationRule
        fields = (
            "id",
            "key",
            "trigger",
            "template",
            "priority",
            "cooldown_days",
            "max_per_week",
            "is_active",
            "parameters",
        )


class MessageSerializer(serializers.ModelSerializer):
    rule_key = serializers.CharField(source="rule.key", read_only=True, default=None)

    class Meta:
        model = Message
        fields = (
            "id",
            "rule_key",
            "channel",
            "subject",
            "body",
            "status",
            "suppression_reason",
            "scheduled_for",
            "sent_at",
            "context",
            "created_at",
        )
        read_only_fields = fields


class SweepResultSerializer(serializers.Serializer):
    evaluated = serializers.IntegerField()
    scheduled = serializers.IntegerField()
    suppressed = serializers.IntegerField()
