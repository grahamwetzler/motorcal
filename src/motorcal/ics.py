"""Deterministic ICS generation from published_events state."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from icalendar import Alarm, Calendar, Event

from motorcal.config import SeriesConfig, parse_alarm_offset
from motorcal.store import (
    get_feed_revision,
    list_published_events_by_series,
    transaction,
    upsert_feed_revision,
)

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


def _row_to_vevent(row: sqlite3.Row) -> Event:
    return build_vevent(
        uid=row["uid"],
        summary=row["summary"],
        status=row["status"],
        start=datetime.fromisoformat(row["start"]) if row["start"] else None,
        all_day_date=row["all_day_date"],
        duration_seconds=row["duration_seconds"],
        dtstamp=datetime.fromisoformat(row["dtstamp"]),
        last_modified=datetime.fromisoformat(row["last_modified"]),
        sequence=row["sequence"],
        description=row["description"],
        location=row["location"],
        alarms=json.loads(row["alarms_json"]),
    )


def render_calendar_bytes(
    conn: sqlite3.Connection, series: str, series_config: SeriesConfig
) -> bytes:
    """Render the current, deterministic ICS bytes for one series from stored state."""
    rows = list_published_events_by_series(conn, series)
    vevents = [_row_to_vevent(row) for row in rows]
    return build_calendar(series_config, vevents).to_ical()


def compute_content_hash(ics_bytes: bytes) -> str:
    return hashlib.sha256(ics_bytes).hexdigest()


@dataclass
class FeedRevisionState:
    revision: str
    updated_at: str


def sync_feed_revision(
    conn: sqlite3.Connection, series: str, ics_bytes: bytes, now: str
) -> FeedRevisionState:
    """Advance the stored feed revision only if the content actually changed."""
    new_revision = compute_content_hash(ics_bytes)
    existing = get_feed_revision(conn, series)
    if existing is not None and existing["revision"] == new_revision:
        return FeedRevisionState(revision=existing["revision"], updated_at=existing["updated_at"])

    with transaction(conn):
        upsert_feed_revision(conn, series, new_revision, now)
    return FeedRevisionState(revision=new_revision, updated_at=now)
