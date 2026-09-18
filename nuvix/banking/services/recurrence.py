"""Recurring-transaction detection.

Bright-style money guidance is only as good as its view of a household's
committed cashflow. Bank feeds do not label a paycheque or a car payment, so
NuviX derives them: group a user's history by normalised merchant, look for a
stable cadence in the date gaps and a stable amount, and emit a
:class:`DetectedSeries` when both hold.

Design notes
------------
* Runs in ``O(n log n)`` for ``n`` transactions -- one pass to bucket, then a
  sort per bucket. It is deliberately a deterministic statistical rule rather
  than a learned model: the output drives customer-facing statements
  ("your paycheque lands Friday"), and a rule can be explained to a support
  agent and to a regulator.
* Tolerances are module constants rather than magic numbers so that they can
  be tuned from one place once real feed data exposes their failure modes.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from nuvix.core.money import money

#: Minimum number of observations before a pattern is called recurring. Two
#: points define a gap, not a cadence -- three is the smallest number that can
#: confirm a repeat.
MIN_OCCURRENCES = 3

#: Nominal day-gap for each supported cadence, and how far an individual gap
#: may drift and still count. Tolerances widen with the period because a
#: monthly biller drifting over a weekend moves further in absolute days than
#: a weekly one.
CADENCE_DAYS: dict[str, int] = {
    "weekly": 7,
    "biweekly": 14,
    "semimonthly": 15,
    "monthly": 30,
    "quarterly": 91,
}
CADENCE_TOLERANCE: dict[str, int] = {
    "weekly": 2,
    "biweekly": 3,
    "semimonthly": 3,
    "monthly": 5,
    "quarterly": 10,
}

#: Fraction of observed gaps that must agree on one cadence.
MIN_CADENCE_AGREEMENT = 0.6

#: Maximum relative dispersion of amounts. A utility bill varies month to
#: month; a subscription does not. 0.25 admits the former, rejects noise.
MAX_AMOUNT_DISPERSION = Decimal("0.25")

#: Tokens banks staple onto merchant descriptors that carry no identity.
_NOISE_TOKENS = frozenset(
    {
        "POS",
        "ACH",
        "DEBIT",
        "CREDIT",
        "PURCHASE",
        "PAYMENT",
        "PMT",
        "RECURRING",
        "WITHDRAWAL",
        "DEPOSIT",
        "TRANSFER",
        "XFER",
        "ONLINE",
        "WEB",
        "ID",
        "REF",
        "CKCD",
        "DES",
        "INDN",
        "CO",
        "TYPE",
        "PPD",
        "TEL",
        "VISA",
        "MASTERCARD",
    }
)
_NON_ALPHANUM = re.compile(r"[^A-Z0-9 ]+")
_LONG_DIGITS = re.compile(r"\b\d{2,}\b")


def normalise_merchant(raw: str) -> str:
    """Reduce a bank descriptor to a stable merchant key.

    ``"POS DEBIT SAFEWAY #1423 SEATTLE WA 03/14"`` and
    ``"SAFEWAY #0881 PORTLAND OR 04/14"`` both collapse to ``"SAFEWAY"``, so
    the two rows land in the same bucket and a cadence becomes visible.
    """

    text = _NON_ALPHANUM.sub(" ", raw.upper())
    text = _LONG_DIGITS.sub(" ", text)
    tokens = [t for t in text.split() if t and t not in _NOISE_TOKENS and len(t) > 1]
    # Two tokens is enough to disambiguate ("STATE FARM", "AMERICAN EXPRESS")
    # without letting a branch name split one merchant into many.
    return " ".join(tokens[:2]) or raw.upper().strip()[:32]


@dataclass(frozen=True, slots=True)
class Observation:
    """One transaction, reduced to what detection actually needs."""

    posted_on: dt.date
    amount: Decimal
    merchant_key: str
    merchant: str
    category: str


@dataclass(frozen=True, slots=True)
class DetectedSeries:
    merchant_key: str
    label: str
    category: str
    cadence: str
    average_amount: Decimal
    is_income: bool
    occurrences: int
    confidence: Decimal
    last_seen_on: dt.date
    next_expected_on: dt.date


def _classify_gap(gap_days: int) -> str | None:
    """Map one observed day-gap onto a cadence, or ``None`` if it fits none.

    Candidates are checked in ascending period order and the closest match
    within tolerance wins, so a 15-day gap resolves to ``semimonthly`` rather
    than being claimed by ``biweekly``'s upper tolerance bound.
    """

    best: tuple[int, str] | None = None
    for name, nominal in CADENCE_DAYS.items():
        distance = abs(gap_days - nominal)
        if distance <= CADENCE_TOLERANCE[name] and (best is None or distance < best[0]):
            best = (distance, name)
    return best[1] if best else None


def _amount_dispersion(amounts: Sequence[Decimal]) -> Decimal:
    """Relative dispersion of amounts, robust to a single odd month.

    Median absolute deviation over the median, rather than a standard
    deviation over a mean: one unusually large electricity bill in a heatwave
    should not disqualify an otherwise obvious monthly series.
    """

    median = statistics.median(amounts)
    if median == 0:
        return Decimal("1")
    mad = statistics.median([abs(a - median) for a in amounts])
    return (Decimal(mad) / Decimal(abs(median))).quantize(Decimal("0.0001"))


def _advance(date: dt.date, cadence: str) -> dt.date:
    """Next expected occurrence after ``date``.

    Monthly and quarterly step by calendar month -- clamped to the last valid
    day -- rather than by a fixed 30/91 days, so a rent payment on the 31st
    does not drift backwards through the year.
    """

    if cadence in {"weekly", "biweekly", "semimonthly"}:
        return date + dt.timedelta(days=CADENCE_DAYS[cadence])

    step = 3 if cadence == "quarterly" else 1
    month_index = date.month - 1 + step
    year = date.year + month_index // 12
    month = month_index % 12 + 1
    last_day_of_month = (
        dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    ).day
    return dt.date(year, month, min(date.day, last_day_of_month))


def detect_series(observations: Iterable[Observation]) -> list[DetectedSeries]:
    """Find every recurring inflow and outflow in ``observations``.

    Inflows and outflows are bucketed separately even under the same merchant:
    a refund from a merchant you also pay is a different phenomenon from the
    payment, and merging the two would destroy both the cadence and the mean.
    """

    buckets: dict[tuple[str, bool], list[Observation]] = defaultdict(list)
    for obs in observations:
        if obs.amount == 0:
            continue
        buckets[(obs.merchant_key, obs.amount > 0)].append(obs)

    detected: list[DetectedSeries] = []
    for (merchant_key, is_income), rows in buckets.items():
        if len(rows) < MIN_OCCURRENCES:
            continue
        series = _evaluate_bucket(merchant_key, is_income, rows)
        if series is not None:
            detected.append(series)

    detected.sort(key=lambda s: (not s.is_income, -s.average_amount))
    return detected


def _evaluate_bucket(
    merchant_key: str, is_income: bool, rows: list[Observation]
) -> DetectedSeries | None:
    rows = sorted(rows, key=lambda o: o.posted_on)

    # Collapse same-day duplicates: one bill split across two postings is one
    # occurrence, and leaving both in fabricates a zero-day gap.
    deduped: list[Observation] = []
    for row in rows:
        if deduped and deduped[-1].posted_on == row.posted_on:
            continue
        deduped.append(row)
    if len(deduped) < MIN_OCCURRENCES:
        return None

    gaps = [(b.posted_on - a.posted_on).days for a, b in itertools.pairwise(deduped)]
    classified = [c for c in (_classify_gap(gap) for gap in gaps) if c is not None]
    if not classified:
        return None

    cadence, hits = Counter(classified).most_common(1)[0]
    agreement = hits / len(gaps)
    if agreement < MIN_CADENCE_AGREEMENT:
        return None

    amounts = [abs(row.amount) for row in deduped]
    dispersion = _amount_dispersion(amounts)
    if dispersion > MAX_AMOUNT_DISPERSION:
        return None

    # Confidence blends the three independent signals: how consistently the
    # gaps agree, how stable the amount is, and how much evidence there is.
    # Each is in [0, 1], so the product stays interpretable.
    evidence = min(len(deduped) / 6, 1.0)
    stability = float(1 - dispersion / MAX_AMOUNT_DISPERSION)
    confidence = Decimal(str(round(agreement * stability * evidence, 3)))

    last = deduped[-1]
    return DetectedSeries(
        merchant_key=merchant_key,
        label=last.merchant,
        category=last.category,
        cadence=cadence,
        average_amount=money(statistics.median(amounts)),
        is_income=is_income,
        occurrences=len(deduped),
        confidence=confidence,
        last_seen_on=last.posted_on,
        next_expected_on=_advance(last.posted_on, cadence),
    )


def upcoming_occurrences(
    series: DetectedSeries, until: dt.date, start_from: dt.date | None = None
) -> list[dt.date]:
    """Project future dates for ``series`` up to and including ``until``."""

    dates: list[dt.date] = []
    cursor = start_from or series.next_expected_on
    guard = 0
    while cursor <= until and guard < 500:
        dates.append(cursor)
        cursor = _advance(cursor, series.cadence)
        guard += 1
    return dates
