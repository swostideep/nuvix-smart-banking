"""Forward cashflow projection and safe-to-spend.

The question a paycheque-to-paycheque customer actually asks is not "what is
my balance?" but "how much can I spend today without missing rent?". This
module answers that by walking the detected recurring series forward day by
day and reporting the *trough* of the projected balance curve, not its
endpoint.
"""

from __future__ import annotations

import datetime as dt
import heapq
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from nuvix.banking.services.recurrence import (
    DetectedSeries,
    _advance,
)
from nuvix.core.money import ZERO, money

#: Cash held back from safe-to-spend. A projection is an estimate; telling a
#: customer they can spend to exactly zero converts any small error into an
#: overdraft fee, which is the precise harm the product exists to prevent.
DEFAULT_BUFFER = Decimal("50.00")

DEFAULT_HORIZON_DAYS = 45


@dataclass(frozen=True, slots=True)
class CashflowEvent:
    date: dt.date
    amount: Decimal
    label: str
    is_income: bool


@dataclass(slots=True)
class CashflowProjection:
    opening_balance: Decimal
    horizon_days: int
    events: list[CashflowEvent] = field(default_factory=list)
    daily_balances: list[tuple[dt.date, Decimal]] = field(default_factory=list)
    lowest_balance: Decimal = ZERO
    lowest_balance_on: dt.date | None = None
    next_income_on: dt.date | None = None
    next_income_amount: Decimal = ZERO
    total_inflow: Decimal = ZERO
    total_outflow: Decimal = ZERO
    safe_to_spend: Decimal = ZERO
    shortfall_risk: bool = False

    @property
    def closing_balance(self) -> Decimal:
        return self.daily_balances[-1][1] if self.daily_balances else self.opening_balance


def expand_events(
    series: Iterable[DetectedSeries], start: dt.date, end: dt.date
) -> list[CashflowEvent]:
    """Flatten recurring series into a date-ordered event stream.

    Each series is an independent, already-sorted generator of dates, so the
    streams are merged with a heap: ``O(k log s)`` for ``k`` emitted events
    across ``s`` series, rather than concatenating everything and sorting.
    """

    heap: list[tuple[dt.date, int, Decimal, str, bool]] = []
    materialised: list[DetectedSeries] = list(series)

    for index, item in enumerate(materialised):
        # A series whose next expected date has already slipped into the past
        # is pulled forward to the window start rather than dropped: a bill
        # that is late is still owed, and hiding it overstates safe-to-spend.
        cursor = max(item.next_expected_on, start)
        if cursor <= end:
            signed = item.average_amount if item.is_income else -item.average_amount
            heapq.heappush(heap, (cursor, index, signed, item.label, item.is_income))

    events: list[CashflowEvent] = []
    while heap:
        date, index, amount, label, is_income = heapq.heappop(heap)
        events.append(CashflowEvent(date=date, amount=amount, label=label, is_income=is_income))
        nxt = _advance(date, materialised[index].cadence)
        if nxt <= end:
            heapq.heappush(heap, (nxt, index, amount, label, is_income))
    return events


def project(
    opening_balance: Decimal,
    series: Sequence[DetectedSeries],
    today: dt.date,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    buffer: Decimal = DEFAULT_BUFFER,
) -> CashflowProjection:
    """Project the balance curve and derive safe-to-spend.

    Safe-to-spend is the lowest point of the curve between now and the *next
    income event*, less a buffer -- money that will be needed before more
    arrives is not spendable, however healthy today's balance looks.
    """

    end = today + dt.timedelta(days=horizon_days)
    events = expand_events(series, today, end)

    projection = CashflowProjection(
        opening_balance=money(opening_balance), horizon_days=horizon_days, events=events
    )

    by_date: dict[dt.date, Decimal] = {}
    for event in events:
        by_date[event.date] = by_date.get(event.date, ZERO) + event.amount
        if event.is_income:
            projection.total_inflow += event.amount
            if projection.next_income_on is None:
                projection.next_income_on = event.date
                projection.next_income_amount = event.amount
        else:
            projection.total_outflow += -event.amount

    balance = projection.opening_balance
    lowest = balance
    lowest_on = today
    lowest_before_income = balance

    cursor = today
    while cursor <= end:
        balance += by_date.get(cursor, ZERO)
        projection.daily_balances.append((cursor, money(balance)))
        if balance < lowest:
            lowest, lowest_on = balance, cursor
        horizon_for_spend = projection.next_income_on or end
        if cursor <= horizon_for_spend and balance < lowest_before_income:
            lowest_before_income = balance
        cursor += dt.timedelta(days=1)

    projection.lowest_balance = money(lowest)
    projection.lowest_balance_on = lowest_on
    projection.safe_to_spend = money(max(lowest_before_income - buffer, ZERO))
    projection.shortfall_risk = lowest < ZERO
    return projection
