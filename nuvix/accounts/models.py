"""Identity and the financial profile that every engine reads from."""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from nuvix.core.money import ZERO


class UserManager(BaseUserManager):
    """Email-first user creation; NuviX has no separate username concept."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("Superuser must have is_staff and is_superuser set.")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    phone = models.CharField(max_length=20, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now, db_index=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        db_table = "accounts_user"
        ordering = ("-date_joined",)

    def __str__(self) -> str:
        return self.email

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email


class EmploymentStatus(models.TextChoices):
    FULL_TIME = "full_time", "Full time"
    PART_TIME = "part_time", "Part time"
    SELF_EMPLOYED = "self_employed", "Self employed"
    CONTRACT = "contract", "Contract"
    UNEMPLOYED = "unemployed", "Unemployed"
    RETIRED = "retired", "Retired"


class PayFrequency(models.TextChoices):
    WEEKLY = "weekly", "Weekly"
    BIWEEKLY = "biweekly", "Every two weeks"
    SEMIMONTHLY = "semimonthly", "Twice a month"
    MONTHLY = "monthly", "Monthly"


class Profile(models.Model):
    """The financial facts the credit, debt and marketplace engines depend on.

    Split from ``User`` because it is written on a different cadence (a user
    row changes at signup; a profile changes whenever income or employment
    changes) and because it carries the sensitive attributes that deserve
    narrower access.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    date_of_birth = models.DateField(null=True, blank=True)
    state = models.CharField(max_length=2, blank=True, help_text="US state code")

    annual_income = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    monthly_net_income = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    pay_frequency = models.CharField(
        max_length=20, choices=PayFrequency.choices, default=PayFrequency.BIWEEKLY
    )
    employment_status = models.CharField(
        max_length=20, choices=EmploymentStatus.choices, default=EmploymentStatus.FULL_TIME
    )
    months_employed = models.PositiveIntegerField(default=0)

    dependents = models.PositiveSmallIntegerField(default=0)
    has_mortgage = models.BooleanField(default=False)

    # Declared at onboarding, refined from linked accounts once they exist.
    monthly_fixed_expenses = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    onboarding_completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_profile"

    def __str__(self) -> str:
        return f"Profile<{self.user.email}>"

    @property
    def age(self) -> int | None:
        if not self.date_of_birth:
            return None
        today = timezone.localdate()
        return (
            today.year
            - self.date_of_birth.year
            - ((today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day))
        )

    @property
    def monthly_gross_income(self) -> Decimal:
        return (self.annual_income / Decimal(12)).quantize(Decimal("0.01"))

    @property
    def monthly_surplus(self) -> Decimal:
        """Income left after declared fixed expenses.

        The ceiling on what the debt engine may direct at balances -- planning
        a payoff the customer cannot fund is worse than no plan at all.
        """

        return self.monthly_net_income - self.monthly_fixed_expenses
