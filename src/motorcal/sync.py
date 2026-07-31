"""Fold a provider snapshot into a series' race events without clobbering hand edits.

The provider has no notion of a race weekend: it reports one flat event per
session. This module groups a round's provider events into one `EventConfig` --
name, location and round stored once -- with one `SessionConfig` per session.

Three-way merge, per field: a session's `source` records what the provider said on
the previous fetch. A field is only overwritten when the provider actually changed
it *and* the stored value still matches what the provider said before. Any value
you edited by hand has diverged from that baseline, so it always wins. That holds
for the event-level fields too, whose baseline is derived from the whole group's
previous snapshots.

Manual sessions (no `source`) are never touched.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from motorcal.classify import classify_session
from motorcal.config import EventConfig, SeriesConfig, SessionConfig, SourceSnapshot
from motorcal.providers.thesportsdb import ProviderEvent, SnapshotResult

# The fields a provider fetch can own. Everything else (duration, status, note,
# alarms) is yours alone and is never synced.
DERIVED_EVENT_FIELDS = ("name", "location", "round")

_LABEL_SEPARATORS = " -–—:"


def _snapshot_of(event: ProviderEvent) -> SourceSnapshot:
    return SourceSnapshot(
        name=event.name, date=event.date, time=event.time,
        venue=event.venue, country=event.country, round=event.round, season=event.season,
    )


def _location(source: SourceSnapshot) -> str | None:
    if source.venue and source.country:
        return f"{source.venue}, {source.country}"
    return source.venue or source.country


def event_name_from(session_names: list[str]) -> str:
    """The weekend's name, given the names of its sessions.

    It is the session name that prefixes every other one -- the race, which the
    provider names after the weekend itself.
    """
    names = sorted(session_names)
    # ponytail: falls back to the shortest name when no session name prefixes the
    # rest (a provider that names its race unlike its sessions). Hand-edit `name:`
    # if that guess is wrong -- the merge below will keep your value.
    prefixes = [name for name in names if all(other.startswith(name) for other in names)]
    return min(prefixes or names, key=len)


def derive_event(sources: list[SourceSnapshot]) -> dict:
    """The event-level values a round's provider snapshots imply, before any local edit.

    The location is the most complete one any session reported, since the provider
    routinely omits the venue on some sessions of a weekend it gave in full on others.
    """
    locations = [text for text in map(_location, sources) if text]
    return {
        "name": event_name_from([source.name for source in sources]),
        "location": max(locations, key=len) if locations else None,
        "round": sources[0].round,
    }


def derive_session(source: SourceSnapshot, event_name: str) -> dict:
    """The session-level values one provider snapshot implies, before any local edit.

    A missing or midnight time means the provider hasn't announced one yet, which
    is published as an all-day event rather than a guess at 00:00.
    """
    if source.name.startswith(event_name):
        label = source.name[len(event_name):].strip(_LABEL_SEPARATORS)
    else:
        # Not named after its own weekend; classify off the whole name instead and
        # let the session type supply the label.
        label = source.name

    session_type = classify_session(label, source.round)

    if source.time in (None, "", "00:00:00"):
        start, date = None, source.date
    else:
        start, date = f"{source.date}T{source.time}+00:00", None

    return {
        "label": label or session_type.value.title(),
        "type": session_type,
        "start": start,
        "date": date,
    }


def _take_changed(target, was: dict, now: dict, fields: tuple[str, ...]) -> list[str]:
    """Apply every field the provider changed and the local value hasn't diverged from."""
    taken: list[str] = []
    for field in fields:
        if was[field] != now[field] and getattr(target, field) == was[field]:
            setattr(target, field, now[field])
            taken.append(field)
    return taken


def merge_event(event: EventConfig, old_sources: list[SourceSnapshot],
                new_sources: list[SourceSnapshot]) -> list[str]:
    """Apply a round's fresh snapshots to the event holding them. Returns fields taken."""
    if not old_sources:
        return []  # nothing upstream said before, so nothing to have diverged from
    return _take_changed(
        event, derive_event(old_sources), derive_event(new_sources), DERIVED_EVENT_FIELDS
    )


def merge_session(session: SessionConfig, new_source: SourceSnapshot, *,
                  was_event_name: str, now_event_name: str) -> list[str]:
    """Apply a fresh provider snapshot to one stored session. Returns the fields taken.

    `start` and `date` are merged as a pair: they are two encodings of one fact
    (is the time known?), and treating them independently could leave a session
    with both set, or neither.
    """
    if session.source is None:
        return []  # manual session: nothing upstream owns any of its fields

    was = derive_session(session.source, was_event_name)
    now = derive_session(new_source, now_event_name)
    taken = _take_changed(session, was, now, ("label", "type"))

    timing_changed = (was["start"], was["date"]) != (now["start"], now["date"])
    timing_untouched = (session.start, session.date) == (was["start"], was["date"])
    if timing_changed and timing_untouched:
        session.start, session.date = now["start"], now["date"]
        taken.append("start" if now["start"] else "date")

    session.source = new_source
    return taken


