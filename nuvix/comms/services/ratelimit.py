"""Token-bucket rate limiting for outbound messages.

A money app earns attention slowly and loses it instantly. The guardrail that
matters is not "is this message true?" but "is this the fourth message this
week?" -- so every send passes a bucket before it passes a template.

A token bucket rather than a fixed window because the fixed window has a
well-known edge: a user capped at four per week can receive four on Sunday
night and four on Monday morning. The bucket refills continuously, so the
long-run rate holds no matter where the boundary falls, while still allowing
a small burst when something genuinely urgent coincides with a nudge.

The implementation is pure and clock-injectable; :func:`consume` is the only
function that touches shared state, and it does so through Django's cache so
that concurrent workers share one bucket per user.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from django.core.cache import cache

#: Bucket state is kept a little longer than a full refill so that an idle
#: user's bucket expires naturally at full capacity rather than being carried
#: forever.
_TTL_MULTIPLIER = 3


@dataclass(frozen=True, slots=True)
class BucketState:
    tokens: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class TokenBucket:
    """Capacity ``capacity``, refilling ``capacity`` tokens per ``period``."""

    capacity: float
    period_seconds: float

    @property
    def refill_per_second(self) -> float:
        return self.capacity / self.period_seconds

    def refill(self, state: BucketState, now: float) -> BucketState:
        elapsed = max(now - state.updated_at, 0.0)
        return BucketState(
            tokens=min(self.capacity, state.tokens + elapsed * self.refill_per_second),
            updated_at=now,
        )

    def try_consume(
        self, state: BucketState, now: float, cost: float = 1.0
    ) -> tuple[bool, BucketState]:
        """Attempt to spend ``cost`` tokens. Pure -- returns the new state."""

        refilled = self.refill(state, now)
        if refilled.tokens >= cost:
            return True, BucketState(tokens=refilled.tokens - cost, updated_at=now)
        return False, refilled

    def seconds_until(self, state: BucketState, now: float, cost: float = 1.0) -> float:
        """How long until ``cost`` tokens are available.

        A capacity of zero is a legitimate configuration -- it is how an
        operator turns a channel off entirely -- and it never refills, so the
        honest answer is infinity rather than a division by zero.
        """

        refilled = self.refill(state, now)
        if refilled.tokens >= cost:
            return 0.0
        if self.refill_per_second <= 0:
            return math.inf
        return (cost - refilled.tokens) / self.refill_per_second

    def fresh(self, now: float | None = None) -> BucketState:
        return BucketState(
            tokens=self.capacity, updated_at=now if now is not None else time.time()
        )


def _cache_key(scope: str, identifier: str) -> str:
    return f"nuvix:ratelimit:{scope}:{identifier}"


def consume(
    scope: str,
    identifier: str,
    bucket: TokenBucket,
    cost: float = 1.0,
    now: float | None = None,
) -> tuple[bool, float]:
    """Spend a token for ``identifier`` in ``scope``.

    Returns ``(allowed, retry_after_seconds)``.

    This is a read-modify-write against a shared cache and is therefore
    racy under concurrency: two workers can both read the same state and both
    allow a send. That is an accepted trade. The cost of the race is one extra
    message; the cost of avoiding it is a distributed lock on the hot path of
    every send. If the cap ever needs to be a hard guarantee rather than a
    guardrail, the correct fix is a Lua script on Redis that does the
    check-and-decrement atomically -- not a lock here.
    """

    now = now if now is not None else time.time()
    key = _cache_key(scope, identifier)

    raw = cache.get(key)
    state = (
        BucketState(tokens=raw["tokens"], updated_at=raw["updated_at"])
        if raw
        else bucket.fresh(now)
    )

    allowed, new_state = bucket.try_consume(state, now, cost)
    cache.set(
        key,
        {"tokens": new_state.tokens, "updated_at": new_state.updated_at},
        timeout=int(bucket.period_seconds * _TTL_MULTIPLIER),
    )

    if allowed:
        return True, 0.0
    return False, bucket.seconds_until(new_state, now, cost)


def peek(scope: str, identifier: str, bucket: TokenBucket, now: float | None = None) -> float:
    """Tokens currently available, without spending one."""

    now = now if now is not None else time.time()
    raw = cache.get(_cache_key(scope, identifier))
    state = (
        BucketState(tokens=raw["tokens"], updated_at=raw["updated_at"])
        if raw
        else bucket.fresh(now)
    )
    return bucket.refill(state, now).tokens
