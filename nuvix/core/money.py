"""Money and rate primitives.

Every monetary amount in NuviX is a ``Decimal`` quantised to cents. Floats are
banned in the financial engines: a payoff schedule iterates hundreds of times,
and binary floating point drift compounds into visibly wrong dollar figures on
a customer's statement.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")
BASIS_POINT = Decimal("0.0001")

ZERO = Decimal("0.00")


def money(value: Decimal | int | float | str) -> Decimal:
    """Coerce ``value`` to a cent-quantised :class:`~decimal.Decimal`.

    ``float`` input is accepted for ergonomics at the edges (JSON payloads,
    test fixtures) but is routed through ``str`` so that ``0.1`` becomes
    ``Decimal("0.10")`` rather than ``Decimal("0.1000000000000000055...")``.
    """

    if isinstance(value, float):
        value = str(value)
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def rate(value: Decimal | int | float | str) -> Decimal:
    """Coerce an annual percentage rate to a 4dp decimal fraction.

    Accepts either a percentage (``18.99``) or a fraction (``0.1899``) and
    normalises to a fraction. Anything at or above 1 is read as a percentage --
    a 100%+ APR fraction is not a product NuviX would ever surface.
    """

    if isinstance(value, float):
        value = str(value)
    decimal_value = Decimal(value)
    if decimal_value >= 1:
        decimal_value = decimal_value / Decimal(100)
    return decimal_value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def monthly_rate(annual_rate: Decimal) -> Decimal:
    """Convert an APR fraction to the periodic monthly rate.

    Uses simple division by 12 rather than the geometric
    ``(1 + apr) ** (1/12) - 1`` because that is how US card issuers and
    lenders actually compute a monthly periodic rate on a statement. Matching
    the issuer's arithmetic matters more than theoretical purity: a projection
    that disagrees with the customer's real statement destroys trust.
    """

    return (Decimal(annual_rate) / Decimal(12)).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_UP
    )


def percent(value: Decimal) -> Decimal:
    """Render a decimal fraction as a 2dp percentage for display."""

    return (Decimal(value) * Decimal(100)).quantize(CENTS, rounding=ROUND_HALF_UP)
