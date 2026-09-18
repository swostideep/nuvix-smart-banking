from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from nuvix.credit.engine.simulator import ACTIONS
from nuvix.credit.models import CreditProfile, RiskAssessment, ScoreSnapshot


class CreditProfileSerializer(serializers.ModelSerializer):
    utilization = serializers.DecimalField(max_digits=6, decimal_places=4, read_only=True)

    class Meta:
        model = CreditProfile
        fields = (
            "on_time_payment_rate",
            "late_payments_30d",
            "late_payments_90d",
            "derogatory_marks",
            "total_balance",
            "total_credit_limit",
            "max_single_card_utilization",
            "oldest_account_months",
            "average_account_age_months",
            "credit_mix_types",
            "open_accounts",
            "hard_inquiries_12m",
            "accounts_opened_24m",
            "score",
            "band",
            "factors",
            "utilization",
            "last_scored_at",
            "bureau",
        )
        read_only_fields = ("score", "band", "factors", "last_scored_at", "bureau")


class FactorSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    weight = serializers.DecimalField(max_digits=4, decimal_places=2)
    sub_score = serializers.DecimalField(max_digits=4, decimal_places=3)
    points_earned = serializers.IntegerField()
    points_available = serializers.IntegerField()
    status = serializers.CharField()
    reason = serializers.CharField()


class ScoreBreakdownSerializer(serializers.Serializer):
    score = serializers.IntegerField()
    band = serializers.CharField()
    factors = FactorSerializer(many=True)


class ScoreSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScoreSnapshot
        fields = ("captured_on", "score", "band", "delta", "utilization")


class SimulationRequestSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=sorted(ACTIONS))
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0")
    )
    credit_limit = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0")
    )
    months = serializers.IntegerField(required=False, min_value=0, max_value=120)
    is_oldest = serializers.BooleanField(required=False, default=False)

    #: Which parameters each action requires -- enforced here so the engine
    #: never has to defend itself against a malformed request.
    REQUIRED = {
        "pay_down_balance": {"amount"},
        "open_new_card": {"credit_limit"},
        "close_card": {"credit_limit"},
        "on_time_months": {"months"},
        "increase_limit": {"amount"},
    }

    def validate(self, attrs):
        missing = self.REQUIRED[attrs["action"]] - {
            key for key, value in attrs.items() if value is not None
        }
        if missing:
            raise serializers.ValidationError(
                {key: "This field is required for this action." for key in missing}
            )
        return attrs

    def engine_kwargs(self) -> dict:
        data = dict(self.validated_data)
        action = data.pop("action")
        allowed = self.REQUIRED[action] | ({"is_oldest"} if action == "close_card" else set())
        return action, {k: v for k, v in data.items() if k in allowed}


class FactorDeltaSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    points_before = serializers.IntegerField()
    points_after = serializers.IntegerField()
    change = serializers.IntegerField()


class SimulationResultSerializer(serializers.Serializer):
    action = serializers.CharField()
    score_before = serializers.IntegerField()
    score_after = serializers.IntegerField()
    score_change = serializers.IntegerField()
    band_before = serializers.CharField()
    band_after = serializers.CharField()
    explanation = serializers.CharField()
    factor_deltas = FactorDeltaSerializer(many=True)


class RiskRequestSerializer(serializers.Serializer):
    loan_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("100")
    )
    interest_rate = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("60")
    )
    loan_term_months = serializers.IntegerField(min_value=3, max_value=360)
    loan_purpose = serializers.ChoiceField(
        choices=[
            "business",
            "home",
            "education",
            "auto",
            "debt_consolidation",
            "medical",
            "other",
        ],
        default="other",
    )


class RiskAssessmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = RiskAssessment
        fields = (
            "id",
            "model_version",
            "model_kind",
            "probability_of_default",
            "risk_grade",
            "decision",
            "requested_amount",
            "reason_codes",
            "created_at",
        )
        read_only_fields = fields
