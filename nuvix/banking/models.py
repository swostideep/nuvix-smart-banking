"""Linked bank accounts and the transaction ledger they produce."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from nuvix.core.models import SoftDeleteModel, TimeStampedModel
from nuvix.core.money import ZERO


class Institution(TimeStampedModel):
    """A bank or issuer NuviX can link to."""

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=120, unique=True)
    logo_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta(TimeStampedModel.Meta):
        db_table = "banking_institution"

    def __str__(self) -> str:
        return self.name


class AccountType(models.TextChoices):
    CHECKING = "checking", "Checking"
    SAVINGS = "savings", "Savings"
    CREDIT_CARD = "credit_card", "Credit card"
    LOAN = "loan", "Loan"


class LinkedAccount(SoftDeleteModel):
    """An account the user has connected.

    ``current_balance`` is stored signed from the *user's* point of view:
    positive is money the user has, negative is money the user owes. Keeping
    one convention across deposit and credit accounts means the cashflow
    engine never has to branch on account type to add two balances together.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="linked_accounts"
    )
    institution = models.ForeignKey(
        Institution, on_delete=models.PROTECT, related_name="linked_accounts"
    )
    name = models.CharField(max_length=120)
    account_type = models.CharField(max_length=20, choices=AccountType.choices)
    mask = models.CharField(max_length=4, blank=True, help_text="Last 4 digits")

    current_balance = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    available_balance = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)

    # Credit-line attributes; null for deposit accounts.
    credit_limit = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    apr = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True, help_text="Annual %"
    )
    minimum_payment = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    statement_due_day = models.PositiveSmallIntegerField(null=True, blank=True)

    is_primary_checking = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta(SoftDeleteModel.Meta):
        db_table = "banking_linked_account"
        indexes = [
            models.Index(fields=["user", "account_type"]),
            models.Index(fields=["user", "deleted_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "institution", "mask", "account_type"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_active_account_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ••{self.mask}"

    @property
    def is_revolving(self) -> bool:
        return self.account_type == AccountType.CREDIT_CARD

    @property
    def utilization(self) -> Decimal | None:
        """Balance as a fraction of the limit -- the single biggest movable
        lever on a credit score after payment history."""

        if not self.is_revolving or not self.credit_limit:
            return None
        used = abs(min(self.current_balance, ZERO))
        return (used / self.credit_limit).quantize(Decimal("0.0001"))


class TransactionCategory(models.TextChoices):
    INCOME = "income", "Income"
    HOUSING = "housing", "Housing"
    TRANSPORT = "transport", "Transport"
    GROCERIES = "groceries", "Groceries"
    UTILITIES = "utilities", "Utilities"
    INSURANCE = "insurance", "Insurance"
    DEBT_PAYMENT = "debt_payment", "Debt payment"
    SUBSCRIPTION = "subscription", "Subscription"
    DINING = "dining", "Dining"
    SHOPPING = "shopping", "Shopping"
    HEALTHCARE = "healthcare", "Healthcare"
    TRANSFER = "transfer", "Transfer"
    FEE = "fee", "Fee"
    OTHER = "other", "Other"


#: Categories a household cannot skip next month. Used by the cashflow engine
#: to separate committed outflow from discretionary spend.
FIXED_CATEGORIES = frozenset(
    {
        TransactionCategory.HOUSING,
        TransactionCategory.UTILITIES,
        TransactionCategory.INSURANCE,
        TransactionCategory.DEBT_PAYMENT,
        TransactionCategory.SUBSCRIPTION,
    }
)


class Transaction(TimeStampedModel):
    """A single posted or pending movement of money.

    ``amount`` is signed: credits to the user are positive, debits negative.
    """

    account = models.ForeignKey(
        LinkedAccount, on_delete=models.CASCADE, related_name="transactions"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="transactions"
    )
    external_id = models.CharField(max_length=64, blank=True, db_index=True)

    posted_on = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    merchant = models.CharField(max_length=160)
    merchant_key = models.CharField(
        max_length=160,
        db_index=True,
        help_text="Normalised merchant used for recurrence detection",
    )
    category = models.CharField(
        max_length=20,
        choices=TransactionCategory.choices,
        default=TransactionCategory.OTHER,
        db_index=True,
    )
    is_pending = models.BooleanField(default=False)

    class Meta(TimeStampedModel.Meta):
        db_table = "banking_transaction"
        ordering = ("-posted_on", "-created_at")
        indexes = [
            models.Index(fields=["user", "-posted_on"]),
            models.Index(fields=["user", "category", "-posted_on"]),
            models.Index(fields=["account", "-posted_on"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "external_id"],
                condition=models.Q(external_id__gt=""),
                name="uniq_txn_per_account_external_id",
            )
        ]

    def __str__(self) -> str:
        return f"{self.posted_on} {self.merchant} {self.amount}"

    def save(self, *args, **kwargs):
        if not self.merchant_key:
            from nuvix.banking.services.recurrence import normalise_merchant

            self.merchant_key = normalise_merchant(self.merchant)
        if self.user_id is None:
            self.user_id = self.account.user_id
        super().save(*args, **kwargs)


class RecurringSeries(TimeStampedModel):
    """A detected recurring inflow or outflow.

    Materialised from transaction history by
    :mod:`nuvix.banking.services.recurrence` so that cashflow projections and
    the communications engine can read a stable row instead of re-running
    detection on every request.
    """

    class Cadence(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Every two weeks"
        SEMIMONTHLY = "semimonthly", "Twice a month"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recurring_series"
    )
    merchant_key = models.CharField(max_length=160)
    label = models.CharField(max_length=160)
    category = models.CharField(
        max_length=20,
        choices=TransactionCategory.choices,
        default=TransactionCategory.OTHER,
    )
    cadence = models.CharField(max_length=20, choices=Cadence.choices)
    average_amount = models.DecimalField(max_digits=12, decimal_places=2)
    is_income = models.BooleanField(default=False)
    occurrences = models.PositiveSmallIntegerField(default=0)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0"))
    last_seen_on = models.DateField()
    next_expected_on = models.DateField()

    class Meta(TimeStampedModel.Meta):
        db_table = "banking_recurring_series"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "merchant_key", "cadence"],
                name="uniq_series_per_user_merchant_cadence",
            )
        ]
        indexes = [models.Index(fields=["user", "next_expected_on"])]

    def __str__(self) -> str:
        return f"{self.label} ({self.cadence})"
