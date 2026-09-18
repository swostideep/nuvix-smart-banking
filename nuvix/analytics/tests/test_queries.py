"""Tests for the raw-SQL marketing analytics.

These run against SQLite while production runs Postgres, which is the whole
reason :mod:`nuvix.analytics.sql.dialect` exists. The assertions check real
arithmetic -- a CAC of exactly $50, a retention rate of exactly 0.5 -- because
a reporting query that returns plausible-looking wrong numbers is worse than
one that errors.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from nuvix.accounts.models import User
from nuvix.analytics import queries
from nuvix.analytics.models import (
    Channel,
    FunnelEvent,
    FunnelStep,
    MarketingSpend,
    Subscription,
    TouchPoint,
)
from nuvix.analytics.sql.dialect import current_dialect

pytestmark = pytest.mark.django_db

WINDOW_START = dt.date(2026, 1, 1)
WINDOW_END = dt.date(2026, 4, 1)


def make_user(email: str, joined: dt.date) -> User:
    user = User.objects.create_user(email=email, password="a-long-enough-password")
    User.objects.filter(pk=user.pk).update(
        date_joined=timezone.make_aware(dt.datetime.combine(joined, dt.time(12, 0)))
    )
    user.refresh_from_db()
    return user


def touch(user: User, channel: str, when: dt.date, campaign: str = "spring") -> TouchPoint:
    return TouchPoint.objects.create(
        user=user,
        channel=channel,
        campaign=campaign,
        occurred_at=timezone.make_aware(dt.datetime.combine(when, dt.time(9, 0))),
    )


def event(user: User, step: str, when: dt.date) -> FunnelEvent:
    return FunnelEvent.objects.create(
        user=user,
        step=step,
        occurred_at=timezone.make_aware(dt.datetime.combine(when, dt.time(10, 0))),
    )


class TestDialect:
    def test_month_truncation_works_on_this_backend(self):
        from django.db import connection

        sql = f"""
            WITH one AS (SELECT %s AS when_it_happened)
            SELECT {current_dialect().month("when_it_happened")} FROM one
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, [dt.date(2026, 3, 17)])
            result = cursor.fetchone()[0]
        assert str(result).startswith("2026-03-01")

    @staticmethod
    def _months_between(later: dt.date, earlier: dt.date) -> int:
        """Evaluate the fragment against columns, never against placeholders.

        Binding through a CTE is the documented contract for every fragment
        that references its arguments more than once.
        """

        from django.db import connection

        dialect = current_dialect()
        sql = f"""
            WITH pair AS (SELECT %s AS later_date, %s AS earlier_date)
            SELECT {dialect.months_between("later_date", "earlier_date")} FROM pair
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, [later, earlier])
            return cursor.fetchone()[0]

    def test_months_between_counts_calendar_months(self):
        assert self._months_between(dt.date(2026, 6, 1), dt.date(2026, 1, 1)) == 5

    def test_months_between_spans_a_year_boundary(self):
        assert self._months_between(dt.date(2027, 2, 1), dt.date(2026, 11, 1)) == 3

    def test_months_between_is_zero_within_one_month(self):
        assert self._months_between(dt.date(2026, 3, 28), dt.date(2026, 3, 1)) == 0

    def test_months_between_goes_negative_when_reversed(self):
        assert self._months_between(dt.date(2026, 1, 1), dt.date(2026, 4, 1)) == -3

    def test_safe_divide_returns_null_on_a_zero_denominator(self):
        from django.db import connection

        dialect = current_dialect()
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {dialect.safe_divide('10', '0')}")
            assert cursor.fetchone()[0] is None


class TestChannelPerformance:
    def test_cac_is_spend_over_acquired_users(self):
        for index in range(4):
            user = make_user(f"paid{index}@example.com", dt.date(2026, 1, 10))
            touch(user, Channel.PAID_SEARCH, dt.date(2026, 1, 9))
        MarketingSpend.objects.create(
            channel=Channel.PAID_SEARCH,
            campaign="spring",
            spend_on=dt.date(2026, 1, 5),
            amount=Decimal("200.00"),
        )
        rows = queries.channel_performance(WINDOW_START, WINDOW_END)
        row = next(r for r in rows if r["channel"] == Channel.PAID_SEARCH)
        assert row["acquired_users"] == 4
        assert float(row["cac"]) == pytest.approx(50.0)

    def test_users_are_credited_to_their_first_touch(self):
        user = make_user("multi@example.com", dt.date(2026, 2, 1))
        touch(user, Channel.CONTENT, dt.date(2026, 1, 20))
        touch(user, Channel.PAID_SOCIAL, dt.date(2026, 1, 28))

        rows = {r["channel"]: r for r in queries.channel_performance(WINDOW_START, WINDOW_END)}
        assert rows[Channel.CONTENT]["acquired_users"] == 1
        assert Channel.PAID_SOCIAL not in rows

    def test_activation_rate_counts_linked_accounts(self):
        for index in range(4):
            user = make_user(f"act{index}@example.com", dt.date(2026, 1, 10))
            touch(user, Channel.REFERRAL, dt.date(2026, 1, 9))
            if index < 3:
                event(user, FunnelStep.ACCOUNT_LINKED, dt.date(2026, 1, 12))

        row = next(
            r
            for r in queries.channel_performance(WINDOW_START, WINDOW_END)
            if r["channel"] == Channel.REFERRAL
        )
        assert row["linked_users"] == 3
        assert float(row["activation_rate"]) == pytest.approx(0.75)

    def test_revenue_counts_billed_subscription_months(self):
        user = make_user("sub@example.com", dt.date(2026, 1, 5))
        touch(user, Channel.EMAIL, dt.date(2026, 1, 4))
        Subscription.objects.create(
            user=user,
            plan=Subscription.Plan.PLUS,
            monthly_price=Decimal("10.00"),
            started_on=dt.date(2026, 1, 10),
            cancelled_on=dt.date(2026, 3, 10),
        )
        row = next(
            r
            for r in queries.channel_performance(WINDOW_START, WINDOW_END)
            if r["channel"] == Channel.EMAIL
        )
        # January, February, March -> three billed months at $10.
        assert float(row["revenue"]) == pytest.approx(30.0)

    def test_a_channel_with_no_spend_still_appears(self):
        user = make_user("organic@example.com", dt.date(2026, 1, 15))
        touch(user, Channel.ORGANIC_SEARCH, dt.date(2026, 1, 14))
        row = next(
            r
            for r in queries.channel_performance(WINDOW_START, WINDOW_END)
            if r["channel"] == Channel.ORGANIC_SEARCH
        )
        assert row["acquired_users"] == 1
        assert row["cac"] is None  # not a crash, and not a fake zero

    def test_the_window_is_half_open(self):
        """A user joining on the end date belongs to the next window."""

        edge = make_user("edge@example.com", WINDOW_END)
        touch(edge, Channel.DIRECT, dt.date(2026, 3, 30))
        rows = queries.channel_performance(WINDOW_START, WINDOW_END)
        assert all(r["channel"] != Channel.DIRECT for r in rows)

    def test_no_data_returns_an_empty_list_not_an_error(self):
        assert queries.channel_performance(WINDOW_START, WINDOW_END) == []


class TestFunnel:
    def test_steps_are_reported_in_product_order(self):
        rows = queries.funnel_conversion(WINDOW_START, WINDOW_END)
        assert [r["step"] for r in rows] == [c for c, _ in FunnelStep.choices]

    def test_conversion_rates_are_computed_between_steps(self):
        for index in range(10):
            user = make_user(f"f{index}@example.com", dt.date(2026, 1, 10))
            touch(user, Channel.PAID_SEARCH, dt.date(2026, 1, 9))
            event(user, FunnelStep.SIGNUP, dt.date(2026, 1, 10))
            if index < 5:
                event(user, FunnelStep.ACCOUNT_LINKED, dt.date(2026, 1, 11))

        rows = {r["step"]: r for r in queries.funnel_conversion(WINDOW_START, WINDOW_END)}
        assert rows[FunnelStep.SIGNUP]["users"] == 10
        assert rows[FunnelStep.ACCOUNT_LINKED]["users"] == 5
        # The five who never linked are lost at the first step that can prove
        # it -- PROFILE_COMPLETE, which nothing instruments here.
        assert rows[FunnelStep.PROFILE_COMPLETE]["dropped"] == 5
        assert rows[FunnelStep.SIGNUP]["overall_conversion"] == 1.0

    def test_an_uninstrumented_step_does_not_break_monotonicity(self):
        """No PROFILE_COMPLETE events are recorded, yet the funnel must still
        read sensibly rather than reporting a negative drop-off."""

        for index in range(10):
            user = make_user(f"g{index}@example.com", dt.date(2026, 1, 10))
            touch(user, Channel.PAID_SEARCH, dt.date(2026, 1, 9))
            event(user, FunnelStep.SIGNUP, dt.date(2026, 1, 10))
            if index < 5:
                event(user, FunnelStep.ACCOUNT_LINKED, dt.date(2026, 1, 11))

        rows = {r["step"]: r for r in queries.funnel_conversion(WINDOW_START, WINDOW_END)}

        # Nothing records this step, but five users provably got past it,
        # so the stage counts those five rather than a misleading zero.
        assert rows[FunnelStep.PROFILE_COMPLETE]["users_recorded"] == 0
        assert rows[FunnelStep.PROFILE_COMPLETE]["users"] == 5
        assert all(
            r["dropped"] is None or r["dropped"] >= 0
            for r in queries.funnel_conversion(WINDOW_START, WINDOW_END)
        )
        assert all(
            r["step_conversion"] is None or r["step_conversion"] <= 1.0
            for r in queries.funnel_conversion(WINDOW_START, WINDOW_END)
        )

    def test_the_funnel_is_monotone(self):
        for index in range(6):
            user = make_user(f"m{index}@example.com", dt.date(2026, 1, 10))
            touch(user, Channel.PAID_SEARCH, dt.date(2026, 1, 9))
            event(user, FunnelStep.SIGNUP, dt.date(2026, 1, 10))
            if index < 4:
                event(user, FunnelStep.ACCOUNT_LINKED, dt.date(2026, 1, 11))
            if index < 2:
                event(user, FunnelStep.SUBSCRIBED, dt.date(2026, 1, 20))

        rows = queries.funnel_conversion(WINDOW_START, WINDOW_END)
        counts = [r["users"] for r in rows]
        assert counts == sorted(counts, reverse=True)

    def test_filtering_by_channel(self):
        paid = make_user("paid@example.com", dt.date(2026, 1, 10))
        touch(paid, Channel.PAID_SEARCH, dt.date(2026, 1, 9))
        event(paid, FunnelStep.SIGNUP, dt.date(2026, 1, 10))

        organic = make_user("org@example.com", dt.date(2026, 1, 10))
        touch(organic, Channel.ORGANIC_SEARCH, dt.date(2026, 1, 9))
        event(organic, FunnelStep.SIGNUP, dt.date(2026, 1, 10))

        rows = {
            r["step"]: r
            for r in queries.funnel_conversion(
                WINDOW_START, WINDOW_END, channel=Channel.PAID_SEARCH
            )
        }
        assert rows[FunnelStep.SIGNUP]["users"] == 1


class TestAttribution:
    def test_first_and_last_touch_disagree_as_expected(self):
        user = make_user("journey@example.com", dt.date(2026, 2, 1))
        touch(user, Channel.CONTENT, dt.date(2026, 1, 10))
        touch(user, Channel.PAID_SEARCH, dt.date(2026, 1, 20))
        touch(user, Channel.EMAIL, dt.date(2026, 1, 30))
        event(user, FunnelStep.SUBSCRIBED, dt.date(2026, 2, 2))

        rows = {
            r["channel"]: r for r in queries.attribution_comparison(WINDOW_START, WINDOW_END)
        }
        assert rows[Channel.CONTENT]["first_touch_conversions"] == 1
        assert rows[Channel.CONTENT]["last_touch_conversions"] == 0
        assert rows[Channel.EMAIL]["last_touch_conversions"] == 1
        assert rows[Channel.EMAIL]["first_touch_conversions"] == 0

    def test_linear_credit_splits_evenly_across_touches(self):
        user = make_user("linear@example.com", dt.date(2026, 2, 1))
        for channel in (Channel.CONTENT, Channel.PAID_SEARCH, Channel.EMAIL):
            touch(user, channel, dt.date(2026, 1, 10))
        event(user, FunnelStep.SUBSCRIBED, dt.date(2026, 2, 2))

        rows = queries.attribution_comparison(WINDOW_START, WINDOW_END)
        assert sum(r["linear_conversions"] for r in rows) == pytest.approx(1.0)
        assert all(r["linear_conversions"] == pytest.approx(1 / 3) for r in rows)

    def test_the_gap_is_reported(self):
        user = make_user("gap@example.com", dt.date(2026, 2, 1))
        touch(user, Channel.CONTENT, dt.date(2026, 1, 10))
        touch(user, Channel.EMAIL, dt.date(2026, 1, 30))
        event(user, FunnelStep.SUBSCRIBED, dt.date(2026, 2, 2))

        rows = {
            r["channel"]: r for r in queries.attribution_comparison(WINDOW_START, WINDOW_END)
        }
        assert rows[Channel.CONTENT]["attribution_gap"] == 1
        assert rows[Channel.EMAIL]["attribution_gap"] == -1

    def test_non_converters_are_excluded(self):
        user = make_user("browser@example.com", dt.date(2026, 2, 1))
        touch(user, Channel.CONTENT, dt.date(2026, 1, 10))
        assert queries.attribution_comparison(WINDOW_START, WINDOW_END) == []


class TestCohortRetention:
    def test_retention_rate_is_retained_over_cohort_size(self):
        for index in range(4):
            user = make_user(f"c{index}@example.com", dt.date(2026, 1, 10))
            if index < 2:
                Subscription.objects.create(
                    user=user,
                    plan=Subscription.Plan.STARTER,
                    monthly_price=Decimal("8.00"),
                    started_on=dt.date(2026, 1, 15),
                )

        rows = queries.cohort_retention(months=6, as_of=dt.date(2026, 3, 1))
        month_zero = next(r for r in rows if r["month_offset"] == 0)
        assert month_zero["cohort_size"] == 4
        assert month_zero["retained_users"] == 2
        assert float(month_zero["retention_rate"]) == pytest.approx(0.5)

    def test_a_cancelled_subscription_stops_being_retained(self):
        user = make_user("churn@example.com", dt.date(2026, 1, 10))
        Subscription.objects.create(
            user=user,
            plan=Subscription.Plan.STARTER,
            monthly_price=Decimal("8.00"),
            started_on=dt.date(2026, 1, 15),
            cancelled_on=dt.date(2026, 2, 20),
        )
        rows = {
            r["month_offset"]: r
            for r in queries.cohort_retention(months=6, as_of=dt.date(2026, 5, 1))
        }
        assert rows[0]["retained_users"] == 1
        assert rows[1]["retained_users"] == 1
        assert 3 not in rows or rows[3]["retained_users"] == 0

    def test_no_subscriptions_returns_an_empty_report(self):
        make_user("nobody@example.com", dt.date(2026, 1, 10))
        assert queries.cohort_retention(months=6, as_of=dt.date(2026, 3, 1)) == []


class TestCampaignPayback:
    def test_payback_months_is_spend_over_monthly_revenue(self):
        for index in range(2):
            user = make_user(f"p{index}@example.com", dt.date(2026, 1, 10))
            touch(user, Channel.PAID_SOCIAL, dt.date(2026, 1, 9), campaign="q1-brand")
            Subscription.objects.create(
                user=user,
                plan=Subscription.Plan.PLUS,
                monthly_price=Decimal("10.00"),
                started_on=dt.date(2026, 1, 15),
            )
        MarketingSpend.objects.create(
            channel=Channel.PAID_SOCIAL,
            campaign="q1-brand",
            spend_on=dt.date(2026, 1, 3),
            amount=Decimal("100.00"),
        )
        row = queries.campaign_payback(WINDOW_START, WINDOW_END)[0]
        assert float(row["cac"]) == pytest.approx(50.0)
        # $100 spend against $20/month of recurring revenue.
        assert float(row["payback_months"]) == pytest.approx(5.0)


class TestDebugModeRendering:
    """Regression cover for a DEBUG-only crash.

    Django's SQLite backend renders the debug query with ``sql % params``, so
    a literal percent sign in the statement -- the obvious ``strftime('%Y')``
    -- raises ``ValueError: unsupported format character``. It passes with
    DEBUG off and fails the moment a developer opens the report locally, which
    is the worst possible place for a bug to hide.
    """

    @pytest.fixture
    def debug_on(self, settings):
        settings.DEBUG = True
        from django.db import connection

        connection.queries_log.clear()
        connection.force_debug_cursor = True
        yield
        connection.force_debug_cursor = False

    def test_every_report_renders_under_debug(self, debug_on):
        user = make_user("debug@example.com", dt.date(2026, 1, 10))
        touch(user, Channel.PAID_SEARCH, dt.date(2026, 1, 9))
        event(user, FunnelStep.SUBSCRIBED, dt.date(2026, 1, 20))
        Subscription.objects.create(
            user=user,
            plan=Subscription.Plan.PLUS,
            monthly_price=Decimal("10.00"),
            started_on=dt.date(2026, 1, 15),
        )

        # Each of these executes raw SQL with bound parameters; none may raise.
        assert queries.channel_performance(WINDOW_START, WINDOW_END)
        assert queries.cohort_retention(months=6, as_of=dt.date(2026, 3, 1))
        assert queries.funnel_conversion(WINDOW_START, WINDOW_END)
        assert queries.attribution_comparison(WINDOW_START, WINDOW_END)
        assert queries.campaign_payback(WINDOW_START, WINDOW_END) is not None

    def test_no_report_sql_contains_a_bare_percent_sign(self):
        """The rule the fix relies on, asserted directly on the fragments."""

        dialect = current_dialect()
        fragments = [
            dialect.month("col"),
            dialect.day("col"),
            dialect.months_between("a", "b"),
            dialect.safe_divide("a", "b"),
        ]
        for fragment in fragments:
            assert "%" not in fragment.replace("%%", ""), fragment


class TestSafeDivideParenthesisation:
    """Regression cover for a silent reporting bug.

    ``safe_divide`` originally interpolated its arguments bare, so a compound
    numerator lost all but its last term to operator precedence. ROAS was
    computed as ``subscription_revenue + partner_revenue / spend`` and reported
    numbers in the hundreds where the true ratio was under one.
    """

    def _evaluate(self, numerator: str, denominator: str) -> float:
        from django.db import connection

        sql = f"SELECT {current_dialect().safe_divide(numerator, denominator)}"
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchone()[0]

    def test_a_compound_numerator_divides_as_a_whole(self):
        assert self._evaluate("10 + 30", "4") == pytest.approx(10.0)

    def test_a_compound_denominator_divides_as_a_whole(self):
        assert self._evaluate("100", "2 + 3") == pytest.approx(20.0)

    def test_a_zero_denominator_yields_null(self):
        assert self._evaluate("10 + 30", "0") is None

    def test_roas_is_revenue_over_spend(self):
        """The end-to-end assertion: the ratio must be < 1 when spend exceeds
        revenue, not a number in the hundreds."""

        user = make_user("roas@example.com", dt.date(2026, 1, 10))
        touch(user, Channel.PAID_SEARCH, dt.date(2026, 1, 9))
        Subscription.objects.create(
            user=user,
            plan=Subscription.Plan.PLUS,
            monthly_price=Decimal("10.00"),
            started_on=dt.date(2026, 1, 15),
            cancelled_on=dt.date(2026, 2, 15),
        )
        MarketingSpend.objects.create(
            channel=Channel.PAID_SEARCH,
            campaign="spring",
            spend_on=dt.date(2026, 1, 5),
            amount=Decimal("200.00"),
        )

        row = next(
            r
            for r in queries.channel_performance(WINDOW_START, WINDOW_END)
            if r["channel"] == Channel.PAID_SEARCH
        )
        # Two billed months at $10 against $200 of spend.
        assert float(row["revenue"]) == pytest.approx(20.0)
        assert float(row["roas"]) == pytest.approx(20.0 / 200.0)
