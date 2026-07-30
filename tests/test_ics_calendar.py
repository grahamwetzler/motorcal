from datetime import datetime, timezone

from motorcal.config import SeriesConfig
from motorcal.ics import build_calendar, build_vevent


def _event(uid, start_hour=13):
    return build_vevent(
        uid=uid, summary=f"Event {uid}", series_name="WEC", status="CONFIRMED",
        start=datetime(2026, 4, 19, start_hour, 0, tzinfo=timezone.utc), all_day_date=None,
        duration_seconds=3600, dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1,
        description="d", location=None, alarms=[],
    )


def test_calendar_has_required_calendar_level_properties():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    cal = build_calendar(series_cfg, [_event("u1")])
    ics_bytes = cal.to_ical()

    assert b"VERSION:2.0" in ics_bytes
    assert b"METHOD:PUBLISH" in ics_bytes
    assert b"X-WR-CALNAME:WEC" in ics_bytes
    assert b"REFRESH-INTERVAL;VALUE=DURATION:PT1H" in ics_bytes
    assert b"X-PUBLISHED-TTL:PT1H" in ics_bytes
    assert b"PRODID" in ics_bytes


def test_race_only_series_mentions_it_in_caldesc():
    series_cfg = SeriesConfig(league_id=4373, name="IndyCar", max_round=30, race_only=True)
    cal = build_calendar(series_cfg, [_event("u1")])
    ics_bytes = cal.to_ical()

    assert b"CALDESC" in ics_bytes
    assert b"race" in ics_bytes.lower()


def test_non_race_only_series_caldesc_has_no_race_only_note():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20, race_only=False)
    cal = build_calendar(series_cfg, [_event("u1")])
    ics_bytes = cal.to_ical()

    assert b"race sessions only" not in ics_bytes.lower()


def test_events_are_rendered_in_uid_sorted_order_regardless_of_input_order():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    events_in_reverse = [_event("z-event"), _event("a-event"), _event("m-event")]
    cal = build_calendar(series_cfg, events_in_reverse)
    ics_bytes = cal.to_ical()

    a_pos = ics_bytes.index(b"UID:a-event")
    m_pos = ics_bytes.index(b"UID:m-event")
    z_pos = ics_bytes.index(b"UID:z-event")
    assert a_pos < m_pos < z_pos


def test_rendering_the_same_calendar_twice_is_byte_identical():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    events = [_event("u1"), _event("u2")]
    b1 = build_calendar(series_cfg, events).to_ical()
    b2 = build_calendar(series_cfg, [_event("u1"), _event("u2")]).to_ical()
    assert b1 == b2


def test_rendering_is_stable_regardless_of_input_list_order():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    forward = [_event("u1"), _event("u2")]
    backward = [_event("u2"), _event("u1")]
    assert build_calendar(series_cfg, forward).to_ical() == build_calendar(series_cfg, backward).to_ical()


def test_empty_calendar_still_has_valid_header():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    cal = build_calendar(series_cfg, [])
    ics_bytes = cal.to_ical()
    assert b"BEGIN:VCALENDAR" in ics_bytes
    assert b"END:VCALENDAR" in ics_bytes
    assert b"BEGIN:VEVENT" not in ics_bytes
