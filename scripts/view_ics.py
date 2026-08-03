#!/usr/bin/env python3
"""Fetch an ICS feed and print its events in a human-readable form.

Usage: uv run scripts/view_ics.py <url>
"""

from __future__ import annotations

import sys
from datetime import date, datetime

import httpx
from icalendar import Calendar


def fmt_dt(value: date | datetime) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M %Z").strip()
    return value.strftime("%Y-%m-%d") + " (all day)"


def fmt_trigger(alarm) -> str:
    trigger = alarm.get("trigger")
    if trigger is None:
        return "?"
    delta = trigger.dt
    if isinstance(delta, (datetime, date)):
        return f"at {delta}"
    seconds = int(delta.total_seconds())
    return (
        f"{-seconds // 60} min before"
        if seconds < 0
        else f"{seconds // 60} min after start"
    )


def print_event(vevent) -> None:
    summary = str(vevent.get("summary", "(no title)"))
    print(f"• {summary}")

    start = vevent.get("dtstart")
    end = vevent.get("dtend")
    if start is not None:
        print(
            f"    When:        {fmt_dt(start.dt)}"
            + (f" → {fmt_dt(end.dt)}" if end is not None else "")
        )

    location = vevent.get("location")
    if location:
        print(f"    Location:    {location}")

    status = vevent.get("status")
    if status:
        print(f"    Status:      {status}")

    organizer = vevent.get("organizer")
    if organizer:
        print(f"    Organizer:   {organizer}")

    url = vevent.get("url")
    if url:
        print(f"    URL:         {url}")

    rrule = vevent.get("rrule")
    if rrule:
        print(f"    Repeats:     {rrule.to_ical().decode()}")

    for alarm in vevent.walk("VALARM"):
        print(f"    Reminder:    {fmt_trigger(alarm)}")

    description = vevent.get("description")
    if description:
        text = str(description).strip()
        print("    Description:")
        for line in text.splitlines():
            print(f"        {line}")

    print()


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <url>", file=sys.stderr)
        raise SystemExit(1)

    response = httpx.get(sys.argv[1], follow_redirects=True, timeout=30)
    response.raise_for_status()

    calendar = Calendar.from_ical(response.content)

    name = calendar.get("x-wr-calname")
    if name:
        print(f"Calendar: {name}\n")

    events = sorted(
        calendar.walk("VEVENT"),
        key=lambda e: str(e.get("dtstart").dt) if e.get("dtstart") else "",
    )
    for vevent in events:
        print_event(vevent)


if __name__ == "__main__":
    main()
