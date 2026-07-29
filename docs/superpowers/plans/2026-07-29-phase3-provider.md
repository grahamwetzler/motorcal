# Motorsports Calendar — Phase 3: Provider Bounded Round Scan + Snapshot Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/motorcal/providers/thesportsdb.py`: a pure fetch-and-validate layer for TheSportsDB's `eventsround.php` endpoint, with a token-bucket rate limiter, bounded round scanning per `{series, season}`, and the "complete snapshot" contract the spec requires — an incomplete or suspicious scan must never be able to corrupt stored state.

**Architecture:** This module has **no dependency on `store.py`** and performs no database writes — it only fetches, rate-limits, retries, parses, and validates, returning an in-memory `SnapshotResult`. Classifying events into session types and writing them into `source_events` is Phase 4's job; this phase's output is exactly what Phase 4 will consume as input. This separation is deliberate: it lets this phase be tested entirely against captured fixtures and a mocked HTTP transport, with zero real network calls in the test suite.

**Tech Stack:** `httpx.Client` (already a dependency from Phase 1) for HTTP, `httpx.MockTransport` for tests (no real network in the suite), stdlib `json`/`time`/`dataclasses`. Reuses the real TheSportsDB fixture corpus captured in Phase 1 (`tests/fixtures/thesportsdb/`).

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous.
- Phases 1-2 (already complete) produced `src/motorcal/models.py`, `src/motorcal/config.py`, and `src/motorcal/store.py`. This phase does not modify any of them and does not call any `store.py` function.
- For each enabled `{series, season}`, request every round from `1` through `max_round`. Round `500` is fetched only when non-championship events are enabled (`include_non_championship: true` in config).
- Do not infer completeness from consecutive empty rounds — an empty round (TheSportsDB returns `{"events": null}` for a round with nothing scheduled) is a normal, successful response, not an error and not evidence the scan is done.
- A snapshot is complete only when every planned round request in the scan returns a valid, parseable response. Requests use bounded timeouts and retries. Any exhausted error, 429, or malformed response makes that `{series, season}` snapshot incomplete — but the scan still attempts every remaining round (for full diagnostics), it does not abort early.
- Use a configurable token-bucket limiter, defaulting to 28 requests per minute. On 429, respect `Retry-After` when present, apply bounded exponential backoff, and abandon that round (contributing to an incomplete snapshot) after the retry budget for that round is exhausted.
- Parse responses with `json.loads(..., strict=False)` because source descriptions may contain raw control characters. Validate required identity (`idEvent`), series (`idLeague`), season (`strSeason`), date (`dateEvent`), and name (`strEvent`) fields before staging — treat a response containing an event missing any of these as malformed (contributing to an incomplete snapshot), the same as a JSON parse failure.
- This phase does not decide what to do with an incomplete or suspicious-empty snapshot (discard vs. commit) — that decision, and the actual database write, belongs to Phase 4 (which will call this phase's `scan_series_season` and inspect the returned `SnapshotResult.complete` flag before writing anything). This phase only needs to make that flag trustworthy.
- No pip: dependency management is `uv` only.

---

### Task 1: Token-bucket rate limiter

**Files:**
- Create: `src/motorcal/providers/thesportsdb.py`
- Test: `tests/test_provider_rate_limiter.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (used by Tasks 2-3 and later by Phase 9's scheduler):
  - `class RateLimiter` — constructor `RateLimiter(rate_per_minute: float, *, capacity: float | None = None, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)`. `capacity` defaults to `rate_per_minute` (a full minute's burst). `def acquire(self) -> None` blocks (via the injected `sleep`) until a token is available, then consumes one token.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_provider_rate_limiter.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_provider_rate_limiter.py -v`
Expected: FAIL / collection error — `motorcal.providers.thesportsdb` does not exist yet.

- [ ] **Step 3: Write `src/motorcal/providers/thesportsdb.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_provider_rate_limiter.py -v`
Expected: PASS, 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/motorcal/providers/thesportsdb.py tests/test_provider_rate_limiter.py
git commit -m "Add token-bucket rate limiter for the TheSportsDB provider"
```

---

### Task 2: Fetch, parse, validate, and retry a single round

**Files:**
- Modify: `src/motorcal/providers/thesportsdb.py`
- Test: `tests/test_provider_fetch_round.py`

**Interfaces:**
- Consumes: `RateLimiter` from Task 1.
- Produces (used by Task 3 and by Phase 4):
  - `class ProviderError(Exception)` — raised when a round's response is malformed/invalid, or retries are exhausted.
  - `@dataclass(frozen=True) class ProviderEvent` fields: `id_event: str`, `name: str`, `date: str`, `time: str | None`, `round: int`, `season: str`, `series: str`, `venue: str | None`, `country: str | None`, `raw: dict`. `series` is the **config series key** (e.g. `"wec"`), not TheSportsDB's numeric `idLeague` — Phase 4's classifier selects its regex rule set by this field.
  - `def fetch_round(client: httpx.Client, api_key: str, league_id: int, season: str, round_number: int, *, series: str, rate_limiter: RateLimiter, timeout: float = 10.0, max_retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> list[ProviderEvent]` — fetches one round, retries on network error/429/non-200 with bounded exponential backoff (respecting `Retry-After` on 429), and raises `ProviderError` if retries are exhausted. Returns `[]` for an empty round (`{"events": null}`), which is a successful, non-error result.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_provider_fetch_round.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_provider_fetch_round.py -v`
Expected: FAIL / collection error — `ProviderError`, `ProviderEvent`, `fetch_round` do not exist yet.

- [ ] **Step 3: Append to `src/motorcal/providers/thesportsdb.py`**

Add `import json` to the top of the file's imports, alongside the existing `time`/`Callable` imports. Add `from dataclasses import dataclass` and `import httpx`. Append to the end of the file:

```python
class ProviderError(Exception):
    """Raised when a round's response is malformed/invalid, or retries are exhausted."""


@dataclass(frozen=True)
class ProviderEvent:
    """A validated event as reported by TheSportsDB for one round."""

    id_event: str
    name: str
    date: str
    time: str | None
    round: int
    season: str
    series: str
    venue: str | None
    country: str | None
    raw: dict


_REQUIRED_EVENT_FIELDS = ("idEvent", "idLeague", "strSeason", "dateEvent", "strEvent")


def _validate_event(raw: dict) -> None:
    for field in _REQUIRED_EVENT_FIELDS:
        if not raw.get(field):
            raise ProviderError(f"Event missing required field {field!r}: {raw!r}")


def _parse_events(response_text: str, round_number: int, series: str) -> list[ProviderEvent]:
    try:
        data = json.loads(response_text, strict=False)
    except ValueError as exc:
        raise ProviderError(f"Malformed JSON response for round {round_number}: {exc}") from exc

    if not isinstance(data, dict) or "events" not in data:
        raise ProviderError(f"Unexpected response shape for round {round_number}: {data!r}")

    raw_events = data["events"] or []
    events = []
    for raw in raw_events:
        _validate_event(raw)
        events.append(
            ProviderEvent(
                id_event=raw["idEvent"],
                name=raw["strEvent"],
                date=raw["dateEvent"],
                time=raw.get("strTime") or None,
                round=round_number,
                season=raw["strSeason"],
                series=series,
                venue=raw.get("strVenue") or None,
                country=raw.get("strCountry") or None,
                raw=raw,
            )
        )
    return events


def fetch_round(
    client: httpx.Client,
    api_key: str,
    league_id: int,
    season: str,
    round_number: int,
    *,
    series: str,
    rate_limiter: RateLimiter,
    timeout: float = 10.0,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ProviderEvent]:
    """Fetch and validate one round's events. Raises ProviderError if retries are exhausted."""
    url = f"https://www.thesportsdb.com/api/v1/json/{api_key}/eventsround.php"
    params = {"id": league_id, "r": round_number, "s": season}

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        rate_limiter.acquire()
        try:
            response = client.get(url, params=params, timeout=timeout)
        except httpx.HTTPError as exc:
            last_error = exc
            sleep(min(2**attempt, 30))
            continue

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else min(2**attempt, 30)
            last_error = ProviderError(f"Rate limited (429) on round {round_number}")
            sleep(wait)
            continue

        if response.status_code != 200:
            last_error = ProviderError(
                f"Unexpected status {response.status_code} for round {round_number}"
            )
            sleep(min(2**attempt, 30))
            continue

        return _parse_events(response.text, round_number, series)

    raise ProviderError(f"Exhausted retries fetching round {round_number}: {last_error}") from last_error
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_provider_fetch_round.py -v`
Expected: PASS, 8 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-2 (88) plus Task 1 (4) plus Task 2 (8) pass — 100 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/providers/thesportsdb.py tests/test_provider_fetch_round.py
git commit -m "Add fetch_round: parsing, required-field validation, and 429/error retry with backoff"
```

---

### Task 3: Bounded round scan and the complete-snapshot contract

**Files:**
- Modify: `src/motorcal/providers/thesportsdb.py`
- Test: `tests/test_provider_scan.py`

**Interfaces:**
- Consumes: `RateLimiter`, `ProviderError`, `ProviderEvent`, `fetch_round` from Tasks 1-2.
- Produces (used by Phase 4):
  - `@dataclass class SnapshotResult` fields: `complete: bool`, `events: list[ProviderEvent]`, `diagnostics: list[str]`, `rounds_attempted: int`.
  - `def scan_series_season(client: httpx.Client, api_key: str, league_id: int, season: str, max_round: int, *, series: str, include_non_championship: bool, rate_limiter: RateLimiter, timeout: float = 10.0, max_retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> SnapshotResult` — scans rounds `1..max_round` plus round `500` (only if `include_non_championship`), collecting every round's events. A round that raises `ProviderError` is recorded in `diagnostics` and flips `complete` to `False`, but scanning continues through the remaining rounds so `/status` (a later phase) can show a full diagnostic picture rather than stopping at the first failure.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_provider_scan.py
from pathlib import Path

import httpx

from motorcal.providers.thesportsdb import RateLimiter, scan_series_season

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "thesportsdb"


class _NoOpRateLimiter:
    def acquire(self) -> None:
        pass


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_provider_scan.py -v`
Expected: FAIL / collection error — `SnapshotResult`, `scan_series_season` do not exist yet.

- [ ] **Step 3: Append to `src/motorcal/providers/thesportsdb.py`**

```python
@dataclass
class SnapshotResult:
    """The outcome of scanning every planned round for one {series, season}."""

    complete: bool
    events: list[ProviderEvent]
    diagnostics: list[str]
    rounds_attempted: int


def scan_series_season(
    client: httpx.Client,
    api_key: str,
    league_id: int,
    season: str,
    max_round: int,
    *,
    series: str,
    include_non_championship: bool,
    rate_limiter: RateLimiter,
    timeout: float = 10.0,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> SnapshotResult:
    """Scan rounds 1..max_round (plus 500 if include_non_championship) for one series/season."""
    rounds = list(range(1, max_round + 1))
    if include_non_championship:
        rounds.append(500)

    events: list[ProviderEvent] = []
    diagnostics: list[str] = []
    complete = True

    for round_number in rounds:
        try:
            round_events = fetch_round(
                client,
                api_key,
                league_id,
                season,
                round_number,
                series=series,
                rate_limiter=rate_limiter,
                timeout=timeout,
                max_retries=max_retries,
                sleep=sleep,
            )
            events.extend(round_events)
        except ProviderError as exc:
            complete = False
            diagnostics.append(f"round {round_number}: {exc}")

    return SnapshotResult(
        complete=complete,
        events=events,
        diagnostics=diagnostics,
        rounds_attempted=len(rounds),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_provider_scan.py -v`
Expected: PASS, 5 passed.

- [ ] **Step 5: Run the entire test suite**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-2 and Phase 3 Tasks 1-3 pass — 105 passed total (88 + 4 + 8 + 5).

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/providers/thesportsdb.py tests/test_provider_scan.py
git commit -m "Add bounded round scan and the complete-snapshot contract"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: token-bucket rate limiter defaulting to 28/min (Rate limiting and concurrency section); bounded round scanning 1..max_round plus conditional round 500 (Bounded round scanning section); complete-snapshot contract — any error/429/malformed response marks the snapshot incomplete without inferring completeness from empty rounds (same section); `json.loads(strict=False)` parsing and required-field validation (Parsing section); 429 handling with `Retry-After` and bounded exponential backoff (Rate limiting and concurrency section).
- Explicitly out of scope for this phase (Phase 4 owns them): deciding whether to commit an incomplete/suspicious-empty snapshot, writing to `source_events`, classification, disappearance reconciliation, and the actual per-`{provider,series,season}` "was there previously populated data" check (that check needs `store.py`'s `source_snapshot_meta` table, which this phase never touches). This phase's contract is deliberately narrow: given a scan, tell the truth about whether it was complete and what it found.
- Design decision worth flagging explicitly: this module fetches synchronously and blocks on rate-limit waits and retry backoff via a real (or injected) `sleep`. This is intentional — the refresh scan runs from a background scheduler tick (Phase 9), never from an HTTP request-handling path (the spec requires "Serving never calls the upstream API"), so blocking I/O here has no request-latency impact.
- Type consistency check: `ProviderEvent.series` is the config series key (e.g. `"wec"`), not TheSportsDB's `idLeague` int — Phase 4's classifier will select its per-series regex rule set using this field, and Phase 4's `SourceEvent.series` (from `models.py`, Phase 1) should be populated from this same value, not re-derived from `idLeague`.
