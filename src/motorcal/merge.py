"""Publication rebuild logic: patch matching (this phase) and, in a later phase,
fingerprinting, sequencing, duration/alarm resolution, and cancellation lifecycle."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from motorcal.config import PatchConfig, RootConfig, SeriesConfig, SyntheticEventConfig, parse_duration
from motorcal.models import EventStatus, PublishedEvent, SessionType, SourceEvent, source_uid, synthetic_event_uid
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


@dataclass
class PreviousPublishedState:
    """A lightweight snapshot of what was previously published for one UID.

    Kept independent of sqlite3.Row so build_published_event_* stay pure and
    testable without a database — the caller (Task 4's orchestration) converts
    a fetched published_events row into this shape before calling in.
    """

    fingerprint: str
    sequence: int
    dtstamp: str
    last_modified: str
    status: str


def build_description(
    *,
    venue: str | None,
    country: str | None,
    round_number: int | None,
    race_only: bool,
    time_confirmed: bool,
    time_source: str,
    note: str | None,
) -> str:
    """Build the human-readable DESCRIPTION text for one published event."""
    lines: list[str] = []
    if venue:
        lines.append(f"Venue: {venue}")
    if country:
        lines.append(f"Country: {country}")
    if round_number is not None:
        lines.append(f"Round: {round_number}")
    lines.append("Source: TheSportsDB" if time_source != "synthetic" else "Source: local synthetic event")
    if race_only:
        lines.append("This series' feed includes race sessions only.")
    if time_source == "patch":
        lines.append("Time confirmed by local override.")
    elif time_source == "synthetic":
        lines.append("Time supplied by local synthetic event definition.")
    elif not time_confirmed:
        lines.append("Time not yet confirmed by the source (TBC).")
    else:
        lines.append("Time confirmed by source.")
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def _resolve_status(
    *,
    is_disappeared: bool,
    is_future_or_active: bool,
    patch_status: str | None,
    previous_status: EventStatus | None,
) -> EventStatus:
    """Cancellation is sticky: once CANCELLED, stay CANCELLED regardless of later rebuilds."""
    if is_disappeared:
        if previous_status == EventStatus.CANCELLED:
            return EventStatus.CANCELLED
        if is_future_or_active:
            return EventStatus.CANCELLED
        # A past event disappearing for the first time remains last-known-good, unchanged.
        return previous_status if previous_status is not None else EventStatus.CONFIRMED
    if patch_status is not None:
        return EventStatus(patch_status)
    return EventStatus.CONFIRMED


def build_published_event_from_source(
    *,
    source_event: SourceEvent,
    session_type: SessionType,
    is_disappeared: bool,
    matched_patch: PatchConfig | None,
    uid_domain: str,
    race_only: bool,
    series_config: SeriesConfig,
    root_config: RootConfig,
    previous: PreviousPublishedState | None,
    now: datetime,
) -> PublishedEvent:
    """Build (or rebuild) the published state for one source-backed event."""
    uid = source_uid(source_event.key.id_event, uid_domain)

    patched_start = matched_patch.start if matched_patch else None
    if patched_start:
        start_dt = datetime.fromisoformat(patched_start.replace("Z", "+00:00"))
        time_confirmed = (
            matched_patch.time_confirmed if matched_patch.time_confirmed is not None else True
        )
        time_source = "patch"
    else:
        if source_event.time is None or source_event.time == "00:00:00":
            time_confirmed = False
            start_dt = None
        else:
            start_dt = datetime.fromisoformat(f"{source_event.date}T{source_event.time}+00:00")
            time_confirmed = True
        time_source = "provider"

    summary = (matched_patch.summary if matched_patch and matched_patch.summary else source_event.name)
    location = (
        matched_patch.location
        if matched_patch and matched_patch.location
        else f"{source_event.venue}, {source_event.country}"
        if source_event.venue and source_event.country
        else source_event.venue or source_event.country
    )

    if not time_confirmed:
        summary = summary + root_config.unknown_time.summary_suffix
        all_day_date: str | None = source_event.date
        start: datetime | None = None
        duration_seconds: int | None = None
        alarms: list[str] = []
    else:
        all_day_date = None
        start = start_dt
        own_duration = parse_duration(matched_patch.duration) if matched_patch and matched_patch.duration else None
        duration_seconds = resolve_duration(
            session_type, own_duration_seconds=own_duration,
            series_config=series_config, root_config=root_config,
        )
        alarms = resolve_alarms(
            session_type, is_synthetic=False, own_alarms=None,
            time_confirmed=True, root_config=root_config,
        )

    is_future_or_active = _event_effective_end(start, all_day_date, duration_seconds) >= now
    patch_status = matched_patch.status if matched_patch else None
    previous_status = EventStatus(previous.status) if previous else None
    status = _resolve_status(
        is_disappeared=is_disappeared, is_future_or_active=is_future_or_active,
        patch_status=patch_status, previous_status=previous_status,
    )

    description = build_description(
        venue=source_event.venue, country=source_event.country, round_number=source_event.round,
        race_only=race_only, time_confirmed=time_confirmed, time_source=time_source,
        note=matched_patch.note if matched_patch else None,
    )

    fingerprint = compute_fingerprint(
        summary=summary, description=description, location=location, status=status.value,
        start=start.isoformat() if start else None, all_day_date=all_day_date,
        duration_seconds=duration_seconds, alarms=alarms,
    )

    now_unix_minute = int(now.timestamp() // 60)
    if previous is not None and previous.fingerprint == fingerprint:
        sequence = previous.sequence
        dtstamp = datetime.fromisoformat(previous.dtstamp)
        last_modified = datetime.fromisoformat(previous.last_modified)
    else:
        sequence = next_sequence(previous.sequence if previous else None, now_unix_minute)
        dtstamp = now
        last_modified = now

    return PublishedEvent(
        uid=uid, series=source_event.series, session_type=session_type, summary=summary,
        start=start, all_day_date=all_day_date, time_confirmed=time_confirmed,
        duration_seconds=duration_seconds, location=location, description=description,
        status=status, sequence=sequence, dtstamp=dtstamp, last_modified=last_modified,
        fingerprint=fingerprint, alarms=alarms, source_id_event=source_event.key.id_event,
        synthetic_uid=None,
    )


def build_published_event_from_synthetic(
    *,
    uid: str,
    series: str,
    summary: str,
    start: str | None,
    date: str | None,
    duration_seconds: int | None,
    location: str | None,
    note: str | None,
    alarms: list[str],
    is_cancelled: bool,
    uid_domain: str,
    root_config: RootConfig,
    previous: PreviousPublishedState | None,
    now: datetime,
) -> PublishedEvent:
    """Build (or rebuild) the published state for one synthetic event."""
    full_uid = synthetic_event_uid(uid, uid_domain)

    if start:
        start_dt: datetime | None = datetime.fromisoformat(start.replace("Z", "+00:00"))
        all_day_date: str | None = None
    else:
        start_dt = None
        all_day_date = date

    status = EventStatus.CANCELLED if is_cancelled else EventStatus.CONFIRMED

    description = build_description(
        venue=None, country=None, round_number=None, race_only=False,
        time_confirmed=True, time_source="synthetic", note=note,
    )

    fingerprint = compute_fingerprint(
        summary=summary, description=description, location=location, status=status.value,
        start=start_dt.isoformat() if start_dt else None, all_day_date=all_day_date,
        duration_seconds=duration_seconds, alarms=alarms,
    )

    now_unix_minute = int(now.timestamp() // 60)
    if previous is not None and previous.fingerprint == fingerprint:
        sequence = previous.sequence
        dtstamp = datetime.fromisoformat(previous.dtstamp)
        last_modified = datetime.fromisoformat(previous.last_modified)
    else:
        sequence = next_sequence(previous.sequence if previous else None, now_unix_minute)
        dtstamp = now
        last_modified = now

    return PublishedEvent(
        uid=full_uid, series=series, session_type=SessionType.RACE, summary=summary,
        start=start_dt, all_day_date=all_day_date, time_confirmed=True,
        duration_seconds=duration_seconds, location=location, description=description,
        status=status, sequence=sequence, dtstamp=dtstamp, last_modified=last_modified,
        fingerprint=fingerprint, alarms=alarms, source_id_event=None, synthetic_uid=uid,
    )


def _event_effective_end(
    start: datetime | None, all_day_date: str | None, duration_seconds: int | None
) -> datetime:
    """The last instant this event is considered 'happening', for retention/cancellation decisions."""
    from datetime import timedelta

    if start is not None:
        if duration_seconds:
            return start + timedelta(seconds=duration_seconds)
        return start
    day = datetime.fromisoformat(all_day_date).replace(tzinfo=timezone.utc)
    return day + timedelta(days=1)
