import threading
import time as time_module

from motorcal.providers.thesportsdb import RateLimiter


def _fake_clock_and_sleep():
    """Returns (clock, sleep, advance) where sleep() advances the fake clock instead of blocking."""
    fake_time = [0.0]

    def clock():
        return fake_time[0]

    def sleep(seconds):
        fake_time[0] += seconds

    return clock, sleep, fake_time


def test_acquire_does_not_wait_while_tokens_available():
    clock, sleep, fake_time = _fake_clock_and_sleep()
    limiter = RateLimiter(rate_per_minute=60, capacity=2, clock=clock, sleep=sleep)

    limiter.acquire()
    limiter.acquire()

    assert fake_time[0] == 0.0  # both tokens were available immediately, no sleep needed


def test_acquire_waits_when_capacity_is_exhausted():
    clock, sleep, fake_time = _fake_clock_and_sleep()
    limiter = RateLimiter(rate_per_minute=60, capacity=2, clock=clock, sleep=sleep)

    limiter.acquire()
    limiter.acquire()
    limiter.acquire()  # capacity exhausted; at 60/min = 1 token/sec, must wait ~1s

    assert 0.9 <= fake_time[0] <= 1.1


def test_tokens_refill_over_time():
    clock, sleep, fake_time = _fake_clock_and_sleep()
    limiter = RateLimiter(rate_per_minute=60, capacity=1, clock=clock, sleep=sleep)

    limiter.acquire()  # consumes the only token
    fake_time[0] += 1.0  # simulate 1 second passing (a full refill at 1 token/sec)

    before = fake_time[0]
    limiter.acquire()  # token should already be available — no additional sleep
    assert fake_time[0] == before


def test_default_capacity_equals_rate_per_minute():
    clock, sleep, fake_time = _fake_clock_and_sleep()
    limiter = RateLimiter(rate_per_minute=28, clock=clock, sleep=sleep)

    for _ in range(28):
        limiter.acquire()

    assert fake_time[0] == 0.0  # a full minute's burst (28) should never need to wait

    limiter.acquire()  # the 29th call exhausts the default capacity
    assert fake_time[0] > 0.0


def test_rate_limiter_is_thread_safe_under_concurrent_acquire():
    limiter = RateLimiter(rate_per_minute=60, capacity=2)  # real clock/sleep, 1 token/sec after the burst
    start = time_module.monotonic()

    def worker():
        for _ in range(3):
            limiter.acquire()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elapsed = time_module.monotonic() - start
    # 12 total acquisitions; 2 are free (capacity), the remaining 10 must be paced at 1/sec.
    # A thread-safe limiter takes close to 10s; a racy one (double-granting tokens) finishes much faster.
    assert elapsed >= 9.0
