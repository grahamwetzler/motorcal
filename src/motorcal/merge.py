"""Turn configured events into published events, ready for ICS rendering.

Pure functions over a `Config` and the version ledger. `rebuild_publication`
mutates `state.versions` in place but touches no file, so callers get
all-or-nothing by rebuilding against a deep copy and persisting only on success.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from motorcal.config import (
    Config,
    EventConfig,
    GlobalConfig,
    RetentionConfig,
    SeriesConfig,
    SessionConfig,
    parse_duration,
)
from motorcal.models import EventStatus, PublishedEvent, SessionType, session_uid
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
        series_value = series_config.durations.get(session_type.value)
        if series_value is not None:
            return parse_duration(series_value)

    global_value = globals_.defaults.durations.get(session_type.value)
    return parse_duration(global_value) if global_value is not None else None


def resolve_alarms(
    session_type: SessionType,
    *,
    own_alarms: list[str] | None,
    time_confirmed: bool,
    series_config: SeriesConfig,
    globals_: GlobalConfig,
) -> list[str]:
    """Alarms only apply to confirmed, non-testing sessions.

    3-tier priority: the event's own > per-series > global. An explicit list on
    the event always wins, including an explicit empty one -- that is how you
    silence a single race without touching the series or global defaults.
    """
    if not time_confirmed or session_type is SessionType.TESTING:
        return []
    if own_alarms is not None:
        return list(own_alarms)
    if series_config.alerts is not None and session_type.value in series_config.alerts:
        return list(series_config.alerts[session_type.value])
    return list(globals_.defaults.alerts.get(session_type.value, []))


def build_description(*, event: EventConfig, session: SessionConfig) -> str:
    """Build the human-readable DESCRIPTION text for one published session.

    The event's `location` is not repeated here -- the VEVENT already carries it in
    its own LOCATION field.
    """
    lines: list[str] = []
    round_number = session.round if session.round is not None else event.round
    if round_number is not None:
        lines.append(f"Round: {round_number}")

    if session.tbc:
        lines.append("Start time not yet announced (TBC).")
    if session.note:
        lines.append(f"Note: {session.note}")
    return "\n".join(lines)


def _event_effective_end(
    start: datetime | None, all_day_date: str | None, duration_seconds: int | None
) -> datetime:
    """The last instant this event is 'happening', for retention/cancellation decisions."""
    if start is not None:
        return start + timedelta(seconds=duration_seconds) if duration_seconds else start
    day = datetime.fromisoformat(all_day_date).replace(tzinfo=timezone.utc)
    return day + timedelta(days=1)


def build_published_event(
    event: EventConfig,
    session: SessionConfig,
    *,
    series: str,
    series_config: SeriesConfig,
    globals_: GlobalConfig,
    previous: VersionState | None,
    now: datetime,
) -> PublishedEvent:
    """Build (or rebuild) the published state for one session of one race event."""
    uid = session_uid(session, globals_.uid_domain)
    session_type = session.type
    time_confirmed = session.start is not None

    # The published title: "{event} {session}", which the ICS layer prefixes with
    # the series name. A session with no label of its own is just the event.
    summary = " ".join(part for part in (event.name, session.label) if part)
    if session.tbc:
        # An all-day session whose time simply hasn't been announced yet, as opposed
        # to one that is deliberately all-day (a test day).
        summary += globals_.unknown_time.summary_suffix

    if time_confirmed:
        start: datetime | None = datetime.fromisoformat(session.start.replace("Z", "+00:00"))
        all_day_date: str | None = None
        duration_seconds = resolve_duration(
            session_type, own_duration=session.duration,
            series_config=series_config, globals_=globals_,
        )
        alarms = resolve_alarms(
            session_type, own_alarms=session.alarms, time_confirmed=True,
            series_config=series_config, globals_=globals_,
        )
    else:
        start, all_day_date = None, session.date
        duration_seconds, alarms = None, []

    status = EventStatus(session.status)
    description = build_description(event=event, session=session)
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
        fingerprint=fingerprint, alarms=alarms,
    )


def rebuild_publication(
    config: Config, state: State, *, now: datetime
) -> dict[str, list[PublishedEvent]]:
    """Rebuild every published event from the data directory and the version ledger.

    Mutates `state.versions` but writes nothing to disk, and never touches `config`:
    the data directory is read-only to this process.
    """
    published: dict[str, list[PublishedEvent]] = {}

    for series, series_config in config.series.items():
        published[series] = []
        for event, session in series_config.iter_sessions():
            built = build_published_event(
                event, session, series=series, series_config=series_config,
                globals_=config.globals,
                previous=state.versions.get(session_uid(session, config.globals.uid_domain)),
                now=now,
            )
            published[series].append(built)

    _prune_expired(config, published, now=now)

    for events in published.values():
        for built in events:
            state.versions[built.uid] = VersionState(
                fingerprint=built.fingerprint, sequence=built.sequence,
                dtstamp=built.dtstamp.isoformat(),
                last_modified=built.last_modified.isoformat(),
            )

    # The ledger exists to keep SEQUENCE stable for events a subscriber can still
    # see, so it is exactly the set of published UIDs. Anything else -- an expired
    # session, or one deleted from the data directory -- is dead weight that no
    # later rebuild would ever revisit to clean up.
    live_uids = {built.uid for events in published.values() for built in events}
    state.versions = {uid: v for uid, v in state.versions.items() if uid in live_uids}

    return published


def _prune_expired(
    config: Config, published: dict[str, list[PublishedEvent]], *, now: datetime
) -> None:
    """Drop sessions past their retention window from the feed."""
    retention: RetentionConfig = config.globals.retention

    for series, events in published.items():
        kept = []
        for built in events:
            effective_end = _event_effective_end(
                built.start, built.all_day_date, built.duration_seconds
            )
            days = (
                retention.cancelled_after_event_days
                if built.status == EventStatus.CANCELLED
                else retention.historical_days
            )
            # Still current/future, or inside the retention window: keep it.
            if effective_end >= now or now <= effective_end + timedelta(days=days):
                kept.append(built)
        published[series] = kept
