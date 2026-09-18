from __future__ import annotations

from rest_framework import serializers

from nuvix.marketplace.models import Offer, Partner, Product, ProductType


class PartnerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Partner
        fields = ("id", "name", "slug", "logo_url")


class ProductSerializer(serializers.ModelSerializer):
    partner = PartnerSerializer(read_only=True)
    excluded_state_list = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = Product
        fields = (
            "id",
            "partner",
            "name",
            "product_type",
            "apr_min",
            "apr_max",
            "annual_fee",
            "origination_fee_rate",
            "intro_apr",
            "intro_apr_months",
            "rewards_rate",
            "amount_min",
            "amount_max",
            "term_months_min",
            "term_months_max",
            "min_credit_score",
            "max_dti_ratio",
            "min_annual_income",
            "min_months_employed",
            "excluded_state_list",
            "description",
        )


class MatchedOfferSerializer(serializers.Serializer):
    """An offer as computed by the matcher, before persistence."""

    product = ProductSerializer()
    estimated_apr = serializers.DecimalField(max_digits=6, decimal_places=2)
    estimated_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    estimated_term_months = serializers.IntegerField()
    approval_odds = serializers.DecimalField(max_digits=4, decimal_places=3)
    estimated_benefit = serializers.DecimalField(max_digits=12, decimal_places=2)
    rank_score = serializers.DecimalField(max_digits=12, decimal_places=4)
    reasons = serializers.ListField(child=serializers.CharField())


class OfferSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)

    class Meta:
        model = Offer
        fields = (
            "id",
            "product",
            "estimated_apr",
            "estimated_amount",
            "estimated_term_months",
            "approval_odds",
            "estimated_benefit",
            "rank_score",
            "reasons",
            "is_prequalified",
            "clicked_at",
            "created_at",
        )
        read_only_fields = fields


class MatchRequestSerializer(serializers.Serializer):
    product_type = serializers.ChoiceField(
        choices=ProductType.choices, required=False, allow_null=True
    )
    limit = serializers.IntegerField(default=10, min_value=1, max_value=50)
    persist = serializers.BooleanField(default=False)
