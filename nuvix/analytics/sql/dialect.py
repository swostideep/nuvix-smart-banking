"""A minimal SQL dialect shim.

The analytics in this module are written as **raw SQL on purpose**. The
questions -- cohort retention, channel payback, multi-touch attribution --
are set-shaped: they want window functions, ``FILTER`` clauses, generated
month series and self-joins. Expressing them through the ORM produces code
that is longer, slower and harder to check against the numbers a finance team
will quote back at you. The ORM is the right tool for the transactional apps;
it is the wrong tool here.

Raw SQL costs portability, because the date functions every analytical query
needs are the least standardised part of SQL. This module is that cost, paid
once: Postgres runs in production, SQLite runs the test suite, and the queries
themselves stay readable in both.

Everything here emits *fragments*, never values. Values are always passed as
bound parameters by the caller -- see :mod:`nuvix.analytics.queries`.

.. warning::

   **A fragment argument must be a column or expression, never a ``%s``
   placeholder.** Several fragments reference their arguments more than once
   (``months_between`` expands to a year term and a month term), so a
   placeholder passed in would be emitted twice while the caller binds it
   once, and the driver would reject the statement -- or worse, silently bind
   the wrong values in the wrong positions.

   Where a query needs a parameter inside one of these fragments, bind it once
   as a column in a preceding CTE and pass that column's name here. Both
   :func:`~nuvix.analytics.queries.channel_performance` and
   :func:`~nuvix.analytics.queries.cohort_retention` do exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import connection


@dataclass(frozen=True, slots=True)
class Dialect:
    """SQL fragments that differ between backends."""

    vendor: str

    # -- date truncation ---------------------------------------------------
    def month(self, column: str) -> str:
        """Truncate a date/timestamp column to the first of its month."""

        if self.vendor == "postgresql":
            return f"DATE_TRUNC('month', {column})::date"
        if self.vendor == "sqlite":
            return f"DATE({column}, 'start of month')"
        if self.vendor == "mysql":
            # Doubled percent signs for the same reason as _sqlite_year below.
            return f"DATE_FORMAT({column}, '%%Y-%%m-01')"
        raise NotImplementedError(f"No month() for vendor {self.vendor!r}")

    def day(self, column: str) -> str:
        if self.vendor == "postgresql":
            return f"{column}::date"
        if self.vendor == "sqlite":
            return f"DATE({column})"
        return f"DATE({column})"

    # -- date arithmetic ---------------------------------------------------
    def months_between(self, later: str, earlier: str) -> str:
        """Whole calendar months from ``earlier`` to ``later``.

        Calendar months, not elapsed days over 30: a cohort's "month 1" is the
        next calendar month, and a day-based approximation silently misfiles
        users who signed up on the 31st.
        """

        if self.vendor == "postgresql":
            return (
                f"((EXTRACT(YEAR FROM {later})::int - EXTRACT(YEAR FROM {earlier})::int) * 12"
                f" + (EXTRACT(MONTH FROM {later})::int - EXTRACT(MONTH FROM {earlier})::int))"
            )
        if self.vendor == "sqlite":
            return (
                f"(({self._sqlite_year(later)} - {self._sqlite_year(earlier)}) * 12"
                f" + ({self._sqlite_month(later)} - {self._sqlite_month(earlier)}))"
            )
        raise NotImplementedError(f"No months_between() for vendor {self.vendor!r}")

    # -- SQLite date parts -------------------------------------------------
    #
    # ``strftime('%Y', ...)`` is the obvious way to do this and is a trap.
    # Django's SQLite backend renders the debug query with ``sql % params``,
    # so a literal ``%Y`` in the statement raises
    # ``ValueError: unsupported format character 'Y'`` -- but only when DEBUG
    # is on, which means it passes every test on a CI runner and then breaks
    # the first time someone opens the report locally.
    #
    # SQLite stores dates as ``YYYY-MM-DD`` text (and datetimes as
    # ``YYYY-MM-DD HH:MM:SS``), so slicing the string gets the same answer
    # with no percent sign anywhere in the SQL.

    @staticmethod
    def _sqlite_year(column: str) -> str:
        return f"CAST(substr({column}, 1, 4) AS INTEGER)"

    @staticmethod
    def _sqlite_month(column: str) -> str:
        return f"CAST(substr({column}, 6, 2) AS INTEGER)"

    # -- numeric safety ----------------------------------------------------
    def safe_divide(self, numerator: str, denominator: str) -> str:
        """Division that yields NULL rather than raising on a zero divisor.

        ``NULLIF`` is standard and supported everywhere. Worth stating
        explicitly: a channel with zero spend is a real row in a marketing
        report, and it must not take the whole query down.

        """

        return f"({numerator} * 1.0 / NULLIF({denominator}, 0))"

    def cast_numeric(self, expression: str) -> str:
        if self.vendor == "postgresql":
            return f"({expression})::numeric"
        return f"CAST({expression} AS REAL)"


def current_dialect() -> Dialect:
    return Dialect(vendor=connection.vendor)
