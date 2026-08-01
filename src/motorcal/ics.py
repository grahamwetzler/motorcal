"""Deterministic ICS generation from built PublishedEvents."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from icalendar import Alarm, Calendar, Event

from motorcal.config import Config, SeriesConfig, parse_alarm_offset
from motorcal.models import PublishedEvent

PRODID = "-//motorcal//motorsports-calendar//EN"


def build_title(series_name: str, summary: str, status: str) -> str:
    """The event title a calendar app shows, before any `prefix` is put in front.

    Split out so the landing page can preview the exact title a subscriber will
    end up with rather than approximating it.
    """
    title = f"{series_name}: {summary}"
    return f"[Postponed] {title}" if status == "TENTATIVE" else title


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
    prefix: str = "",
) -> Event:
    """Render one published event into an icalendar VEVENT component.

    `prefix` goes outermost, in front of everything else the title carries:
    "🏁 [Postponed] WEC: 6 Hours of Imola".
    """
    event = Event()
    event.add("uid", uid)

    rendered_summary = f"{prefix}{build_title(series_name, summary, status)}"
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


def _series_caldesc(series_config: SeriesConfig) -> str:
    caldesc = f"{series_config.name} calendar"
    if series_config.race_only:
        caldesc += " (race sessions only)"
    return caldesc


def build_calendar(series_config: SeriesConfig, vevents: list[Event]) -> Calendar:
    """Assemble one deterministic VCALENDAR for a series from its rendered VEVENTs."""
    return _calendar(series_config.name, _series_caldesc(series_config), vevents)


def _to_vevent(event: PublishedEvent, series_name: str, prefix: str) -> Event:
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
        prefix=prefix,
    )


def render_bytes(
    calname: str,
    caldesc: str,
    entries: list[tuple[str, list[PublishedEvent]]],
    *,
    prefix: str = "",
) -> bytes:
    """Render one VCALENDAR from (series display name, that series' events) pairs.

    The one place published events become ICS bytes. It takes display names and a
    plain prefix string rather than any richer object: `web.py` imports this
    module to render its per-request feeds, so accepting a type owned by `web.py`
    would put the two modules in an import cycle.
    """
    vevents = [
        _to_vevent(event, series_name, prefix)
        for series_name, events in entries
        for event in events
    ]
    return _calendar(calname, caldesc, vevents).to_ical()


def render_calendar_bytes(
    series_config: SeriesConfig,
    events: list[PublishedEvent],
    *,
    prefix: str = "",
    calname: str | None = None,
) -> bytes:
    """Render the deterministic ICS bytes for one series' published events."""
    return render_bytes(
        calname or series_config.name,
        _series_caldesc(series_config),
        [(series_config.name, events)],
        prefix=prefix,
    )


def render_combined_bytes(
    config: Config,
    published: dict[str, list[PublishedEvent]],
    *,
    prefix: str = "",
    calname: str | None = None,
) -> bytes:
    """Render every series' events into the one combined feed.

    Each event keeps its own series' display name, which `build_vevent` already
    puts in front of every summary, so the series stay legible once mixed.
    """
    return render_bytes(
        calname or "Motorsports",
        "All series",
        [
            (config.series[series].name, events)
            for series, events in published.items()
            if series in config.series
        ],
        prefix=prefix,
    )


def compute_content_hash(ics_bytes: bytes) -> str:
    """The feed's ETag: identical bytes always yield an identical revision."""
    return hashlib.sha256(ics_bytes).hexdigest()
