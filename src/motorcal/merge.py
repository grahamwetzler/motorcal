"""Turn configured events into published events, ready for ICS rendering.

Pure functions over a `Config` and the version ledger. `rebuild_publication`
mutates `state.versions` in place but touches no file, so callers get
all-or-nothing by rebuilding against a deep copy and persisting only on success.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from motorcal.classify import classify_event
from motorcal.config import (
    Config,
    EventConfig,
    GlobalConfig,
    RetentionConfig,
    SeriesConfig,
    parse_duration,
)
from motorcal.models import EventStatus, PublishedEvent, SessionType, event_uid
from motorcal.state import State, VersionState


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
    own_duration: str | None,
    series_config: SeriesConfig,
    globals_: GlobalConfig,
) -> int | None:
    """4-tier duration priority: the event's own > per-series > global > none."""
    if own_duration is not None:
        return parse_duration(own_duration)

    if series_config.durations is not None:
        series_value = getattr(series_config.durations, session_type.value, None)
        if series_value is not None:
            return parse_duration(series_value)

    global_value = getattr(globals_.defaults.durations, session_type.value, None)
    return parse_duration(global_value) if global_value is not None else None


def resolve_alarms(
    session_type: SessionType,
    *,
    own_alarms: list[str] | None,
    time_confirmed: bool,
    series_config: SeriesConfig,
    globals_: GlobalConfig,
) -> list[str]:
    """Alarms only apply to confirmed, non-testing, non-unknown sessions.

    3-tier priority: the event's own > per-series > global. An explicit list on
    the event always wins, including an explicit empty one -- that is how you
    silence a single race without touching the series or global defaults.
    """
    if not time_confirmed or session_type in (SessionType.UNKNOWN, SessionType.TESTING):
        return []
    if own_alarms is not None:
        return list(own_alarms)
    if series_config.alerts is not None and session_type.value in series_config.alerts:
        return list(series_config.alerts[session_type.value])
    return list(globals_.defaults.alerts.get(session_type.value, []))


def build_description(
    *,
    event: EventConfig,
    race_only: bool,
    time_confirmed: bool,
) -> str:
    """Build the human-readable DESCRIPTION text for one published event."""
    source = event.source
    lines: list[str] = []
    if source and source.venue:
        lines.append(f"Venue: {source.venue}")
    if source and source.country:
        lines.append(f"Country: {source.country}")
    if event.round is not None:
        lines.append(f"Round: {event.round}")

    lines.append("Source: TheSportsDB" if source else "Source: local event")
    if race_only:
        lines.append("This series' feed includes race sessions only.")

    if source is None:
        lines.append("Time supplied by local event definition.")
    elif not time_confirmed:
        lines.append("Time not yet confirmed by the source (TBC).")
    else:
        lines.append("Time confirmed by source.")

    if event.note:
        lines.append(f"Note: {event.note}")
    return "\n".join(lines)


def _event_effective_end(
    start: datetime | None, all_day_date: str | None, duration_seconds: int | None
) -> datetime:
    """The last instant this event is 'happening', for retention/cancellation decisions."""
    if start is not None:
        return start + timedelta(seconds=duration_seconds) if duration_seconds else start
    day = datetime.fromisoformat(all_day_date).replace(tzinfo=timezone.utc)
    return day + timedelta(days=1)


def _resolve_status(
    *,
    is_disappeared: bool,
    is_future_or_active: bool,
    configured_status: EventStatus,
    previous_status: EventStatus | None,
) -> EventStatus:
    """Cancellation is sticky: once CANCELLED, stay CANCELLED regardless of later rebuilds."""
    if is_disappeared:
        if previous_status == EventStatus.CANCELLED:
            return EventStatus.CANCELLED
        if is_future_or_active:
            return EventStatus.CANCELLED
        # A past event disappearing for the first time stays last-known-good.
        return previous_status if previous_status is not None else configured_status
    return configured_status