def session_from_source(source: SourceSnapshot, id_event: str, event_name: str) -> SessionConfig:
    """Build a brand-new session from a provider snapshot -- nothing local to preserve."""
    values = derive_session(source, event_name)
    return SessionConfig(
        id_event=id_event,
        label=values["label"],
        type=values["type"],
        start=values["start"],
        date=values["date"],
        source=source,
    )


def event_from_sources(sources: dict[str, SourceSnapshot]) -> EventConfig:
    """Build a brand-new race event and its sessions from one round's snapshots."""
    values = derive_event(list(sources.values()))
    return EventConfig(
        name=values["name"],
        location=values["location"],
        round=values["round"],
        sessions=[
            session_from_source(source, id_event, values["name"])
            for id_event, source in sources.items()
        ],
    )


@dataclass
class SyncResult:
    """The outcome of deciding whether to accept one provider scan."""

    committed: bool
    reason: str | None
    events_added: int = 0
    events_updated: int = 0
    events_disappeared: int = 0


def sync_snapshot(
    series_config: SeriesConfig,
    snapshot: SnapshotResult,
    *,
    season: str,
    now: str,
    is_current_season: bool,
    previous_count: int | None,
) -> SyncResult:
    """Merge one provider scan into a series' race events, in place.

    Guards against a flaky upstream, unchanged in intent from every earlier version:
    an incomplete snapshot is discarded in full, and an empty one is suspicious --
    always for the current season, and for a future season once that scope has
    previously been populated. Disappearance is marked, never deleted; retention
    prunes later.
    """
    if not snapshot.complete:
        return SyncResult(committed=False, reason="incomplete_snapshot")

    if not snapshot.events:
        if is_current_season:
            return SyncResult(committed=False, reason="suspicious_empty_current_season")
        if previous_count:
            return SyncResult(committed=False, reason="suspicious_empty_future_season")

    pairs = series_config.iter_sessions()
    session_by_key = {session.key: session for _, session in pairs}
    event_by_key = {session.key: event for event, session in pairs}

    by_round: dict[int, list[ProviderEvent]] = defaultdict(list)
    for provider_event in snapshot.events:
        by_round[provider_event.round].append(provider_event)

    added = updated = 0
    for round_number in sorted(by_round):
        group = by_round[round_number]
        new_sources = [_snapshot_of(provider_event) for provider_event in group]

        # ponytail: the weekend a round's sessions belong to is whichever stored
        # event already holds the first of them. A round reshuffle upstream can
        # land the group on the wrong weekend; move the sessions by hand if so.
        event = next(
            (event_by_key[e.id_event] for e in group if e.id_event in event_by_key), None
        )
        if event is None:
            # No session of this round is stored yet, so the whole weekend is new.
            event = event_from_sources(dict(zip((e.id_event for e in group), new_sources)))
            series_config.events.append(event)
            for session in event.sessions:
                session_by_key[session.key] = session
                event_by_key[session.key] = event
            added += len(event.sessions)
            continue

        # Scoped by season, not round: a session whose round the provider moved
        # still has to count as this weekend's baseline, or the round would never
        # be allowed to change.
        old_sources = [
            session.source for session in event.sessions
            if session.source is not None and session.source.season == season
        ]
        was_event_name = derive_event(old_sources)["name"] if old_sources else ""
        if merge_event(event, old_sources, new_sources):
            updated += 1
        now_event_name = derive_event(new_sources)["name"]

        for provider_event, source in zip(group, new_sources):
            session = session_by_key.get(provider_event.id_event)
            if session is None:
                session = session_from_source(source, provider_event.id_event, now_event_name)
                event.sessions.append(session)
                session_by_key[session.key] = session
                event_by_key[session.key] = event
                added += 1
                continue
            if merge_session(session, source, was_event_name=was_event_name,
                             now_event_name=now_event_name):
                updated += 1
            if session.disappeared_at is not None:
                session.disappeared_at = None  # reappeared upstream
                updated += 1

    seen = {provider_event.id_event for provider_event in snapshot.events}
    disappeared = 0
    for _, session in series_config.iter_sessions():
        in_scope = session.source is not None and session.source.season == season
        if in_scope and session.id_event not in seen and session.disappeared_at is None:
            session.disappeared_at = now
            disappeared += 1

    for event in series_config.events:
        event.sessions.sort(key=lambda session: (session.when, session.key))
    series_config.events.sort(key=lambda event: (event.when, event.name))

    return SyncResult(
        committed=True, reason=None, events_added=added,
        events_updated=updated, events_disappeared=disappeared,
    )
