"""Partner product catalogue and the offers matched to users."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from nuvix.core.models import TimeStampedModel
from nuvix.core.money import ZERO


class Partner(TimeStampedModel):
    """A lender or issuer whose products NuviX surfaces."""

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=120, unique=True)
    logo_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)

    #: What NuviX is paid when a user funds or opens through this partner.
    #: Stored because it is real, and kept strictly out of the ranking score
    #: -- see ``nuvix.marketplace.services.matching`` for why.
    payout_per_funded = models.DecimalField(max_digits=10, decimal_places=2, default=ZERO)

    class Meta(TimeStampedModel.Meta):
        db_table = "marketplace_partner"

    def __str__(self) -> str:
        return self.name


class ProductType(models.TextChoices):
    PERSONAL_LOAN = "personal_loan", "Personal loan"
    DEBT_CONSOLIDATION = "debt_consolidation", "Debt consolidation loan"
    CREDIT_CARD = "credit_card", "Credit card"
    SECURED_CARD = "secured_card", "Secured credit card"
    CREDIT_BUILDER = "credit_builder", "Credit builder loan"


class Product(TimeStampedModel):
    """One offerable product and its underwriting box.

    Eligibility criteria are columns rather than a rules blob so that the
    catalogue can be filtered in SQL. Matching a few hundred products per
    request in Python would work today and stop working at scale.
    """

    partner = models.ForeignKey(Partner, on_delete=models.CASCADE, related_name="products")
    name = models.CharField(max_length=140)
    product_type = models.CharField(max_length=30, choices=ProductType.choices)
    is_active = models.BooleanField(default=True)

    # --- pricing -----------------------------------------------------------
    apr_min = models.DecimalField(max_digits=6, decimal_places=2)
    apr_max = models.DecimalField(max_digits=6, decimal_places=2)
    annual_fee = models.DecimalField(max_digits=8, decimal_places=2, default=ZERO)
    origination_fee_rate = models.DecimalField(
        max_digits=6, decimal_places=4, default=ZERO, help_text="Fraction of principal"
    )
    intro_apr = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    intro_apr_months = models.PositiveSmallIntegerField(default=0)
    rewards_rate = models.DecimalField(
        max_digits=6, decimal_places=4, default=ZERO, help_text="Fraction of spend"
    )

    # --- amounts and terms -------------------------------------------------
    amount_min = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    amount_max = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    term_months_min = models.PositiveSmallIntegerField(default=12)
    term_months_max = models.PositiveSmallIntegerField(default=60)

    # --- underwriting box --------------------------------------------------
    min_credit_score = models.PositiveSmallIntegerField(default=300)
    max_dti_ratio = models.DecimalField(max_digits=5, decimal_places=3, default=1)
    min_annual_income = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    min_months_employed = models.PositiveSmallIntegerField(default=0)
    #: Comma-delimited state codes with a leading and trailing comma
    #: (``",CA,NY,"``). A JSONField would be the natural choice, but its
    #: ``contains`` lookup is unsupported on SQLite, which would push the
    #: exclusion out of the database and into a Python loop over the whole
    #: catalogue. This encoding keeps the filter in SQL on every backend.
    excluded_states = models.CharField(max_length=255, blank=True, default="")

    #: Share of applicants inside the box that this partner actually approves.
    #: Sourced from partner-reported funnel data; the honest prior when a
    #: partner reports nothing is well below 1.
    historical_approval_rate = models.DecimalField(
        max_digits=4, decimal_places=3, default="0.600"
    )

    description = models.TextField(blank=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "marketplace_product"
        indexes = [
            models.Index(fields=["product_type", "is_active"]),
            models.Index(fields=["min_credit_score"]),
        ]

    def __str__(self) -> str:
        return f"{self.partner.name} {self.name}"

    @staticmethod
    def encode_states(states: list[str]) -> str:
        """Encode state codes for :attr:`excluded_states`."""

        cleaned = [s.strip().upper() for s in states if s and s.strip()]
        return f",{','.join(cleaned)}," if cleaned else ""

    @property
    def excluded_state_list(self) -> list[str]:
        return [s for s in self.excluded_states.split(",") if s]

    @property
    def is_revolving(self) -> bool:
        return self.product_type in {ProductType.CREDIT_CARD, ProductType.SECURED_CARD}


class Offer(TimeStampedModel):
    """A product matched to a user at a point in time.

    Persisted because an offer shown to a customer is a representation: if the
    rate they were quoted differs from the rate they are given, NuviX needs to
    be able to show exactly what was displayed and why.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="offers"
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="offers")

    estimated_apr = models.DecimalField(max_digits=6, decimal_places=2)
    estimated_amount = models.DecimalField(max_digits=12, decimal_places=2)
    estimated_term_months = models.PositiveSmallIntegerField()
    approval_odds = models.DecimalField(max_digits=4, decimal_places=3)
    #: Modelled dollar benefit to the *user* over the product's life.
    estimated_benefit = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    rank_score = models.DecimalField(max_digits=12, decimal_places=4, default=ZERO)
    reasons = models.JSONField(default=list)

    is_prequalified = models.BooleanField(default=False)
    clicked_at = models.DateTimeField(null=True, blank=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "marketplace_offer"
        indexes = [models.Index(fields=["user", "-rank_score"])]

    def __str__(self) -> str:
        return f"Offer<{self.product_id} -> {self.user_id}>"
