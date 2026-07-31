from datetime import datetime, timezone

from tests.conftest import make_config, make_series
from motorcal.ics import compute_content_hash, render_calendar_bytes, render_combined_bytes
from motorcal.models import EventStatus, PublishedEvent, SessionType

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SERIES_CFG = make_series()


def _published(uid, series="wec", summary="S"):
    return PublishedEvent(
        uid=uid, series=series, session_type=SessionType.RACE, summary=summary,
        start=datetime(2026, 4, 19, 13, tzinfo=timezone.utc), all_day_date=None,
        time_confirmed=True, duration_seconds=3600, location="L", description="D",
        status=EventStatus.CONFIRMED, sequence=1, dtstamp=NOW, last_modified=NOW,
        fingerprint="fp", alarms=["-1d"], session_key="1",
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


def test_render_combined_bytes_carries_every_series_under_its_own_name():
    config = make_config(
        series={"wec": make_series(), "f1": make_series(league_id=4370, name="F1")}
    )
    published = {"wec": [_published("u1", summary="Imola")], "f1": [_published("u2", summary="Bahrain")]}

    ics_bytes = render_combined_bytes(config, published)

    assert b"X-WR-CALNAME:Motorsports" in ics_bytes
    assert b"SUMMARY:WEC: Imola" in ics_bytes
    assert b"SUMMARY:F1: Bahrain" in ics_bytes


def test_render_combined_bytes_skips_series_the_config_no_longer_has():
    """published can outlive a series a config reload dropped; it must not KeyError."""
    config = make_config(series={"wec": make_series()})
    published = {"wec": [_published("u1")], "gone": [_published("u2")]}

    ics_bytes = render_combined_bytes(config, published)

    assert b"UID:u1" in ics_bytes
    assert b"UID:u2" not in ics_bytes


def test_render_combined_bytes_is_deterministic_regardless_of_series_order():
    config = make_config(
        series={"wec": make_series(), "f1": make_series(league_id=4370, name="F1")}
    )
    wec, f1 = {"wec": [_published("u1")]}, {"f1": [_published("u2", series="f1")]}

    assert render_combined_bytes(config, {**wec, **f1}) == render_combined_bytes(config, {**f1, **wec})


def test_compute_content_hash_is_stable_for_identical_bytes():
    assert compute_content_hash(b"hello") == compute_content_hash(b"hello")
    assert compute_content_hash(b"hello") != compute_content_hash(b"world")
