"""Token bucket tests with an injected clock (no real sleeping)."""

from __future__ import annotations

import pytest

from okxbot.client.rate_limiter import TokenBucket


class FakeClock:
    """Deterministic monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_initial_capacity_allows_burst() -> None:
    clock = FakeClock()
    bucket = TokenBucket(capacity=3, refill_rate=1, clock=clock)
    assert all(bucket.try_acquire() for _ in range(3))
    assert bucket.try_acquire() is False


def test_refill_after_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(capacity=2, refill_rate=2, clock=clock)
    bucket.try_acquire()
    bucket.try_acquire()
    assert bucket.try_acquire() is False
    clock.advance(0.5)
    assert bucket.try_acquire() is True


def test_refill_capped_at_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket(capacity=2, refill_rate=100, clock=clock)
    clock.advance(60)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_rate=1)
    with pytest.raises(ValueError):
        TokenBucket(capacity=1, refill_rate=0)


def test_fractional_tokens() -> None:
    clock = FakeClock()
    bucket = TokenBucket(capacity=10, refill_rate=1, clock=clock)
    assert bucket.try_acquire(2.5) is True
    assert bucket.try_acquire(8) is False
