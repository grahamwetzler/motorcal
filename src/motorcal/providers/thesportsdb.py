"""TheSportsDB provider: rate-limited fetch, parsing, and the complete-snapshot contract.

This module never touches the database. It returns validated, in-memory data;
Phase 4 decides whether and how to persist it.
"""
from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    """Token-bucket rate limiter. acquire() blocks until a token is available."""

    def __init__(
        self,
        rate_per_minute: float,
        *,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = capacity if capacity is not None else rate_per_minute
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._last_refill = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
        self._last_refill = now

    def acquire(self) -> None:
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return
        deficit = 1 - self._tokens
        wait_time = deficit / self._rate_per_second
        self._sleep(wait_time)
        self._refill()
        self._tokens -= 1
