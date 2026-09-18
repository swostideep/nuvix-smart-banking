from __future__ import annotations

from rest_framework import serializers

from nuvix.banking.models import Institution, LinkedAccount, RecurringSeries, Transaction


class InstitutionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Institution
        fields = ("id", "name", "slug", "logo_url")


class LinkedAccountSerializer(serializers.ModelSerializer):
    institution_name = serializers.CharField(source="institution.name", read_only=True)
    utilization = serializers.DecimalField(max_digits=6, decimal_places=4, read_only=True)

    class Meta:
        model = LinkedAccount
        fields = (
            "id",
            "institution",
            "institution_name",
            "name",
            "account_type",
            "mask",
            "current_balance",
            "available_balance",
            "credit_limit",
            "apr",
            "minimum_payment",
            "statement_due_day",
            "is_primary_checking",
            "utilization",
            "last_synced_at",
            "created_at",
        )
        read_only_fields = ("id", "last_synced_at", "created_at")

    def validate(self, attrs):
        account_type = attrs.get("account_type", getattr(self.instance, "account_type", None))
        if account_type == "credit_card" and not attrs.get(
            "credit_limit", getattr(self.instance, "credit_limit", None)
        ):
            raise serializers.ValidationError(
                {"credit_limit": "A credit card requires a credit limit."}
            )
        return attrs

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = (
            "id",
            "account",
            "posted_on",
            "amount",
            "merchant",
            "merchant_key",
            "category",
            "is_pending",
            "external_id",
        )
        read_only_fields = ("id", "merchant_key")

    def validate_account(self, account: LinkedAccount) -> LinkedAccount:
        if account.user_id != self.context["request"].user.id:
            raise serializers.ValidationError("Unknown account.")
        return account

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class RecurringSeriesSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecurringSeries
        fields = (
            "id",
            "label",
            "merchant_key",
            "category",
            "cadence",
            "average_amount",
            "is_income",
            "occurrences",
            "confidence",
            "last_seen_on",
            "next_expected_on",
        )


class CashflowEventSerializer(serializers.Serializer):
    date = serializers.DateField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    label = serializers.CharField()
    is_income = serializers.BooleanField()


class CashflowProjectionSerializer(serializers.Serializer):
    opening_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    closing_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    horizon_days = serializers.IntegerField()
    safe_to_spend = serializers.DecimalField(max_digits=14, decimal_places=2)
    lowest_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    lowest_balance_on = serializers.DateField(allow_null=True)
    next_income_on = serializers.DateField(allow_null=True)
    next_income_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_inflow = serializers.DecimalField(max_digits=14, decimal_places=2)
    total_outflow = serializers.DecimalField(max_digits=14, decimal_places=2)
    shortfall_risk = serializers.BooleanField()
    events = CashflowEventSerializer(many=True)
