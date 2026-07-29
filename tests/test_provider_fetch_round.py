from pathlib import Path

import httpx
import pytest

from motorcal.providers.thesportsdb import ProviderError, RateLimiter, fetch_round

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
