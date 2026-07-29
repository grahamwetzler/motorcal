from pathlib import Path

import httpx
import pytest

from motorcal.providers.thesportsdb import (
    ProviderError,
    ProviderEvent,
    build_client,
    fetch_round,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "thesportsdb"


class _NoOpRateLimiter:
    def acquire(self) -> None:
        pass


def _client_with_handler(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_round_parses_real_fixture_data():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(FIXTURE_DIR / "wec_r1_2026.json").read_text())

    client = _client_with_handler(handler)
    events = fetch_round(
        client, "3", 4413, "2026", 1, series="wec", rate_limiter=_NoOpRateLimiter()
    )

    assert len(events) == 3
    race = next(e for e in events if e.id_event == "2421035")
    assert race.name == "6 Hours of Imola"
    assert race.time == "00:00:00"
    assert race.series == "wec"
    assert race.round == 1


def test_fetch_round_preserves_en_dash_in_hyperpole_names():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(FIXTURE_DIR / "wec_r3_2026_hyperpole.json").read_text())

    client = _client_with_handler(handler)
    events = fetch_round(
        client, "3", 4413, "2026", 3, series="wec", rate_limiter=_NoOpRateLimiter()
    )

    hyperpole_names = [e.name for e in events if "Hyperpole" in e.name]
    assert hyperpole_names
    assert any("–" in n for n in hyperpole_names)  # U+2013, not a plain hyphen


def test_fetch_round_returns_empty_list_for_empty_round():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"events":null}')

    client = _client_with_handler(handler)
    events = fetch_round(
        client, "3", 4373, "2026", 42, series="indycar", rate_limiter=_NoOpRateLimiter()
    )

    assert events == []


def test_fetch_round_raises_on_malformed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json{{{")

    client = _client_with_handler(handler)
    with pytest.raises(ProviderError):
        fetch_round(
            client, "3", 4413, "2026", 1, series="wec",
            rate_limiter=_NoOpRateLimiter(), max_retries=0, sleep=lambda s: None,
        )


def test_fetch_round_raises_on_missing_required_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"events": [{"idEvent": "1", "strEvent": "Race"}]}')
        # missing idLeague, strSeason, dateEvent

    client = _client_with_handler(handler)
    with pytest.raises(ProviderError):
        fetch_round(
            client, "3", 4413, "2026", 1, series="wec",
            rate_limiter=_NoOpRateLimiter(), max_retries=0, sleep=lambda s: None,
        )


def test_fetch_round_retries_transient_failure_then_succeeds():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 2:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text=(FIXTURE_DIR / "wec_r500_2026_prologue.json").read_text())

    client = _client_with_handler(handler)
    sleeps = []
    events = fetch_round(
        client, "3", 4413, "2026", 500, series="wec",
        rate_limiter=_NoOpRateLimiter(), sleep=sleeps.append,
    )

    assert len(events) == 2
    assert len(sleeps) == 1  # exactly one retry wait before the second (successful) attempt


def test_fetch_round_respects_retry_after_header_on_429():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "5"}, text="rate limited")

    client = _client_with_handler(handler)
    sleeps = []
    with pytest.raises(ProviderError):
        fetch_round(
            client, "3", 4413, "2026", 1, series="wec",
            rate_limiter=_NoOpRateLimiter(), max_retries=1, sleep=sleeps.append,
        )

    assert 5 in sleeps  # Retry-After value was honored, not the exponential-backoff default


def test_fetch_round_raises_after_exhausting_retries_on_repeated_429():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    client = _client_with_handler(handler)
    with pytest.raises(ProviderError):
        fetch_round(
            client, "3", 4413, "2026", 1, series="wec",
            rate_limiter=_NoOpRateLimiter(), max_retries=2, sleep=lambda s: None,
        )


@pytest.mark.parametrize(
    "events_value",
    [
        '"oops"',
        "42",
        "[null]",
        '["a string"]',
        '[{"idEvent": "1"}]',  # a dict but missing required fields — already covered by validate_event, include for completeness
    ],
)
def test_fetch_round_raises_provider_error_not_something_else_for_hostile_events_shapes(events_value):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f'{{"events": {events_value}}}')

    client = _client_with_handler(handler)
    with pytest.raises(ProviderError):  # must be ProviderError specifically, not AttributeError/TypeError
        fetch_round(
            client, "3", 4413, "2026", 1, series="wec",
            rate_limiter=_NoOpRateLimiter(), max_retries=0, sleep=lambda s: None,
        )


def test_fetch_round_parses_http_date_retry_after_without_crashing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, text="rate limited")

    client = _client_with_handler(handler)
    sleeps = []
    with pytest.raises(ProviderError):
        fetch_round(
            client, "3", 4413, "2026", 1, series="wec",
            rate_limiter=_NoOpRateLimiter(), max_retries=1, sleep=sleeps.append,
        )
    assert len(sleeps) == 1
    assert sleeps[0] >= 0  # did not raise, did not go negative


def test_fetch_round_caps_excessive_retry_after_value():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "86400"}, text="rate limited")

    client = _client_with_handler(handler)
    sleeps = []
    with pytest.raises(ProviderError):
        fetch_round(
            client, "3", 4413, "2026", 1, series="wec",
            rate_limiter=_NoOpRateLimiter(), max_retries=1, sleep=sleeps.append,
        )
    assert sleeps[0] <= 60.0  # capped, not 86400


def test_fetch_round_rejects_negative_retry_after_without_crashing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "-5"}, text="rate limited")

    client = _client_with_handler(handler)
    sleeps = []
    with pytest.raises(ProviderError):
        fetch_round(
            client, "3", 4413, "2026", 1, series="wec",
            rate_limiter=_NoOpRateLimiter(), max_retries=1, sleep=sleeps.append,
        )
    assert sleeps[0] >= 0.0  # negative value was clamped, sleep() was never called with a negative number


def test_provider_event_is_hashable():
    ev = ProviderEvent(
        id_event="1", name="Race", date="2026-01-01", time=None, round=1,
        season="2026", series="wec", venue=None, country=None, raw={"a": 1},
    )
    hash(ev)  # must not raise
    {ev}  # must be usable in a set


def test_build_client_follows_redirects_and_sets_user_agent():
    client = build_client()
    try:
        assert client.follow_redirects is True
        assert "motorcal" in client.headers.get("user-agent", "")
    finally:
        client.close()


class _CountingRateLimiter:
    def __init__(self):
        self.acquire_count = 0

    def acquire(self) -> None:
        self.acquire_count += 1


def test_fetch_round_calls_rate_limiter_acquire_once_per_attempt_including_retries():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text='{"events":null}')

    client = _client_with_handler(handler)
    limiter = _CountingRateLimiter()
    fetch_round(
        client, "3", 4413, "2026", 1, series="wec",
        rate_limiter=limiter, sleep=lambda s: None,
    )
    assert limiter.acquire_count == 3  # one per attempt: 2 failures + 1 success
