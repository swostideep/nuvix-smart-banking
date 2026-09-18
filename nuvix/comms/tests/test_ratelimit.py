"""Tests for the token bucket."""

from __future__ import annotations

import pytest
from django.core.cache import cache

from nuvix.comms.services.ratelimit import BucketState, TokenBucket, consume, peek


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def bucket() -> TokenBucket:
    """Four messages per week."""

    return TokenBucket(capacity=4.0, period_seconds=7 * 24 * 3600)


class TestPureBucket:
    def test_a_fresh_bucket_is_full(self, bucket):
        assert bucket.fresh(now=0.0).tokens == 4.0

    def test_consuming_spends_a_token(self, bucket):
        allowed, state = bucket.try_consume(bucket.fresh(0.0), now=0.0)
        assert allowed is True
        assert state.tokens == 3.0

    def test_an_empty_bucket_refuses(self, bucket):
        allowed, _ = bucket.try_consume(BucketState(tokens=0.0, updated_at=0.0), now=0.0)
        assert allowed is False

    def test_capacity_is_the_burst_limit(self, bucket):
        state = bucket.fresh(0.0)
        for _ in range(4):
            allowed, state = bucket.try_consume(state, now=0.0)
            assert allowed
        allowed, _ = bucket.try_consume(state, now=0.0)
        assert allowed is False

    def test_tokens_refill_continuously(self, bucket):
        """The property a fixed window does not have."""

        empty = BucketState(tokens=0.0, updated_at=0.0)
        half_a_week = 3.5 * 24 * 3600
        refilled = bucket.refill(empty, now=half_a_week)
        assert refilled.tokens == pytest.approx(2.0)

    def test_refill_never_exceeds_capacity(self, bucket):
        empty = BucketState(tokens=0.0, updated_at=0.0)
        assert bucket.refill(empty, now=10**9).tokens == 4.0

    def test_seconds_until_is_zero_when_tokens_are_available(self, bucket):
        assert bucket.seconds_until(bucket.fresh(0.0), now=0.0) == 0.0

    def test_seconds_until_reports_the_wait(self, bucket):
        empty = BucketState(tokens=0.0, updated_at=0.0)
        expected = 7 * 24 * 3600 / 4
        assert bucket.seconds_until(empty, now=0.0) == pytest.approx(expected)

    def test_no_burst_across_a_window_boundary(self, bucket):
        """Eight sends in a few hours is exactly what a fixed window allows."""

        state = bucket.fresh(0.0)
        week = 7 * 24 * 3600
        # Spend the whole budget at the end of week one.
        for _ in range(4):
            allowed, state = bucket.try_consume(state, now=week - 60)
            assert allowed
        # A minute later -- "the next week" for a fixed window -- nothing is free.
        allowed, _ = bucket.try_consume(state, now=week + 60)
        assert allowed is False


class TestDisabledBucket:
    """Capacity zero is how an operator switches a channel off."""

    def test_a_zero_capacity_bucket_always_refuses(self):
        disabled = TokenBucket(capacity=0.0, period_seconds=604800)
        allowed, _ = disabled.try_consume(disabled.fresh(0.0), now=0.0)
        assert allowed is False

    def test_a_zero_capacity_bucket_never_refills(self):
        import math

        disabled = TokenBucket(capacity=0.0, period_seconds=604800)
        state = disabled.fresh(0.0)
        assert disabled.seconds_until(state, now=0.0) == math.inf

    def test_consume_reports_an_infinite_wait_rather_than_raising(self):
        disabled = TokenBucket(capacity=0.0, period_seconds=604800)
        allowed, retry = consume("test", "disabled-user", disabled, now=0.0)
        assert allowed is False
        assert retry == float("inf")


class TestCacheBackedConsume:
    def test_the_first_calls_are_allowed(self, bucket):
        for _ in range(4):
            allowed, retry = consume("test", "user-1", bucket, now=1000.0)
            assert allowed is True
            assert retry == 0.0

    def test_the_budget_runs_out(self, bucket):
        for _ in range(4):
            consume("test", "user-1", bucket, now=1000.0)
        allowed, retry = consume("test", "user-1", bucket, now=1000.0)
        assert allowed is False
        assert retry > 0

    def test_buckets_are_isolated_per_identifier(self, bucket):
        for _ in range(4):
            consume("test", "user-1", bucket, now=1000.0)
        allowed, _ = consume("test", "user-2", bucket, now=1000.0)
        assert allowed is True

    def test_buckets_are_isolated_per_scope(self, bucket):
        for _ in range(4):
            consume("comms", "user-1", bucket, now=1000.0)
        allowed, _ = consume("push", "user-1", bucket, now=1000.0)
        assert allowed is True

    def test_time_restores_the_budget(self, bucket):
        for _ in range(4):
            consume("test", "user-1", bucket, now=1000.0)
        later = 1000.0 + 7 * 24 * 3600 / 4 + 1
        allowed, _ = consume("test", "user-1", bucket, now=later)
        assert allowed is True

    def test_peek_does_not_spend(self, bucket):
        before = peek("test", "user-1", bucket, now=1000.0)
        after = peek("test", "user-1", bucket, now=1000.0)
        assert before == after == 4.0
        consume("test", "user-1", bucket, now=1000.0)
        assert peek("test", "user-1", bucket, now=1000.0) == 3.0