def build_published_event(
    event: EventConfig,
    *,
    series: str,
    series_config: SeriesConfig,
    globals_: GlobalConfig,
    previous: VersionState | None,
    now: datetime,
) -> PublishedEvent:
    """Build (or rebuild) the published state for one configured event."""
    uid = event_uid(event, globals_.uid_domain)
    session_type = classify_event(series, event.summary, event.round or 0)
    time_confirmed = event.start is not None

    summary = event.summary
    if not time_confirmed and event.source is not None:
        # The provider hasn't announced a time. A manual all-day event is
        # deliberate, so it never gets the suffix.
        summary += globals_.unknown_time.summary_suffix

    if time_confirmed:
        start: datetime | None = datetime.fromisoformat(event.start.replace("Z", "+00:00"))
        all_day_date: str | None = None
        duration_seconds = resolve_duration(
            session_type, own_duration=event.duration,
            series_config=series_config, globals_=globals_,
        )
        alarms = resolve_alarms(
            session_type, own_alarms=event.alarms, time_confirmed=True,
            series_config=series_config, globals_=globals_,
        )
    else:
        start, all_day_date = None, event.date
        duration_seconds, alarms = None, []

    is_future_or_active = _event_effective_end(start, all_day_date, duration_seconds) >= now
    status = _resolve_status(
        is_disappeared=event.disappeared_at is not None,
        is_future_or_active=is_future_or_active,
        configured_status=EventStatus(event.status),
        previous_status=EventStatus(previous.status) if previous else None,
    )

    description = build_description(
        event=event, race_only=series_config.race_only, time_confirmed=time_confirmed
    )
    fingerprint = compute_fingerprint(
        summary=summary, description=description, location=event.location, status=status.value,
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
        dtstamp = last_modified = now

    return PublishedEvent(
        uid=uid, series=series, session_type=session_type, summary=summary,
        start=start, all_day_date=all_day_date, time_confirmed=time_confirmed,
        duration_seconds=duration_seconds, location=event.location, description=description,
        status=status, sequence=sequence, dtstamp=dtstamp, last_modified=last_modified,
        fingerprint=fingerprint, alarms=alarms, event_key=event.key,
    )


@dataclass
class RebuildReport:
    events_published: int
    events_cancelled: int
    events_pruned: int
    unknown_events: list[str]


def rebuild_publication(
    config: Config, state: State, *, now: datetime
) -> tuple[dict[str, list[PublishedEvent]], RebuildReport]:
    """Rebuild every published event from the config directory and the version ledger.

    Mutates `state.versions` (and prunes expired entries from both the config and
    the ledger) but writes nothing to disk.
    """
    published: dict[str, list[PublishedEvent]] = {}
    unknown_events: list[str] = []

    for series, series_config in config.series.items():
        published[series] = []
        for event in series_config.events:
            built = build_published_event(
                event, series=series, series_config=series_config, globals_=config.globals,
                previous=state.versions.get(event_uid(event, config.globals.uid_domain)),
                now=now,
            )
            published[series].append(built)
            if built.session_type == SessionType.UNKNOWN:
                unknown_events.append(built.uid)

    for events in published.values():
        for built in events:
            state.versions[built.uid] = VersionState(
                fingerprint=built.fingerprint, sequence=built.sequence,
                dtstamp=built.dtstamp.isoformat(),
                last_modified=built.last_modified.isoformat(), status=built.status.value,
            )

    events_pruned = _prune_expired(config, state, published, now=now)

    all_events = [e for events in published.values() for e in events]
    report = RebuildReport(
        events_published=len(all_events),
        events_cancelled=sum(1 for e in all_events if e.status == EventStatus.CANCELLED),
        events_pruned=events_pruned,
        unknown_events=unknown_events,
    )
    return published, report


def _prune_expired(
    config: Config,
    state: State,
    published: dict[str, list[PublishedEvent]],
    *,
    now: datetime,
) -> int:
    """Drop events past their retention window from the config, the ledger, and the feed."""
    retention: RetentionConfig = config.globals.retention
    pruned = 0

    for series, events in published.items():
        expired_uids: set[str] = set()
        expired_keys: set[str] = set()
        for built in events:
            effective_end = _event_effective_end(
                built.start, built.all_day_date, built.duration_seconds
            )
            if effective_end >= now:
                continue  # still current/future -- never prune

            days = (
                retention.cancelled_after_event_days
                if built.status == EventStatus.CANCELLED
                else retention.historical_days
            )
            if now > effective_end + timedelta(days=days):
                expired_uids.add(built.uid)
                expired_keys.add(built.event_key)

        if not expired_uids:
            continue

        published[series] = [e for e in events if e.uid not in expired_uids]
        config.series[series].events = [
            e for e in config.series[series].events if e.key not in expired_keys
        ]
        for uid in expired_uids:
            state.versions.pop(uid, None)
        pruned += len(expired_uids)

    return pruned
