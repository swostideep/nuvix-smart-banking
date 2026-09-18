"""Debts the user is paying down, and the plans NuviX builds for them."""

from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from nuvix.core.models import SoftDeleteModel, TimeStampedModel
from nuvix.core.money import ZERO
from nuvix.debt.services.types import DebtInput


class PayoffStrategy(models.TextChoices):
    """The strategies the engine can plan with.

    Module level rather than nested on :class:`PayoffPlan` so that the API
    serializers and the engine share one definition. When the same concept has
    two choice lists, they drift, and the generated OpenAPI schema ends up with
    two differently-named enums for one field.
    """

    AVALANCHE = "avalanche", "Avalanche (highest rate first)"
    SNOWBALL = "snowball", "Snowball (smallest balance first)"
    OPTIMAL = "optimal", "Optimised"


class DebtKind(models.TextChoices):
    CREDIT_CARD = "credit_card", "Credit card"
    PERSONAL_LOAN = "personal_loan", "Personal loan"
    STUDENT_LOAN = "student_loan", "Student loan"
    AUTO_LOAN = "auto_loan", "Auto loan"
    MEDICAL = "medical", "Medical debt"
    BNPL = "bnpl", "Buy now pay later"
    OTHER = "other", "Other"


class Debt(SoftDeleteModel):
    """A single balance in the payoff plan.

    Kept separate from :class:`~nuvix.banking.models.LinkedAccount` because a
    debt need not be linked -- plenty of users carry a medical bill or a loan
    from a lender NuviX cannot aggregate, and refusing to plan around it would
    make every plan wrong.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="debts"
    )
    linked_account = models.OneToOneField(
        "banking.LinkedAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="debt",
    )
    name = models.CharField(max_length=120)
    kind = models.CharField(
        max_length=20, choices=DebtKind.choices, default=DebtKind.CREDIT_CARD
    )

    balance = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)]
    )
    apr = models.DecimalField(
        max_digits=6, decimal_places=2, help_text="Standard annual percentage rate"
    )
    minimum_payment = models.DecimalField(max_digits=10, decimal_places=2)
    minimum_payment_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=ZERO,
        help_text="Minimum as a fraction of balance, if the issuer quotes one",
    )

    promo_apr = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Promotional rate, if any",
    )
    promo_months_remaining = models.PositiveSmallIntegerField(default=0)

    due_day = models.PositiveSmallIntegerField(null=True, blank=True)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta(SoftDeleteModel.Meta):
        db_table = "debt_debt"
        indexes = [models.Index(fields=["user", "deleted_at"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.balance})"

    def to_input(self) -> DebtInput:
        """Adapt the ORM row to the pure engine's value object.

        This one method is the whole boundary between persistence and the
        payoff algorithms -- the engine never imports a model.
        """

        return DebtInput(
            key=str(self.id),
            name=self.name,
            balance=self.balance,
            apr=self.apr,
            minimum_payment=self.minimum_payment,
            minimum_payment_rate=self.minimum_payment_rate,
            promo_apr=self.promo_apr,
            promo_months_remaining=self.promo_months_remaining,
        )


class PayoffPlan(TimeStampedModel):
    """A stored projection.

    The full month-by-month schedule is *not* stored: it is derived, it is
    large, and it goes stale the moment a balance changes. What is stored is
    the summary plus the inputs, which is enough to reproduce the schedule on
    demand and to show a customer what they were told and when.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payoff_plans"
    )
    strategy = models.CharField(max_length=20, choices=PayoffStrategy.choices)
    monthly_budget = models.DecimalField(max_digits=12, decimal_places=2)

    months_to_debt_free = models.PositiveIntegerField()
    debt_free_date = models.DateField()
    total_interest = models.DecimalField(max_digits=14, decimal_places=2)
    total_paid = models.DecimalField(max_digits=14, decimal_places=2)
    original_balance = models.DecimalField(max_digits=14, decimal_places=2)
    interest_saved_vs_minimums = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    months_saved_vs_minimums = models.IntegerField(null=True, blank=True)

    #: Engine output, kept verbatim so a historical plan can be explained even
    #: after the engine's own version has moved on.
    summary = models.JSONField(default=dict)
    #: The debt snapshot the plan was computed from.
    inputs = models.JSONField(default=list)

    is_active = models.BooleanField(default=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "debt_payoff_plan"
        indexes = [models.Index(fields=["user", "-created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_active=True),
                name="one_active_plan_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.strategy} plan for {self.user_id}"


class PaymentRecord(TimeStampedModel):
    """An actual payment, used to measure the plan against reality."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="debt_payments"
    )
    debt = models.ForeignKey(Debt, on_delete=models.CASCADE, related_name="payments")
    paid_on = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "debt_payment_record"
        indexes = [models.Index(fields=["user", "-paid_on"])]

    def __str__(self) -> str:
        return f"{self.amount} to {self.debt.name} on {self.paid_on}"
