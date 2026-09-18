"""Persisting detection output.

Detection is pure (see :mod:`nuvix.banking.services.recurrence`); this module
is the only place that touches the database, which keeps the algorithm unit
testable without fixtures and the persistence path trivially reviewable.
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from nuvix.accounts.models import User
from nuvix.banking.models import AccountType, LinkedAccount, RecurringSeries, Transaction
from nuvix.banking.services.cashflow import CashflowProjection, project
from nuvix.banking.services.recurrence import (
    DetectedSeries,
    Observation,
    detect_series,
)
from nuvix.core.money import ZERO

#: Twelve months is long enough to see a quarterly series repeat and short
#: enough that a job change from two years ago does not pollute the estimate.
LOOKBACK_DAYS = 365


def _observations(user: User, lookback_days: int) -> list[Observation]:
    since = timezone.localdate() - dt.timedelta(days=lookback_days)
    rows = (
        Transaction.objects.filter(user=user, posted_on__gte=since, is_pending=False)
        .only("posted_on", "amount", "merchant_key", "merchant", "category")
        .iterator(chunk_size=2000)
    )
    return [
        Observation(
            posted_on=row.posted_on,
            amount=row.amount,
            merchant_key=row.merchant_key,
            merchant=row.merchant,
            category=row.category,
        )
        for row in rows
    ]


@transaction.atomic
def refresh_recurring_series(
    user: User, lookback_days: int = LOOKBACK_DAYS
) -> list[RecurringSeries]:
    """Recompute and persist a user's recurring series.

    Replaces the whole set rather than diffing: detection is cheap, the set is
    small, and a stale series that no longer appears in history must disappear
    or it will keep depressing safe-to-spend forever.
    """

    detected = detect_series(_observations(user, lookback_days))

    RecurringSeries.objects.filter(user=user).delete()
    created = RecurringSeries.objects.bulk_create(
        [
            RecurringSeries(
                user=user,
                merchant_key=item.merchant_key,
                label=item.label,
                category=item.category,
                cadence=item.cadence,
                average_amount=item.average_amount,
                is_income=item.is_income,
                occurrences=item.occurrences,
                confidence=item.confidence,
                last_seen_on=item.last_seen_on,
                next_expected_on=item.next_expected_on,
            )
            for item in detected
        ]
    )
    return created


def as_detected(row: RecurringSeries) -> DetectedSeries:
    """Adapt a persisted row back into the pure engine's dataclass."""

    return DetectedSeries(
        merchant_key=row.merchant_key,
        label=row.label,
        category=row.category,
        cadence=row.cadence,
        average_amount=row.average_amount,
        is_income=row.is_income,
        occurrences=row.occurrences,
        confidence=row.confidence,
        last_seen_on=row.last_seen_on,
        next_expected_on=row.next_expected_on,
    )


def spendable_balance(user: User) -> ZERO.__class__:
    """Cash actually available to spend today across deposit accounts."""

    total = ZERO
    for account in LinkedAccount.objects.alive().filter(
        user=user, account_type__in=[AccountType.CHECKING, AccountType.SAVINGS]
    ):
        total += account.available_balance or account.current_balance
    return total


def build_projection(user: User, horizon_days: int = 45) -> CashflowProjection:
    """Project this user's cashflow from their persisted series."""

    series = [as_detected(row) for row in RecurringSeries.objects.filter(user=user)]
    return project(
        opening_balance=spendable_balance(user),
        series=series,
        today=timezone.localdate(),
        horizon_days=horizon_days,
    )
