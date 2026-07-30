"""Building one published event from one configured event."""
from datetime import datetime, timezone

from tests.conftest import UID_DOMAIN, make_globals, make_series, manual_event, source_event

from motorcal.config import DurationDefaults, EventConfig
from motorcal.merge import build_published_event
from motorcal.models import EventStatus, SessionType
from motorcal.state import VersionState

NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def _build(event, *, series="wec", series_config=None, globals_=None, previous=None, now=NOW):
    return build_published_event(
        event, series=series, series_config=series_config or make_series(),
        globals_=globals_ or make_globals(alerts={"race": ["-1d", "-30m"], "qualifying": ["-15m"]}),
        previous=previous, now=now,
    )


def test_confirmed_time_produces_a_timed_event():
    built = _build(source_event("1", time="13:00:00"))

    assert built.time_confirmed is True
    assert built.start == datetime(2026, 4, 19, 13, tzinfo=timezone.utc)
    assert built.all_day_date is None


def test_unannounced_time_produces_an_all_day_tbc_event_with_no_alarms():
    built = _build(source_event("1", time=None))

    assert built.time_confirmed is False
    assert built.start is None
    assert built.all_day_date == "2026-04-19"
    assert built.summary.endswith(" (time TBC)")
    assert built.alarms == []
    assert built.duration_seconds is None


def test_a_manual_all_day_event_is_not_marked_tbc():
    """An all-day manual event is deliberate, not an unannounced provider time."""
    built = _build(manual_event("mine", summary="Test Day"))

    assert built.all_day_date == "2026-05-01"
    assert built.summary == "Test Day"


def test_uid_distinguishes_provider_and_manual_events():
    assert _build(source_event("1")).uid == f"thesportsdb-1@{UID_DOMAIN}"
    assert _build(manual_event("mine")).uid == f"local-mine@{UID_DOMAIN}"


def test_session_type_is_classified_from_the_summary():
    built = _build(source_event("1", name="6 Hours of Imola Qualifying"))

    assert built.session_type == SessionType.QUALIFYING


def test_alarms_come_from_the_global_defaults_for_the_session_type():
    built = _build(source_event("1", name="6 Hours of Imola", time="13:00:00"))

    assert built.session_type == SessionType.RACE
    assert built.alarms == ["-1d", "-30m"]


def test_an_events_own_alarms_win_over_the_defaults():
    event = source_event("1", time="13:00:00")
    event.alarms = ["-2h"]

    assert _build(event).alarms == ["-2h"]


def test_an_explicit_empty_alarm_list_silences_just_that_event():
    event = source_event("1", time="13:00:00")
    event.alarms = []

    assert _build(event).alarms == []


def test_duration_falls_back_from_event_to_series_to_global():
    event = source_event("1", name="Practice One", time="13:00:00")
    globals_ = make_globals(durations=DurationDefaults(practice="1h"))

    assert _build(event, globals_=globals_).duration_seconds == 3600

    series_config = make_series(durations=DurationDefaults(practice="90m"))
    assert _build(event, globals_=globals_, series_config=series_config).duration_seconds == 5400

    event.duration = "2h"
    assert _build(event, globals_=globals_, series_config=series_config).duration_seconds == 7200


def test_location_and_note_reach_the_published_event():
    event = source_event("1", time="13:00:00")
    event.note = "official timetable"

    built = _build(event)

    assert built.location == "Imola, Italy"
    assert "Note: official timetable" in built.description


def test_description_names_the_provider_for_provider_backed_events():
    assert "Source: TheSportsDB" in _build(source_event("1")).description


def test_description_names_local_for_manual_events():
    assert "Source: local event" in _build(manual_event("mine")).description


def test_race_only_series_says_so_in_the_description():
    series_config = make_series(race_only=True)

    assert "race sessions only" in _build(source_event("1"), series_config=series_config).description


def test_race_only_series_note_is_absent_from_a_manual_qualifying_event():
    """A manually added qualifying event in a race-only series shouldn't claim the
    feed only has races -- that would contradict the event it's attached to."""
    series_config = make_series(race_only=True)
    event = manual_event("indycar-2026-portland-qualifying", summary="Portland Qualifying")

    built = _build(event, series="indycar", series_config=series_config)

    assert built.session_type == SessionType.QUALIFYING
    assert "race sessions only" not in built.description


def test_a_disappeared_future_event_is_cancelled():
    event = source_event("1", time="13:00:00", disappeared_at="t1")

    assert _build(event).status == EventStatus.CANCELLED


def test_a_disappeared_past_event_keeps_its_last_known_status():
    event = source_event("1", date="2025-01-01", time="13:00:00", disappeared_at="t1")

    built = _build(event, previous=None)

    assert built.status == EventStatus.CONFIRMED  # not cancelled -- it already happened


def test_cancellation_is_sticky_once_applied():
    event = source_event("1", date="2025-01-01", time="13:00:00", disappeared_at="t1")
    previous = VersionState(
        fingerprint="x", sequence=1, dtstamp=NOW.isoformat(),
        last_modified=NOW.isoformat(), status="CANCELLED",
    )

    assert _build(event, previous=previous).status == EventStatus.CANCELLED


def test_a_configured_status_is_honoured():
    event = EventConfig(uid="mine", summary="Maybe", date="2026-05-01", status="TENTATIVE")

    assert _build(event).status == EventStatus.TENTATIVE


def test_an_unchanged_event_keeps_its_sequence_and_dtstamp():
    event = source_event("1", time="13:00:00")
    first = _build(event)
    previous = VersionState(
        fingerprint=first.fingerprint, sequence=first.sequence,
        dtstamp=first.dtstamp.isoformat(), last_modified=first.last_modified.isoformat(),
        status=first.status.value,
    )

    second = _build(event, previous=previous, now=datetime(2026, 6, 1, tzinfo=timezone.utc))

    assert second.sequence == first.sequence
    assert second.dtstamp == first.dtstamp


def test_a_changed_event_advances_sequence_and_dtstamp():
    event = source_event("1", time="13:00:00")
    first = _build(event)
    previous = VersionState(
        fingerprint=first.fingerprint, sequence=first.sequence,
        dtstamp=first.dtstamp.isoformat(), last_modified=first.last_modified.isoformat(),
        status=first.status.value,
    )
    event.summary = "Renamed"

    later = datetime(2026, 6, 1, tzinfo=timezone.utc)
    second = _build(event, previous=previous, now=later)

    assert second.sequence > first.sequence
    assert second.dtstamp == later
