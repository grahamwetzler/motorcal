from datetime import datetime, timezone

from tests.conftest import make_series
from motorcal.ics import compute_content_hash, render_calendar_bytes
from motorcal.models import EventStatus, PublishedEvent, SessionType

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SERIES_CFG = make_series()


def _published(uid, series="wec", summary="S"):
    return PublishedEvent(
        uid=uid, series=series, session_type=SessionType.RACE, summary=summary,
        start=datetime(2026, 4, 19, 13, tzinfo=timezone.utc), all_day_date=None,
        time_confirmed=True, duration_seconds=3600, location="L", description="D",
        status=EventStatus.CONFIRMED, sequence=1, dtstamp=NOW, last_modified=NOW,
        fingerprint="fp", alarms=["-1d"], event_key="1",
    )


def test_render_calendar_bytes_produces_valid_ics():
    ics_bytes = render_calendar_bytes(SERIES_CFG, [_published("u1")])

    assert b"BEGIN:VCALENDAR" in ics_bytes
    assert b"UID:u1" in ics_bytes
    assert b"X-WR-CALNAME:WEC" in ics_bytes


def test_render_calendar_bytes_renders_only_what_it_is_given():
    ics_bytes = render_calendar_bytes(SERIES_CFG, [_published("u1")])

    assert b"UID:u2" not in ics_bytes


def test_render_calendar_bytes_is_deterministic_across_calls():
    events = [_published("u1")]

    assert render_calendar_bytes(SERIES_CFG, events) == render_calendar_bytes(SERIES_CFG, events)


def test_render_calendar_bytes_is_independent_of_event_order():
    a, b = _published("u1"), _published("u2")

    assert render_calendar_bytes(SERIES_CFG, [a, b]) == render_calendar_bytes(SERIES_CFG, [b, a])


def test_render_calendar_bytes_handles_an_empty_series():
    ics_bytes = render_calendar_bytes(SERIES_CFG, [])

    assert b"BEGIN:VCALENDAR" in ics_bytes
    assert b"BEGIN:VEVENT" not in ics_bytes


def test_compute_content_hash_is_stable_for_identical_bytes():
    assert compute_content_hash(b"hello") == compute_content_hash(b"hello")
    assert compute_content_hash(b"hello") != compute_content_hash(b"world")
