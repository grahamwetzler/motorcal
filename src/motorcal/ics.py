"""Deterministic ICS generation from built PublishedEvents."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from icalendar import Alarm, Calendar, Event

from motorcal.config import Config, SeriesConfig, parse_alarm_offset
from motorcal.models import PublishedEvent

PRODID = "-//motorcal//motorsports-calendar//EN"


def build_vevent(
    *,
    uid: str,
    summary: str,
    series_name: str,
    status: str,
    start: datetime | None,
    all_day_date: str | None,
    duration_seconds: int | None,
    dtstamp: datetime,
    last_modified: datetime,
    sequence: int,
    description: str,
    location: str | None,
    alarms: list[str],
) -> Event:
    """Render one published event into an icalendar VEVENT component."""
    event = Event()
    event.add("uid", uid)

    summary = f"{series_name}: {summary}"
    rendered_summary = f"[Postponed] {summary}" if status == "TENTATIVE" else summary
    event.add("summary", rendered_summary)

    if start is not None:
        event.add("dtstart", start)
        if duration_seconds:
            event.add("dtend", start + timedelta(seconds=duration_seconds))
    else:
        event.add("dtstart", date.fromisoformat(all_day_date))

    event.add("dtstamp", dtstamp)
    event.add("last-modified", last_modified)
    event.add("sequence", sequence)
    event.add("status", status)
    event.add("description", description)
    if location:
        event.add("location", location)

    for offset in alarms:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", rendered_summary)
        alarm.add("trigger", timedelta(seconds=parse_alarm_offset(offset)))
        event.add_component(alarm)

    return event


def _calendar(calname: str, caldesc: str, vevents: list[Event]) -> Calendar:
    """Assemble one deterministic VCALENDAR from already-rendered VEVENTs."""
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", calname)
    calendar.add("x-wr-caldesc", caldesc)

    calendar.add("refresh-interval;value=duration", "PT1H")
    calendar.add("x-published-ttl", "PT1H")

    for vevent in sorted(vevents, key=lambda e: str(e.get("uid"))):
        calendar.add_component(vevent)

    return calendar


def build_calendar(series_config: SeriesConfig, vevents: list[Event]) -> Calendar:
    """Assemble one deterministic VCALENDAR for a series from its rendered VEVENTs."""
    caldesc = f"{series_config.name} calendar"
    if series_config.race_only:
        caldesc += " (race sessions only)"
    return _calendar(series_config.name, caldesc, vevents)


def _to_vevent(event: PublishedEvent, series_name: str) -> Event:
    return build_vevent(
        uid=event.uid,
        summary=event.summary,
        series_name=series_name,
        status=event.status.value,
        start=event.start,
        all_day_date=event.all_day_date,
        duration_seconds=event.duration_seconds,
        dtstamp=event.dtstamp,
        last_modified=event.last_modified,
        sequence=event.sequence,
        description=event.description,
        location=event.location,
        alarms=event.alarms,
    )


def render_calendar_bytes(
    series_config: SeriesConfig, events: list[PublishedEvent]
) -> bytes:
    """Render the deterministic ICS bytes for one series' published events."""
    return build_calendar(
        series_config, [_to_vevent(e, series_config.name) for e in events]
    ).to_ical()


def render_combined_bytes(
    config: Config, published: dict[str, list[PublishedEvent]]
) -> bytes:
    """Render every series' events into the one combined feed.

    Each event keeps its own series' display name, which `build_vevent` already
    puts in front of every summary, so the series stay legible once mixed.
    """
    vevents = [
        _to_vevent(event, config.series[series].name)
        for series, events in published.items()
        if series in config.series
        for event in events
    ]
    return _calendar("Motorsports", "All series", vevents).to_ical()


def compute_content_hash(ics_bytes: bytes) -> str:
    """The feed's ETag: identical bytes always yield an identical revision."""
    return hashlib.sha256(ics_bytes).hexdigest()
