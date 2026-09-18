"""Marketing and growth analytics, in SQL.

These are the questions the growth team actually asks -- "what did this
channel cost us per activated user, and how long until it pays back?",
"is the January cohort retaining like the November one?", "how different does
the ranking look under first-touch versus last-touch?" -- and each is answered
by one query rather than by pulling rows into Python and aggregating there.

Conventions
-----------
* **Every value is a bound parameter.** No f-string ever interpolates
  user input; the only interpolation is of dialect fragments produced by
  :mod:`nuvix.analytics.sql.dialect`, which are literals under this module's
  control. That is what keeps raw SQL safe from injection.
* **Date windows are half-open** ``[start, end)``. Inclusive upper bounds on
  timestamps are the most common off-by-one in reporting: ``<= end`` silently
  drops everything that happened after midnight on the last day.
* **Results are dicts**, so a caller can serialise them without knowing the
  column order.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db import connection

from nuvix.analytics.sql.dialect import current_dialect


def _rows(sql: str, params: list[Any]) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


def channel_performance(
    start: dt.date, end: dt.date, as_of: dt.date | None = None
) -> list[dict[str, Any]]:
    """Cost and return per acquisition channel.

    Users are attributed to their **first touch**, and spend is summed over
    the same window, giving a CAC that is comparable across channels. Revenue
    is *realised* -- subscription months actually billed plus marketplace
    payouts actually earned -- not a projection, so payback is a fact rather
    than a forecast.

    The activation rate is included beside CAC deliberately: a channel that
    delivers cheap signups who never link an account is more expensive than
    its CAC suggests, and that is exactly the reallocation decision this
    report exists to inform.
    """

    dialect = current_dialect()
    as_of = as_of or end

    sql = f"""
        WITH first_touch AS (
            SELECT
                user_id,
                channel,
                campaign,
                ROW_NUMBER() OVER (
                    PARTITION BY user_id ORDER BY occurred_at, id
                ) AS touch_rank
            FROM analytics_touch_point
            WHERE user_id IS NOT NULL
        ),
        acquired AS (
            SELECT ft.user_id, ft.channel, u.date_joined
            FROM first_touch ft
            JOIN accounts_user u ON u.id = ft.user_id
            WHERE ft.touch_rank = 1
              AND u.date_joined >= %s
              AND u.date_joined < %s
        ),
        spend AS (
            SELECT
                channel,
                SUM(amount)      AS spend,
                SUM(clicks)      AS clicks,
                SUM(impressions) AS impressions
            FROM analytics_marketing_spend
            WHERE spend_on >= %s AND spend_on < %s
            GROUP BY channel
        ),
        activation AS (
            SELECT
                user_id,
                MAX(CASE WHEN step = 'account_linked' THEN 1 ELSE 0 END) AS linked,
                MAX(CASE WHEN step = 'plan_created'   THEN 1 ELSE 0 END) AS planned,
                MAX(CASE WHEN step = 'subscribed'     THEN 1 ELSE 0 END) AS subscribed
            FROM analytics_funnel_event
            WHERE user_id IS NOT NULL
            GROUP BY user_id
        ),
        -- ``as_of`` is bound once here as a column. It cannot be passed
        -- directly into months_between(), which references its arguments
        -- twice; see the warning in nuvix.analytics.sql.dialect.
        subscription_window AS (
            SELECT
                user_id,
                monthly_price,
                started_on,
                COALESCE(cancelled_on, %s) AS billed_until
            FROM analytics_subscription
        ),
        subscription_revenue AS (
            SELECT
                user_id,
                SUM(
                    monthly_price * (
                        {dialect.months_between("billed_until", "started_on")} + 1
                    )
                ) AS revenue
            FROM subscription_window
            GROUP BY user_id
        ),
        partner_revenue AS (
            SELECT user_id, SUM(amount) AS revenue
            FROM analytics_marketplace_revenue
            WHERE earned_on <= %s
            GROUP BY user_id
        )
        SELECT
            a.channel                                        AS channel,
            COUNT(DISTINCT a.user_id)                        AS acquired_users,
            COALESCE(SUM(act.linked), 0)                     AS linked_users,
            COALESCE(SUM(act.subscribed), 0)                 AS subscribed_users,
            COALESCE(MAX(s.spend), 0)                        AS spend,
            COALESCE(MAX(s.clicks), 0)                       AS clicks,
            COALESCE(SUM(sr.revenue), 0)
                + COALESCE(SUM(pr.revenue), 0)               AS revenue,
            {dialect.safe_divide("MAX(s.spend)", "COUNT(DISTINCT a.user_id)")}   AS cac,
            {dialect.safe_divide("SUM(act.linked)", "COUNT(DISTINCT a.user_id)")} AS activation_rate,
            {dialect.safe_divide("SUM(act.subscribed)", "COUNT(DISTINCT a.user_id)")}
                                                             AS subscribe_rate,
            {dialect.safe_divide(
                "COALESCE(SUM(sr.revenue), 0) + COALESCE(SUM(pr.revenue), 0)",
                "MAX(s.spend)",
            )}                                               AS roas
        FROM acquired a
        LEFT JOIN spend s                ON s.channel = a.channel
        LEFT JOIN activation act         ON act.user_id = a.user_id
        LEFT JOIN subscription_revenue sr ON sr.user_id = a.user_id
        LEFT JOIN partner_revenue pr     ON pr.user_id = a.user_id
        GROUP BY a.channel
        ORDER BY revenue DESC, acquired_users DESC
    """
    return _rows(sql, [start, end, start, end, as_of, as_of])


def cohort_retention(months: int = 12, as_of: dt.date | None = None) -> list[dict[str, Any]]:
    """Subscription retention by signup-month cohort.

    A cohort member counts as retained in month *n* if their subscription had
    started by then and had not been cancelled. Cancelled-and-resubscribed
    users are counted as retained, which is the definition a finance team
    recognises -- churn that reverses inside the month is not churn.

    Returned long rather than as a matrix: pivoting is presentation, and doing
    it in SQL bakes the number of columns into the query.

    Reading month 0
    ---------------
    Month 0 is usually *lower* than month 1, and that is not a bug. It counts
    only the cohort members who subscribed within their signup month; people
    who convert a few weeks later first appear in month 1. Retention curves
    from a free-signup product therefore rise before they fall, and the
    meaningful churn signal starts from the peak, not from month 0.
    """

    dialect = current_dialect()
    as_of = as_of or dt.date.today()

    sql = f"""
        WITH cohorts AS (
            SELECT
                u.id                          AS user_id,
                {dialect.month("u.date_joined")} AS cohort_month
            FROM accounts_user u
            WHERE u.date_joined >= %s
        ),
        sizes AS (
            SELECT cohort_month, COUNT(*) AS cohort_size
            FROM cohorts
            GROUP BY cohort_month
        ),
        -- ``as_of`` is bound once as a column for the same reason as above.
        spans AS (
            SELECT
                c.cohort_month,
                c.user_id,
                s.started_on,
                COALESCE(s.cancelled_on, %s) AS active_until
            FROM cohorts c
            JOIN analytics_subscription s ON s.user_id = c.user_id
        ),
        activity AS (
            SELECT
                cohort_month,
                user_id,
                {dialect.months_between("started_on", "cohort_month")} AS start_offset,
                {dialect.months_between("active_until", "cohort_month")} AS end_offset
            FROM spans
        ),
        offsets AS (
            SELECT DISTINCT start_offset AS month_offset FROM activity
            UNION
            SELECT DISTINCT end_offset FROM activity
        )
        SELECT
            a.cohort_month                        AS cohort_month,
            o.month_offset                        AS month_offset,
            z.cohort_size                         AS cohort_size,
            COUNT(DISTINCT a.user_id)             AS retained_users,
            {dialect.safe_divide("COUNT(DISTINCT a.user_id)", "z.cohort_size")}
                                                  AS retention_rate
        FROM activity a
        JOIN offsets o
          ON o.month_offset >= a.start_offset
         AND o.month_offset <= a.end_offset
        JOIN sizes z ON z.cohort_month = a.cohort_month
        WHERE o.month_offset >= 0 AND o.month_offset < %s
        GROUP BY a.cohort_month, o.month_offset, z.cohort_size
        ORDER BY a.cohort_month, o.month_offset
    """
    window_start = dt.date(as_of.year - 3, as_of.month, 1)
    return _rows(sql, [window_start, as_of, months])


def funnel_conversion(
    start: dt.date, end: dt.date, channel: str | None = None
) -> list[dict[str, Any]]:
    """Step-by-step activation funnel, optionally for one channel.

    Counts *distinct users who ever reached* each step rather than events, so
    the sequence is monotone and a step-to-step rate above 100% is impossible
    -- which is the usual symptom of counting events instead of people.
    """

    # The channel filter is appended as a clause rather than written as
    # ``(%s IS NULL OR ft.channel = %s)``. That trick reads well but hands
    # Postgres a bare NULL it cannot type-infer, and it also defeats the index
    # on ``channel``. Two shapes of query, one bound parameter each.
    params: list[Any] = [start, end]
    channel_clause = ""
    if channel:
        channel_clause = "AND ft.channel = %s"
        params.append(channel)

    sql = f"""
        WITH cohort AS (
            SELECT DISTINCT u.id AS user_id
            FROM accounts_user u
            LEFT JOIN (
                SELECT
                    user_id,
                    channel,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_id ORDER BY occurred_at, id
                    ) AS touch_rank
                FROM analytics_touch_point
                WHERE user_id IS NOT NULL
            ) ft ON ft.user_id = u.id AND ft.touch_rank = 1
            WHERE u.date_joined >= %s
              AND u.date_joined < %s
              {channel_clause}
        )
        SELECT
            e.step                    AS step,
            COUNT(DISTINCT e.user_id) AS users
        FROM analytics_funnel_event e
        JOIN cohort c ON c.user_id = e.user_id
        GROUP BY e.step
    """
    counts = {row["step"]: row["users"] for row in _rows(sql, params)}

    # Ordering the funnel is the caller's job, not the database's: the step
    # sequence is a product fact that lives with the enum.
    from nuvix.analytics.models import FunnelStep

    ordered = [choice for choice, _ in FunnelStep.choices]

    # Reaching a later step implies having passed the earlier ones, so each
    # stage counts users who reached it *or anything after it*. Without this,
    # a step that is not yet instrumented reads as zero, and the report then
    # shows a negative drop-off and a conversion rate above 100% at the next
    # stage -- which is how a funnel chart loses a reader's trust for good.
    # ``users_recorded`` is kept alongside so a gap in instrumentation stays
    # visible rather than being silently papered over.
    reached: dict[str, int] = {}
    running = 0
    for step in reversed(ordered):
        running = max(running, counts.get(step, 0))
        reached[step] = running

    results: list[dict[str, Any]] = []
    previous: int | None = None
    top = reached[ordered[0]]

    for step in ordered:
        users = reached[step]
        results.append(
            {
                "step": step,
                "users": users,
                "users_recorded": counts.get(step, 0),
                "step_conversion": round(users / previous, 4) if previous else None,
                "overall_conversion": round(users / top, 4) if top else None,
                "dropped": (previous - users) if previous is not None else None,
            }
        )
        previous = users
    return results


def attribution_comparison(start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    """First-touch versus last-touch credit, side by side.

    The two models disagree systematically -- discovery channels look strong
    on first touch, closing channels on last touch -- and a budget decision
    made on one model alone will over-fund whichever end of the journey that
    model favours. Showing both, with the gap, is the useful artefact.

    ``linear_conversions`` splits credit evenly across every touch a converting
    user had, as a third reading that does not privilege either end.
    """

    sql = """
        WITH converters AS (
            SELECT DISTINCT user_id
            FROM analytics_funnel_event
            WHERE step = 'subscribed'
              AND user_id IS NOT NULL
              AND occurred_at >= %s
              AND occurred_at < %s
        ),
        ranked AS (
            SELECT
                t.user_id,
                t.channel,
                ROW_NUMBER() OVER (
                    PARTITION BY t.user_id ORDER BY t.occurred_at ASC, t.id ASC
                ) AS first_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY t.user_id ORDER BY t.occurred_at DESC, t.id DESC
                ) AS last_rank,
                COUNT(*) OVER (PARTITION BY t.user_id) AS touch_count
            FROM analytics_touch_point t
            JOIN converters c ON c.user_id = t.user_id
        )
        SELECT
            channel                                              AS channel,
            SUM(CASE WHEN first_rank = 1 THEN 1 ELSE 0 END)      AS first_touch_conversions,
            SUM(CASE WHEN last_rank = 1 THEN 1 ELSE 0 END)       AS last_touch_conversions,
            SUM(1.0 / touch_count)                               AS linear_conversions,
            COUNT(*)                                             AS total_touches
        FROM ranked
        GROUP BY channel
        ORDER BY first_touch_conversions DESC
    """
    rows = _rows(sql, [start, end])
    for row in rows:
        first = row["first_touch_conversions"] or 0
        last = row["last_touch_conversions"] or 0
        row["attribution_gap"] = first - last
        # Not rounded here. Linear credit is a fraction per touch, and rounding
        # each channel independently makes the column stop summing to the total
        # conversions -- the first thing anyone checks in an attribution table.
        # Presentation rounds; the report stays exact.
        row["linear_conversions"] = float(row["linear_conversions"] or 0)
    return rows


def campaign_payback(start: dt.date, end: dt.date, as_of: dt.date | None = None):
    """Months of subscription revenue needed to repay each campaign's CAC.

    Payback, not LTV/CAC. A ratio flatters a channel whose revenue arrives
    over four years; payback months answers the question a cash-conscious
    company actually has, which is when the money comes back.
    """

    dialect = current_dialect()
    as_of = as_of or end

    sql = f"""
        WITH first_touch AS (
            SELECT
                user_id,
                channel,
                campaign,
                ROW_NUMBER() OVER (
                    PARTITION BY user_id ORDER BY occurred_at, id
                ) AS touch_rank
            FROM analytics_touch_point
            WHERE user_id IS NOT NULL
        ),
        acquired AS (
            SELECT ft.user_id, ft.channel, ft.campaign
            FROM first_touch ft
            JOIN accounts_user u ON u.id = ft.user_id
            WHERE ft.touch_rank = 1 AND u.date_joined >= %s AND u.date_joined < %s
        ),
        spend AS (
            SELECT channel, campaign, SUM(amount) AS spend
            FROM analytics_marketing_spend
            WHERE spend_on >= %s AND spend_on < %s
            GROUP BY channel, campaign
        ),
        mrr AS (
            SELECT user_id, SUM(monthly_price) AS monthly_revenue
            FROM analytics_subscription
            WHERE cancelled_on IS NULL OR cancelled_on > %s
            GROUP BY user_id
        )
        SELECT
            a.channel                                     AS channel,
            a.campaign                                    AS campaign,
            COUNT(DISTINCT a.user_id)                     AS acquired_users,
            COALESCE(MAX(s.spend), 0)                     AS spend,
            COALESCE(SUM(m.monthly_revenue), 0)           AS monthly_revenue,
            {dialect.safe_divide("MAX(s.spend)", "COUNT(DISTINCT a.user_id)")} AS cac,
            {dialect.safe_divide("MAX(s.spend)", "SUM(m.monthly_revenue)")}    AS payback_months
        FROM acquired a
        LEFT JOIN spend s ON s.channel = a.channel AND s.campaign = a.campaign
        LEFT JOIN mrr m   ON m.user_id = a.user_id
        GROUP BY a.channel, a.campaign
        ORDER BY payback_months
    """
    return _rows(sql, [start, end, start, end, as_of])
