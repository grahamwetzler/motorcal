"""TheSportsDB provider: rate-limited fetch, parsing, and the complete-snapshot contract.

This module never touches the database. It returns validated, in-memory data;
Phase 4 decides whether and how to persist it.
"""
from __future__ import annotations

import email.utils
import httpx
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

_RETRY_AFTER_CAP_SECONDS = 60.0


def _parse_retry_after(value: str | None, *, fallback: float) -> float:
    """Parse a Retry-After header (delta-seconds or HTTP-date), clamped to a sane range."""
    if value is None:
        return fallback
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return fallback
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = (parsed - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(seconds, _RETRY_AFTER_CAP_SECONDS))


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

    raw_events = data["events"]
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        raise ProviderError(
            f"Expected a list for 'events' in round {round_number}, got {type(raw_events).__name__}"
        )

    events = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise ProviderError(f"Expected an event object in round {round_number}, got {raw!r}")
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
            if attempt < max_retries:
                sleep(min(2**attempt, 30) * random.uniform(1.0, 1.3))
            continue

        if response.status_code == 429:
            fallback = min(2**attempt, 30)
            wait = _parse_retry_after(response.headers.get("Retry-After"), fallback=fallback)
            last_error = ProviderError(f"Rate limited (429) on round {round_number}")
            if attempt < max_retries:
                sleep(wait)
            continue

        if response.status_code != 200:
            last_error = ProviderError(
                f"Unexpected status {response.status_code} for round {round_number}"
            )
            if attempt < max_retries:
                sleep(min(2**attempt, 30) * random.uniform(1.0, 1.3))
            continue

        return _parse_events(response.text, round_number, series)

    raise ProviderError(f"Exhausted retries fetching round {round_number}: {last_error}") from last_error


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
