"""Token bucket rate limiter (synchronous, thread-safe)."""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Simple token bucket: `capacity` tokens refilled at `refill_rate` / s."""

    def __init__(self, capacity: float, refill_rate: float, *, clock: object | None = None) -> None:
        """Initializes the bucket. `clock` (callable) enables deterministic tests."""
        if capacity <= 0 or refill_rate <= 0:
            raise ValueError("capacity and refill_rate must be > 0")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._tokens = float(capacity)
        self._clock = clock or time.monotonic
        self._last = self._clock()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Returns True and consumes tokens, or False (without blocking)."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, *, timeout: float | None = None) -> bool:
        """Blocks until tokens are consumed or until `timeout` (None = wait forever)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.try_acquire(tokens):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            deficit = tokens - self._tokens
            sleep_for = max(0.01, deficit / self.refill_rate)
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
            time.sleep(sleep_for)


# OKX v5 limits (per client instance):
# - private endpoints: 1000 req / 2 s = 500 req/s -> bucket 100, refill 100/s (with headroom)
# - public endpoints: 20 req / 2 s = 10 req/s -> bucket 20, refill 10/s
def make_private_bucket() -> TokenBucket:
    """Bucket for private endpoints (orders, balance)."""
    return TokenBucket(capacity=100, refill_rate=100)


def make_public_bucket() -> TokenBucket:
    """Bucket for public endpoints (tickers, candles)."""
    return TokenBucket(capacity=20, refill_rate=10)


def make_coingecko_bucket() -> TokenBucket:
    """Bucket for CoinGecko demo: 100 req/min ~= 1.67 req/s -> bucket 10, refill 1.6/s."""
    return TokenBucket(capacity=10, refill_rate=1.6)
