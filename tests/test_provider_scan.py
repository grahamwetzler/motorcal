from pathlib import Path

import httpx

from motorcal.providers.thesportsdb import RateLimiter, scan_series_season

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "thesportsdb"


def _client_with_handler(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _fixture_handler(round_to_fixture: dict[int, str], failing_rounds: set[int] | None = None):
    failing_rounds = failing_rounds or set()

    def handler(request: httpx.Request) -> httpx.Response:
        round_number = int(request.url.params["r"])
        if round_number in failing_rounds:
            return httpx.Response(500, text="server error")
        if round_number in round_to_fixture:
            return httpx.Response(200, text=(FIXTURE_DIR / round_to_fixture[round_number]).read_text())
        return httpx.Response(200, text='{"events":null}')

    return handler


def test_scan_collects_events_across_all_rounds():
    handler = _fixture_handler({1: "wec_r1_2026.json", 3: "wec_r3_2026_hyperpole.json"})
    client = _client_with_handler(handler)

    result = scan_series_season(
        client, "3", 4413, "2026", max_round=4, series="wec",
        include_non_championship=False, rate_limiter=RateLimiter(rate_per_minute=6000),
    )

    assert result.complete is True
    assert result.rounds_attempted == 4  # rounds 1-4, no round 500
    assert len(result.events) == 3 + 5  # wec_r1 has 3 events, wec_r3_hyperpole has 5


def test_scan_fetches_round_500_only_when_non_championship_enabled():
    handler = _fixture_handler({500: "wec_r500_2026_prologue.json"})
    client = _client_with_handler(handler)

    without_nc = scan_series_season(
        client, "3", 4413, "2026", max_round=2, series="wec",
        include_non_championship=False, rate_limiter=RateLimiter(rate_per_minute=6000),
    )
    assert without_nc.rounds_attempted == 2
    assert len(without_nc.events) == 0

    with_nc = scan_series_season(
        client, "3", 4413, "2026", max_round=2, series="wec",
        include_non_championship=True, rate_limiter=RateLimiter(rate_per_minute=6000),
    )
    assert with_nc.rounds_attempted == 3  # rounds 1, 2, and 500
    assert len(with_nc.events) == 2  # the two prologue sessions


def test_scan_is_incomplete_when_a_round_fails_but_still_attempts_the_rest():
    handler = _fixture_handler({1: "wec_r1_2026.json", 3: "wec_r3_2026_hyperpole.json"}, failing_rounds={2})
    client = _client_with_handler(handler)

    result = scan_series_season(
        client, "3", 4413, "2026", max_round=4, series="wec",
        include_non_championship=False, rate_limiter=RateLimiter(rate_per_minute=6000),
        max_retries=0, sleep=lambda s: None,
    )

    assert result.complete is False
    assert result.rounds_attempted == 4
    assert len(result.diagnostics) == 1
    assert "round 2" in result.diagnostics[0]
    # rounds 1, 3, and 4 were still attempted and round 1/3's events are present,
    # even though the overall snapshot is marked incomplete (Phase 4 decides to discard them)
    assert len(result.events) == 3 + 5


def test_scan_treats_empty_rounds_as_successful_not_errors():
    handler = _fixture_handler({1: "indycar_r1_2026.json"})
    client = _client_with_handler(handler)

    result = scan_series_season(
        client, "3", 4373, "2026", max_round=5, series="indycar",
        include_non_championship=False, rate_limiter=RateLimiter(rate_per_minute=6000),
    )

    assert result.complete is True
    assert result.rounds_attempted == 5
    assert result.diagnostics == []
    assert len(result.events) == 1  # only round 1 (indycar_r1) has an event; rounds 2-5 are empty, not errors


def test_scan_real_world_shape_imsa_race_only_series():
    handler = _fixture_handler({1: "imsa_r1_2026.json", 500: "imsa_r500_2026_roar.json"})
    client = _client_with_handler(handler)

    result = scan_series_season(
        client, "3", 4488, "2026", max_round=2, series="imsa",
        include_non_championship=True, rate_limiter=RateLimiter(rate_per_minute=6000),
    )

    assert result.complete is True
    assert result.rounds_attempted == 3
    names = [e.name for e in result.events]
    assert "Rolex 24 At DAYTONA" in names
    assert "Roar Before The Rolex 24" in names


def test_scan_marks_incomplete_rather_than_raising_on_hostile_response_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"events": "not-a-list"}')

    client = _client_with_handler(handler)
    result = scan_series_season(
        client, "3", 4413, "2026", max_round=2, series="wec",
        include_non_championship=False, rate_limiter=RateLimiter(rate_per_minute=6000),
        max_retries=0, sleep=lambda s: None,
    )
    assert result.complete is False
    assert len(result.diagnostics) == 2  # both rounds hit the hostile shape


def test_scan_stops_issuing_requests_past_deadline():
    handler = _fixture_handler({1: "wec_r1_2026.json"})
    client = _client_with_handler(handler)

    fake_time = [0.0]
    def fake_clock():
        return fake_time[0]

    call_log = []
    real_handler = handler
    def counting_handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request.url.params["r"])
        fake_time[0] += 100  # simulate each request taking so long the deadline blows past immediately
        return real_handler(request)

    client = _client_with_handler(counting_handler)
    result = scan_series_season(
        client, "3", 4413, "2026", max_round=5, series="wec",
        include_non_championship=False, rate_limiter=RateLimiter(rate_per_minute=6000),
        deadline_seconds=50, clock=fake_clock, sleep=lambda s: None,
    )

    assert result.complete is False
    assert len(call_log) == 1  # only round 1 was actually requested before the deadline tripped
    assert any("deadline" in d for d in result.diagnostics)
