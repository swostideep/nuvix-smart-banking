from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from nuvix.debt.models import Debt, PaymentRecord, PayoffPlan, PayoffStrategy


class DebtSerializer(serializers.ModelSerializer):
    utilization = serializers.SerializerMethodField()

    class Meta:
        model = Debt
        fields = (
            "id",
            "name",
            "kind",
            "balance",
            "apr",
            "minimum_payment",
            "minimum_payment_rate",
            "promo_apr",
            "promo_months_remaining",
            "due_day",
            "credit_limit",
            "linked_account",
            "utilization",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def get_utilization(self, obj: Debt) -> str | None:
        if not obj.credit_limit:
            return None
        return str((obj.balance / obj.credit_limit).quantize(Decimal("0.0001")))

    def validate(self, attrs):
        minimum = attrs.get("minimum_payment", getattr(self.instance, "minimum_payment", None))
        balance = attrs.get("balance", getattr(self.instance, "balance", None))
        if minimum is not None and balance is not None and minimum > balance and balance > 0:
            raise serializers.ValidationError(
                {"minimum_payment": "Minimum payment cannot exceed the balance."}
            )
        promo_apr = attrs.get("promo_apr", getattr(self.instance, "promo_apr", None))
        promo_months = attrs.get(
            "promo_months_remaining", getattr(self.instance, "promo_months_remaining", 0)
        )
        if promo_apr is not None and not promo_months:
            raise serializers.ValidationError(
                {"promo_months_remaining": "A promotional rate needs a remaining term."}
            )
        return attrs

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class PaymentRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentRecord
        fields = ("id", "debt", "paid_on", "amount", "balance_after", "created_at")
        read_only_fields = ("id", "created_at")

    def validate_debt(self, debt: Debt) -> Debt:
        if debt.user_id != self.context["request"].user.id:
            raise serializers.ValidationError("Unknown debt.")
        return debt

    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)


class PlanRequestSerializer(serializers.Serializer):
    """Input for building or simulating a plan."""

    strategy = serializers.ChoiceField(
        choices=PayoffStrategy.choices, default=PayoffStrategy.OPTIMAL
    )
    monthly_budget = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True, min_value=Decimal("0")
    )
    include_schedule = serializers.BooleanField(default=False)


class DebtOutcomeSerializer(serializers.Serializer):
    key = serializers.CharField()
    name = serializers.CharField()
    original_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    interest_paid = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_paid = serializers.DecimalField(max_digits=12, decimal_places=2)
    months_to_payoff = serializers.IntegerField()
    payoff_date = serializers.DateField()


class DebtMonthSerializer(serializers.Serializer):
    key = serializers.CharField()
    opening_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    interest_charged = serializers.DecimalField(max_digits=12, decimal_places=2)
    payment = serializers.DecimalField(max_digits=12, decimal_places=2)
    closing_balance = serializers.DecimalField(max_digits=12, decimal_places=2)


class MonthSnapshotSerializer(serializers.Serializer):
    month_index = serializers.IntegerField()
    date = serializers.DateField()
    total_payment = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_interest = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    lines = DebtMonthSerializer(many=True)


class PayoffResultSerializer(serializers.Serializer):
    strategy = serializers.CharField()
    monthly_budget = serializers.DecimalField(max_digits=12, decimal_places=2)
    months_to_debt_free = serializers.IntegerField()
    debt_free_date = serializers.DateField()
    total_interest = serializers.DecimalField(max_digits=14, decimal_places=2)
    total_paid = serializers.DecimalField(max_digits=14, decimal_places=2)
    original_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    order = serializers.ListField(child=serializers.CharField())
    outcomes = DebtOutcomeSerializer(many=True)
    truncated = serializers.BooleanField()
    negative_amortisation = serializers.BooleanField()
    schedule = MonthSnapshotSerializer(many=True, required=False)


class PayoffPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayoffPlan
        fields = (
            "id",
            "strategy",
            "monthly_budget",
            "months_to_debt_free",
            "debt_free_date",
            "total_interest",
            "total_paid",
            "original_balance",
            "interest_saved_vs_minimums",
            "months_saved_vs_minimums",
            "summary",
            "is_active",
            "created_at",
        )
        read_only_fields = fields


class TargetDateRequestSerializer(serializers.Serializer):
    target_months = serializers.IntegerField(min_value=1, max_value=600)
    strategy = serializers.ChoiceField(
        choices=PayoffStrategy.choices, default=PayoffStrategy.AVALANCHE
    )
