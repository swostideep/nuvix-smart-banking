"""Shared fixtures.

Fixtures build *scenarios*, not rows: a user with three debts and a promo card
is a situation the engines are supposed to handle, and naming it makes the
tests read as statements about behaviour rather than as database setup.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from nuvix.accounts.models import User
from nuvix.banking.models import AccountType, Institution, LinkedAccount
from nuvix.credit.models import CreditProfile
from nuvix.debt.models import Debt, DebtKind
from nuvix.marketplace.models import Partner, Product, ProductType


@pytest.fixture
def api() -> APIClient:
    return APIClient()


@pytest.fixture
def user(db) -> User:
    account = User.objects.create_user(
        email="ada@example.com",
        password="correct-horse-battery",
        first_name="Ada",
        last_name="Lovelace",
    )
    profile = account.profile
    profile.annual_income = Decimal("62000")
    profile.monthly_net_income = Decimal("4100")
    profile.monthly_fixed_expenses = Decimal("2600")
    profile.months_employed = 40
    profile.date_of_birth = dt.date(1990, 5, 20)
    profile.state = "WA"
    profile.save()
    return account


@pytest.fixture
def other_user(db) -> User:
    return User.objects.create_user(email="mallory@example.com", password="another-long-pass")


@pytest.fixture
def staff_user(db) -> User:
    return User.objects.create_user(
        email="staff@nuvix.test", password="staff-long-password", is_staff=True
    )


@pytest.fixture
def auth(api: APIClient, user: User) -> APIClient:
    api.force_authenticate(user=user)
    return api


@pytest.fixture
def institution(db) -> Institution:
    return Institution.objects.create(name="First Federal", slug="first-federal")


@pytest.fixture
def checking(user: User, institution: Institution) -> LinkedAccount:
    return LinkedAccount.objects.create(
        user=user,
        institution=institution,
        name="Everyday Checking",
        account_type=AccountType.CHECKING,
        mask="4821",
        current_balance=Decimal("1850.00"),
        available_balance=Decimal("1850.00"),
        is_primary_checking=True,
    )


@pytest.fixture
def card(user: User, institution: Institution) -> LinkedAccount:
    return LinkedAccount.objects.create(
        user=user,
        institution=institution,
        name="Everyday Card",
        account_type=AccountType.CREDIT_CARD,
        mask="9911",
        current_balance=Decimal("-3200.00"),
        credit_limit=Decimal("5000.00"),
        apr=Decimal("24.99"),
        minimum_payment=Decimal("95.00"),
        statement_due_day=14,
    )


@pytest.fixture
def debts(user: User) -> list[Debt]:
    """A realistic mixed book: a promo transfer, a plain card and a loan."""

    return [
        Debt.objects.create(
            user=user,
            name="Balance transfer card",
            kind=DebtKind.CREDIT_CARD,
            balance=Decimal("4800.00"),
            apr=Decimal("26.99"),
            minimum_payment=Decimal("110.00"),
            promo_apr=Decimal("0.00"),
            promo_months_remaining=6,
            credit_limit=Decimal("6000.00"),
            due_day=12,
        ),
        Debt.objects.create(
            user=user,
            name="Store card",
            kind=DebtKind.CREDIT_CARD,
            balance=Decimal("900.00"),
            apr=Decimal("28.99"),
            minimum_payment=Decimal("35.00"),
            credit_limit=Decimal("1500.00"),
            due_day=20,
        ),
        Debt.objects.create(
            user=user,
            name="Auto loan",
            kind=DebtKind.AUTO_LOAN,
            balance=Decimal("9400.00"),
            apr=Decimal("7.25"),
            minimum_payment=Decimal("310.00"),
            due_day=3,
        ),
    ]


@pytest.fixture
def credit_profile(user: User) -> CreditProfile:
    profile, _ = CreditProfile.objects.get_or_create(user=user)
    profile.on_time_payment_rate = Decimal("0.960")
    profile.late_payments_30d = 1
    profile.total_balance = Decimal("3200.00")
    profile.total_credit_limit = Decimal("6500.00")
    profile.max_single_card_utilization = Decimal("0.6400")
    profile.oldest_account_months = 74
    profile.average_account_age_months = 38
    profile.credit_mix_types = 2
    profile.open_accounts = 4
    profile.hard_inquiries_12m = 1
    profile.save()
    return profile


@pytest.fixture
def partner(db) -> Partner:
    return Partner.objects.create(
        name="Harbor Lending", slug="harbor-lending", payout_per_funded=Decimal("120.00")
    )


@pytest.fixture
def products(partner: Partner) -> list[Product]:
    return [
        Product.objects.create(
            partner=partner,
            name="Consolidation Loan",
            product_type=ProductType.DEBT_CONSOLIDATION,
            apr_min=Decimal("8.99"),
            apr_max=Decimal("19.99"),
            origination_fee_rate=Decimal("0.0300"),
            amount_min=Decimal("2000"),
            amount_max=Decimal("25000"),
            term_months_min=24,
            term_months_max=48,
            min_credit_score=640,
            max_dti_ratio=Decimal("0.450"),
            min_annual_income=Decimal("35000"),
            min_months_employed=12,
            historical_approval_rate=Decimal("0.700"),
        ),
        Product.objects.create(
            partner=partner,
            name="Premium Rewards Card",
            product_type=ProductType.CREDIT_CARD,
            apr_min=Decimal("18.99"),
            apr_max=Decimal("28.99"),
            annual_fee=Decimal("95.00"),
            rewards_rate=Decimal("0.0200"),
            amount_min=Decimal("0"),
            amount_max=Decimal("8000"),
            min_credit_score=720,
            max_dti_ratio=Decimal("0.400"),
            min_annual_income=Decimal("60000"),
            historical_approval_rate=Decimal("0.450"),
        ),
        Product.objects.create(
            partner=partner,
            name="Starter Secured Card",
            product_type=ProductType.SECURED_CARD,
            apr_min=Decimal("24.99"),
            apr_max=Decimal("27.99"),
            amount_min=Decimal("200"),
            amount_max=Decimal("1000"),
            min_credit_score=300,
            max_dti_ratio=Decimal("1.000"),
            historical_approval_rate=Decimal("0.950"),
        ),
    ]
