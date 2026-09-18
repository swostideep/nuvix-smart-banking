"""Credit profile, score history and risk decisions."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from nuvix.core.models import TimeStampedModel
from nuvix.core.money import ZERO
from nuvix.credit.engine.factors import CreditInputs


class CreditProfile(TimeStampedModel):
    """The bureau-shaped view of a user.

    Populated from a bureau pull where one exists and otherwise derived from
    linked accounts. Either way it is the sole input to the factor model, so
    the score is always reproducible from a single row.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="credit_profile"
    )

    # --- payment history ---------------------------------------------------
    on_time_payment_rate = models.DecimalField(
        max_digits=4, decimal_places=3, default=Decimal("1.000")
    )
    late_payments_30d = models.PositiveSmallIntegerField(default=0)
    late_payments_90d = models.PositiveSmallIntegerField(default=0)
    derogatory_marks = models.PositiveSmallIntegerField(default=0)

    # --- utilisation -------------------------------------------------------
    total_balance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    total_credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    max_single_card_utilization = models.DecimalField(
        max_digits=6, decimal_places=4, default=ZERO
    )

    # --- age and mix -------------------------------------------------------
    oldest_account_months = models.PositiveIntegerField(default=0)
    average_account_age_months = models.PositiveIntegerField(default=0)
    credit_mix_types = models.PositiveSmallIntegerField(default=1)
    open_accounts = models.PositiveSmallIntegerField(default=1)

    # --- new credit --------------------------------------------------------
    hard_inquiries_12m = models.PositiveSmallIntegerField(default=0)
    accounts_opened_24m = models.PositiveSmallIntegerField(default=0)

    # --- cached score ------------------------------------------------------
    score = models.PositiveSmallIntegerField(default=300)
    band = models.CharField(max_length=20, default="poor")
    factors = models.JSONField(default=list)
    last_scored_at = models.DateTimeField(null=True, blank=True)
    bureau = models.CharField(max_length=20, default="nuvix_model")

    class Meta(TimeStampedModel.Meta):
        db_table = "credit_profile"

    def __str__(self) -> str:
        return f"CreditProfile<{self.user_id}: {self.score}>"

    def to_inputs(self) -> CreditInputs:
        """Adapt the row into the pure factor model's value object."""

        return CreditInputs(
            on_time_payment_rate=self.on_time_payment_rate,
            late_payments_30d=self.late_payments_30d,
            late_payments_90d=self.late_payments_90d,
            derogatory_marks=self.derogatory_marks,
            total_balance=self.total_balance,
            total_credit_limit=self.total_credit_limit,
            max_single_card_utilization=self.max_single_card_utilization,
            oldest_account_months=self.oldest_account_months,
            average_account_age_months=self.average_account_age_months,
            credit_mix_types=self.credit_mix_types,
            open_accounts=self.open_accounts,
            hard_inquiries_12m=self.hard_inquiries_12m,
            accounts_opened_24m=self.accounts_opened_24m,
        )

    @property
    def utilization(self) -> Decimal:
        if self.total_credit_limit <= 0:
            return ZERO
        return (self.total_balance / self.total_credit_limit).quantize(Decimal("0.0001"))


class ScoreSnapshot(TimeStampedModel):
    """One point on the score history chart.

    Written only when the score actually changes, so the series is a record of
    movements rather than a daily log of the same number.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="score_snapshots"
    )
    score = models.PositiveSmallIntegerField()
    band = models.CharField(max_length=20)
    delta = models.SmallIntegerField(default=0)
    utilization = models.DecimalField(max_digits=6, decimal_places=4, default=ZERO)
    factors = models.JSONField(default=list)
    captured_on = models.DateField(db_index=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "credit_score_snapshot"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "captured_on"], name="uniq_snapshot_per_user_day"
            )
        ]
        indexes = [models.Index(fields=["user", "-captured_on"])]

    def __str__(self) -> str:
        return f"{self.captured_on}: {self.score}"


class RiskDecision(models.TextChoices):
    APPROVE = "approve", "Approve"
    REFER = "refer", "Refer for manual review"
    DECLINE = "decline", "Decline"


class RiskAssessment(TimeStampedModel):
    """A probability-of-default inference and the decision it drove.

    Model version, the exact features and the reason codes are all persisted.
    Under US adverse-action rules a declined applicant is entitled to the
    specific reasons, and "we cannot reconstruct what the model saw" is not an
    answer that survives an audit.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="risk_assessments"
    )
    model_version = models.CharField(max_length=60)
    model_kind = models.CharField(max_length=20, default="scorecard")

    probability_of_default = models.DecimalField(max_digits=6, decimal_places=5)
    risk_grade = models.CharField(max_length=2)
    decision = models.CharField(max_length=20, choices=RiskDecision.choices)

    requested_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    features = models.JSONField(default=dict)
    reason_codes = models.JSONField(default=list)

    class Meta(TimeStampedModel.Meta):
        db_table = "credit_risk_assessment"
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.decision} pd={self.probability_of_default}"
