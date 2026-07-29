"""Deterministic ICS generation from published_events state."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from icalendar import Alarm, Calendar, Event

from motorcal.config import SeriesConfig, parse_alarm_offset

PRODID = "-//motorcal//motorsports-calendar//EN"


def build_vevent(
    *,
    uid: str,
    summary: str,
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


def build_calendar(series_config: SeriesConfig, vevents: list[Event]) -> Calendar:
    """Assemble one deterministic VCALENDAR for a series from its rendered VEVENTs."""
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", series_config.name)

    caldesc = f"{series_config.name} calendar"
    if series_config.race_only:
        caldesc += " (race sessions only)"
    calendar.add("x-wr-caldesc", caldesc)

    calendar.add("refresh-interval;value=duration", "PT1H")
    calendar.add("x-published-ttl", "PT1H")

    for vevent in sorted(vevents, key=lambda e: str(e.get("uid"))):
        calendar.add_component(vevent)

    return calendar
