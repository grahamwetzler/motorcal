"""Publication rebuild logic: patch matching (this phase) and, in a later phase,
fingerprinting, sequencing, duration/alarm resolution, and cancellation lifecycle."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from motorcal.config import PatchConfig, RootConfig, SeriesConfig, SyntheticEventConfig, parse_duration
from motorcal.models import SessionType, SourceEvent
from motorcal.store import (
    list_synthetic_events,
    mark_synthetic_event_removed,
    transaction,
    upsert_synthetic_event,
)


@dataclass
class PatchMatchError:
    """A patch that did not match exactly one source event."""

    patch: PatchConfig
    reason: str  # "no_match" or "multiple_matches"
    candidate_count: int


@dataclass
class MatchedPatch:
    """A patch successfully paired with the single source event it modifies."""

    patch: PatchConfig
    source_event: SourceEvent


def _find_candidates(patch: PatchConfig, source_events: list[SourceEvent]) -> list[SourceEvent]:
    if patch.id_event is not None:
        return [e for e in source_events if e.key.id_event == patch.id_event]

    matcher = patch.match
    assert matcher is not None  # config-schema validation (Phase 1) guarantees exactly one is set
    needle = matcher.contains.lower()
    return [
        e
        for e in source_events
        if e.series == matcher.series and e.date == matcher.date and needle in e.name.lower()
    ]


def match_all_patches(
    patches: list[PatchConfig], source_events: list[SourceEvent]
) -> tuple[list[MatchedPatch], list[PatchMatchError]]:
    """Match every patch against source_events, requiring exactly one candidate each."""
    matches: list[MatchedPatch] = []
    errors: list[PatchMatchError] = []

    for patch in patches:
        candidates = _find_candidates(patch, source_events)
        if len(candidates) == 1:
            matches.append(MatchedPatch(patch=patch, source_event=candidates[0]))
        elif len(candidates) == 0:
            errors.append(PatchMatchError(patch=patch, reason="no_match", candidate_count=0))
        else:
            errors.append(
                PatchMatchError(
                    patch=patch, reason="multiple_matches", candidate_count=len(candidates)
                )
            )

    return matches, errors


def reconcile_synthetic_events(
    conn: sqlite3.Connection, synthetic_configs: list[SyntheticEventConfig], now: str
) -> None:
    """Sync configured synthetic events into storage, marking removed ones.

    Every event currently in synthetic_configs is upserted (reactivating it if it
    was previously removed). Every stored synthetic event whose uid is NOT in
    synthetic_configs, and that is not already cancelled, is marked removed at `now`.
    """
    configured_uids = {cfg.uid for cfg in synthetic_configs}

    with transaction(conn):
        for cfg in synthetic_configs:
            upsert_synthetic_event(
                conn,
                uid=cfg.uid,
                series=cfg.series,
                summary=cfg.summary,
                start=cfg.start,
                date=cfg.date,
                duration_seconds=parse_duration(cfg.duration) if cfg.duration else None,
                location=cfg.location,
                status=cfg.status or "CONFIRMED",
                note=cfg.note,
                alarms_json=json.dumps(cfg.alarms),
            )

        for row in list_synthetic_events(conn):
            if row["uid"] not in configured_uids and row["cancelled_at"] is None:
                mark_synthetic_event_removed(conn, row["uid"], now)


def compute_fingerprint(
    *,
    summary: str,
    description: str,
    location: str | None,
    status: str,
    start: str | None,
    all_day_date: str | None,
    duration_seconds: int | None,
    alarms: list[str],
) -> str:
    """A stable digest over every client-visible VEVENT field. Alarm order never matters."""
    payload = {
        "summary": summary,
        "description": description,
        "location": location,
        "status": status,
        "start": start,
        "all_day_date": all_day_date,
        "duration_seconds": duration_seconds,
        "alarms": sorted(alarms),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def next_sequence(previous_sequence: int | None, now_unix_minute: int) -> int:
    """A new event may start at the current minute; an existing one must never regress."""
    if previous_sequence is None:
        return now_unix_minute
    return max(previous_sequence + 1, now_unix_minute)


def resolve_duration(
    session_type: SessionType,
    *,
    own_duration_seconds: int | None,
    series_config: SeriesConfig,
    root_config: RootConfig,
) -> int | None:
    """4-tier duration priority: own > per-series default > global default > None."""
    if own_duration_seconds is not None:
        return own_duration_seconds

    if series_config.durations is not None:
        series_value = getattr(series_config.durations, session_type.value, None)
        if series_value is not None:
            return parse_duration(series_value)

    global_value = getattr(root_config.defaults.durations, session_type.value, None)
    if global_value is not None:
        return parse_duration(global_value)

    return None


def resolve_alarms(
    session_type: SessionType,
    *,
    is_synthetic: bool,
    own_alarms: list[str] | None,
    time_confirmed: bool,
    root_config: RootConfig,
) -> list[str]:
    """Alarms only apply to confirmed, non-testing, non-unknown sessions."""
    if not time_confirmed or session_type in (SessionType.UNKNOWN, SessionType.TESTING):
        return []
    if is_synthetic:
        return list(own_alarms) if own_alarms is not None else []
    return list(root_config.defaults.alerts.get(session_type.value, []))
