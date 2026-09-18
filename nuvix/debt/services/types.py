"""Value objects for the payoff engine.

The engine is deliberately pure: it takes frozen dataclasses and returns
frozen dataclasses, touching neither the ORM nor the clock. That makes every
schedule reproducible from its inputs, which matters when a customer disputes
a projection months later, and it lets the algorithms be unit tested at speed
without a database.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from nuvix.core.money import ZERO, money, rate


@dataclass(frozen=True, slots=True)
class DebtInput:
    """One balance to be paid off.

    Promotional rates are first-class. A balance-transfer card at 0% for nine
    months that reverts to 26.99% is the single most common structure in the
    target market, and an engine that only understands one APR per debt will
    confidently recommend the wrong order.
    """

    key: str
    name: str
    balance: Decimal
    apr: Decimal
    minimum_payment: Decimal
    #: Some issuers quote a minimum as a percentage of the balance. When set,
    #: the required payment is the larger of the two, so the minimum falls as
    #: the balance does -- which is what actually happens on a statement.
    minimum_payment_rate: Decimal = ZERO
    promo_apr: Decimal | None = None
    promo_months_remaining: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "balance", money(self.balance))
        object.__setattr__(self, "apr", rate(self.apr))
        object.__setattr__(self, "minimum_payment", money(self.minimum_payment))
        if self.promo_apr is not None:
            object.__setattr__(self, "promo_apr", rate(self.promo_apr))

    def apr_in_month(self, month_index: int) -> Decimal:
        """Effective annual rate during ``month_index`` (0-based).

        Once the promotional window lapses the balance reverts to the standard
        APR -- the engine must see that cliff coming to price it correctly.
        """

        if self.promo_apr is not None and month_index < self.promo_months_remaining:
            return self.promo_apr
        return self.apr

    def required_payment(self, balance: Decimal) -> Decimal:
        """The contractual minimum at this balance."""

        floor = self.minimum_payment
        if self.minimum_payment_rate > 0:
            floor = max(floor, money(balance * self.minimum_payment_rate))
        return min(money(balance), floor)


@dataclass(frozen=True, slots=True)
class DebtMonth:
    """What happened to one debt in one month."""

    key: str
    opening_balance: Decimal
    interest_charged: Decimal
    payment: Decimal
    closing_balance: Decimal


@dataclass(frozen=True, slots=True)
class MonthSnapshot:
    month_index: int
    date: dt.date
    total_payment: Decimal
    total_interest: Decimal
    total_balance: Decimal
    lines: tuple[DebtMonth, ...]


@dataclass(frozen=True, slots=True)
class DebtOutcome:
    key: str
    name: str
    original_balance: Decimal
    interest_paid: Decimal
    total_paid: Decimal
    months_to_payoff: int
    payoff_date: dt.date


@dataclass(slots=True)
class PayoffResult:
    """A complete payoff projection."""

    strategy: str
    monthly_budget: Decimal
    months_to_debt_free: int
    debt_free_date: dt.date
    total_interest: Decimal
    total_paid: Decimal
    original_balance: Decimal
    order: tuple[str, ...]
    outcomes: tuple[DebtOutcome, ...] = ()
    schedule: list[MonthSnapshot] = field(default_factory=list)
    #: True when the simulation hit its month cap instead of clearing every
    #: balance -- the projected date is a floor, not an answer.
    truncated: bool = False
    #: True when payments stopped covering interest and balances began to
    #: grow. The only honest reading is "this budget never pays this off".
    negative_amortisation: bool = False

    def summary(self) -> dict[str, object]:
        """Compact, JSON-serialisable form for storage and API responses."""

        return {
            "strategy": self.strategy,
            "monthly_budget": str(self.monthly_budget),
            "months_to_debt_free": self.months_to_debt_free,
            "debt_free_date": self.debt_free_date.isoformat(),
            "total_interest": str(self.total_interest),
            "total_paid": str(self.total_paid),
            "original_balance": str(self.original_balance),
            "truncated": self.truncated,
            "negative_amortisation": self.negative_amortisation,
            "order": list(self.order),
            "outcomes": [
                {
                    "key": o.key,
                    "name": o.name,
                    "original_balance": str(o.original_balance),
                    "interest_paid": str(o.interest_paid),
                    "total_paid": str(o.total_paid),
                    "months_to_payoff": o.months_to_payoff,
                    "payoff_date": o.payoff_date.isoformat(),
                }
                for o in self.outcomes
            ],
        }
