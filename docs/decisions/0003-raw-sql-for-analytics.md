# ADR-0003 — Raw SQL for analytics, the ORM for everything else

**Status:** accepted

## Context

Cohort retention, channel payback and multi-touch attribution are the reports
the growth team runs. Django's ORM can express them with `Window`,
`Subquery` and `annotate`.

## Decision

Reports in `nuvix/analytics/queries.py` are hand-written SQL with bound
parameters. Every other database access in the project uses the ORM.

## Reasoning

These queries are **set-shaped**. First-touch attribution is
`ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY occurred_at)`; cohort
retention is a self-join across a generated month series. Through the ORM each
becomes three times the code, emits SQL nobody chose, and — the real problem —
becomes impossible to check against the numbers a finance team quotes back.
A reviewer can read the SQL and verify the definition of CAC. Nobody can do
that with a chain of `annotate` calls.

The ORM stays everywhere else because the transactional apps want exactly what
it gives: validation, migrations, relationships and safety.

## Consequences

**Good.** Reports are readable, fast, and reviewable by anyone who reads SQL.

**Bad, and mitigated.** Raw SQL costs portability and safety, so three rules
are enforced:

1. **Every value is a bound parameter.** The only interpolation is of dialect
   fragments under this module's control.
2. **A dialect shim** (`nuvix/analytics/sql/dialect.py`) isolates the
   backend-specific date functions.
3. **CI runs the whole suite twice** — SQLite and Postgres — because this is
   precisely the code most able to pass on one and fail on the other.

The mitigation earned its keep immediately. Three real bugs came out of it: a
dialect fragment duplicating a bound parameter, a `strftime('%Y')` that
crashed only under `DEBUG=True`, and an unparenthesised `safe_divide` that
reported ROAS as 1535.20 instead of 0.56. All three now have regression tests.
