"""Fold a provider snapshot into a series' event list without clobbering hand edits.

Three-way merge, per event, per field: `event.source` records what the provider
said on the previous fetch. A field is only overwritten when the provider actually
changed it *and* the stored value still matches what the provider said before. Any
value you edited by hand has diverged from that baseline, so it always wins.

Manual events (no `source`) are never touched.
"""
from __future__ import annotations

from dataclasses import dataclass

from motorcal.config import EventConfig, SeriesConfig, SourceSnapshot
from motorcal.providers.thesportsdb import ProviderEvent, SnapshotResult

# The fields a provider fetch can own. Everything else on an EventConfig
# (duration, status, note, alarms) is yours alone and is never synced.
DERIVED_FIELDS = ("summary", "start", "date", "location", "round")


def _snapshot_of(event: ProviderEvent) -> SourceSnapshot:
    return SourceSnapshot(
        name=event.name, date=event.date, time=event.time,
        venue=event.venue, country=event.country, round=event.round, season=event.season,
    )


def derive(source: SourceSnapshot) -> dict:
    """The published values a provider snapshot implies, before any local edit.

    A missing or midnight time means the provider hasn't announced one yet, which
    is published as an all-day event rather than a guess at 00:00.
    """
    if source.time in (None, "", "00:00:00"):
        start, date = None, source.date
    else:
        start, date = f"{source.date}T{source.time}+00:00", None

    if source.venue and source.country:
        location = f"{source.venue}, {source.country}"
    else:
        location = source.venue or source.country

    return {
        "summary": source.name,
        "start": start,
        "date": date,
        "location": location,
        "round": source.round,
    }


def merge_event(event: EventConfig, new_source: SourceSnapshot) -> list[str]:
    """Apply a fresh provider snapshot to one stored event. Returns the fields taken.

    `start` and `date` are merged as a pair: they are two encodings of one fact
    (is the time known?), and treating them independently could leave an event
    with both set, or neither.
    """
    if event.source is None:
        return []  # manual event: nothing upstream owns any of its fields

    was, now = derive(event.source), derive(new_source)
    taken: list[str] = []
    for field in ("summary", "location", "round"):
        if was[field] != now[field] and getattr(event, field) == was[field]:
            setattr(event, field, now[field])
            taken.append(field)

    timing_changed = (was["start"], was["date"]) != (now["start"], now["date"])
    timing_untouched = (event.start, event.date) == (was["start"], was["date"])
    if timing_changed and timing_untouched:
        event.start, event.date = now["start"], now["date"]
        taken.append("start" if now["start"] else "date")

    event.source = new_source
    return taken


def event_from_source(source: SourceSnapshot, id_event: str) -> EventConfig:
    """Build a brand-new event from a provider snapshot -- nothing local to preserve."""
    values = derive(source)
    return EventConfig(
        id_event=id_event,
        summary=values["summary"],
        start=values["start"],
        date=values["date"],
        location=values["location"],
        round=values["round"],
        source=source,
    )


def _sort_key(event: EventConfig) -> tuple:
    return (event.start or event.date or "", event.key)


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
    """Merge one provider scan into a series' events, in place.

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

    by_key = {event.key: event for event in series_config.events}
    added = updated = 0

    for provider_event in snapshot.events:
        source = _snapshot_of(provider_event)
        existing = by_key.get(provider_event.id_event)
        if existing is None:
            new_event = event_from_source(source, provider_event.id_event)
            series_config.events.append(new_event)
            by_key[new_event.key] = new_event
            added += 1
            continue
        if merge_event(existing, source):
            updated += 1
        if existing.disappeared_at is not None:
            existing.disappeared_at = None  # reappeared upstream
            updated += 1

    seen = {event.id_event for event in snapshot.events}
    disappeared = 0
    for event in series_config.events:
        in_scope = event.source is not None and event.source.season == season
        if in_scope and event.id_event not in seen and event.disappeared_at is None:
            event.disappeared_at = now
            disappeared += 1

    series_config.events.sort(key=_sort_key)
    return SyncResult(
        committed=True, reason=None, events_added=added,
        events_updated=updated, events_disappeared=disappeared,
    )
