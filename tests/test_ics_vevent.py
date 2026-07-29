from datetime import date, datetime, timezone

from motorcal.ics import build_vevent


def test_confirmed_timed_event_with_duration_and_alarm():
    event = build_vevent(
        uid="thesportsdb-2421035@x.example.com",
        summary="6 Hours of Imola",
        status="CONFIRMED",
        start=datetime(2026, 4, 19, 13, 0, tzinfo=timezone.utc),
        all_day_date=None,
        duration_seconds=6 * 3600,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=1,
        description="Venue: Imola\nSource: TheSportsDB",
        location="Imola, Italy",
        alarms=["-1d", "-30m"],
    )
    ics_bytes = event.to_ical()

    assert b"VTIMEZONE" not in ics_bytes
    assert b"DTSTART:20260419T130000Z" in ics_bytes
    assert b"DTEND:20260419T190000Z" in ics_bytes
    assert b"UID:thesportsdb-2421035@x.example.com" in ics_bytes
    assert b"STATUS:CONFIRMED" in ics_bytes
    assert b"SUMMARY:6 Hours of Imola" in ics_bytes
    assert ics_bytes.count(b"BEGIN:VALARM") == 2
    assert b"TRIGGER:-P1D" in ics_bytes
    assert b"TRIGGER:-PT30M" in ics_bytes


def test_all_day_event_has_no_dtend_and_no_alarms():
    event = build_vevent(
        uid="thesportsdb-9999@x.example.com",
        summary="Some Race (time TBC)",
        status="CONFIRMED",
        start=None,
        all_day_date="2026-05-01",
        duration_seconds=None,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=1,
        description="Time not yet confirmed by the source (TBC).",
        location=None,
        alarms=[],
    )
    ics_bytes = event.to_ical()

    assert b"DTSTART;VALUE=DATE:20260501" in ics_bytes
    assert b"DTEND" not in ics_bytes
    assert b"DURATION" not in ics_bytes
    assert ics_bytes.count(b"BEGIN:VALARM") == 0


def test_timed_event_with_no_known_duration_has_no_dtend():
    event = build_vevent(
        uid="u3@x.example.com",
        summary="Hyperpole Qualifying",
        status="CONFIRMED",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc),
        all_day_date=None,
        duration_seconds=None,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=1,
        description="d",
        location=None,
        alarms=[],
    )
    ics_bytes = event.to_ical()

    assert b"DTSTART:20260610T164500Z" in ics_bytes
    assert b"DTEND" not in ics_bytes
    assert b"DURATION" not in ics_bytes


def test_tentative_status_prefixes_postponed_on_summary_and_alarm():
    event = build_vevent(
        uid="u4@x.example.com",
        summary="Some Race",
        status="TENTATIVE",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc),
        all_day_date=None,
        duration_seconds=None,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=2,
        description="d",
        location=None,
        alarms=["-1d"],
    )
    ics_bytes = event.to_ical()

    assert b"STATUS:TENTATIVE" in ics_bytes
    assert b"SUMMARY:[Postponed] Some Race" in ics_bytes
    assert b"DESCRIPTION:[Postponed] Some Race" in ics_bytes  # the VALARM's own description


def test_cancelled_status_has_no_special_prefix():
    event = build_vevent(
        uid="u5@x.example.com",
        summary="Cancelled Race",
        status="CANCELLED",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc),
        all_day_date=None,
        duration_seconds=None,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=2,
        description="d",
        location=None,
        alarms=[],
    )
    ics_bytes = event.to_ical()

    assert b"STATUS:CANCELLED" in ics_bytes
    assert b"SUMMARY:Cancelled Race" in ics_bytes  # no prefix


def test_location_omitted_when_none():
    event = build_vevent(
        uid="u6@x.example.com", summary="S", status="CONFIRMED",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc), all_day_date=None,
        duration_seconds=None, dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1,
        description="d", location=None, alarms=[],
    )
    ics_bytes = event.to_ical()
    assert b"LOCATION" not in ics_bytes


def test_rendering_the_same_input_twice_is_byte_identical():
    kwargs = dict(
        uid="u7@x.example.com", summary="S", status="CONFIRMED",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc), all_day_date=None,
        duration_seconds=3600, dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1,
        description="d", location="L", alarms=["-1d", "-30m"],
    )
    b1 = build_vevent(**kwargs).to_ical()
    b2 = build_vevent(**kwargs).to_ical()
    assert b1 == b2
