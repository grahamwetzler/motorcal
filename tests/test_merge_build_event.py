"""Building one published event from one session of one configured race event."""
from datetime import UTC, datetime

from motorcal.config import EventConfig
from motorcal.merge import build_published_event
from motorcal.models import EventStatus, SessionType
from motorcal.state import VersionState
from tests.conftest import (
    UID_DOMAIN,
    make_event,
    make_globals,
    make_series,
    make_session,
)

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _build(event, *, series="wec", series_config=None, globals_=None, previous=None, now=NOW):
    """Build the first (usually only) session of `event`."""
    return build_published_event(
        event, event.sessions[0], series=series, series_config=series_config or make_series(),
        globals_=globals_ or make_globals(alerts={"race": ["-1d", "-30m"], "qualifying": ["-15m"]}),
        previous=previous, now=now,
    )


def test_confirmed_time_produces_a_timed_event():
    built = _build(make_event("r", start="2026-04-19T13:00:00+00:00"))

    assert built.time_confirmed is True
    assert built.start == datetime(2026, 4, 19, 13, tzinfo=UTC)
    assert built.all_day_date is None


def test_the_summary_is_the_event_name_and_the_session_label():
    event = make_event("q", label="Qualifying", type="qualifying",
                       start="2026-04-18T13:00:00+00:00")

    assert _build(event).summary == "6 Hours of Imola Qualifying"


def test_a_session_with_no_label_publishes_just_the_event_name():
    assert _build(make_event("t", name="Pre-season testing")).summary == "Pre-season testing"


def test_a_tbc_session_is_all_day_marked_tbc_and_has_no_alarms():
    built = _build(make_event("r", date="2026-04-19", tbc=True))

    assert built.time_confirmed is False
    assert built.start is None
    assert built.all_day_date == "2026-04-19"
    assert built.summary == "6 Hours of Imola (time TBC)"
    assert "Start time not yet announced (TBC)." in built.description
    assert built.alarms == []
    assert built.duration_seconds is None


def test_an_all_day_session_that_is_not_tbc_is_left_alone():
    """A test day is deliberately all-day, not a time nobody has announced."""
    built = _build(make_event("t", name="Test Day", type="testing"))

    assert built.all_day_date == "2026-05-01"
    assert built.summary == "Test Day"
    assert "TBC" not in built.description


def test_the_uid_is_built_from_the_sessions_own_uid():
    assert _build(make_event("mine")).uid == f"local-mine@{UID_DOMAIN}"


def test_the_published_session_type_is_the_one_stored_on_the_session():
    event = make_event("q", label="Qualifying", type="qualifying")

    assert _build(event).session_type == SessionType.QUALIFYING


def test_alarms_come_from_the_global_defaults_for_the_session_type():
    built = _build(make_event("r", start="2026-04-19T13:00:00+00:00"))

    assert built.session_type == SessionType.RACE
    assert built.alarms == ["-1d", "-30m"]


def test_a_sessions_own_alarms_win_over_the_defaults():
    event = make_event("r", start="2026-04-19T13:00:00+00:00", alarms=["-2h"])

    assert _build(event).alarms == ["-2h"]


def test_an_explicit_empty_alarm_list_silences_just_that_session():
    event = make_event("r", start="2026-04-19T13:00:00+00:00", alarms=[])

    assert _build(event).alarms == []


def test_testing_sessions_never_get_alarms():
    event = make_event("t", type="testing", start="2026-02-11T09:00:00+00:00")
    globals_ = make_globals(alerts={"testing": ["-1d"]})

    assert _build(event, globals_=globals_).alarms == []


def test_duration_falls_back_from_session_to_series_to_global():
    event = make_event("p1", label="Practice 1", type="practice",
                       start="2026-04-18T09:00:00+00:00")
    globals_ = make_globals(durations={"practice": "1h"})

    assert _build(event, globals_=globals_).duration_seconds == 3600

    series_config = make_series(durations={"practice": "90m"})
    assert _build(event, globals_=globals_, series_config=series_config).duration_seconds == 5400

    event.sessions[0].duration = "2h"
    assert _build(event, globals_=globals_, series_config=series_config).duration_seconds == 7200


def test_warmup_resolves_its_own_duration_and_alarms():
    """The session type IMSA and IndyCar run that TheSportsDB never carried."""
    event = make_event("wu", label="Warm-Up", type="warmup",
                       start="2026-03-21T12:00:00+00:00")
    globals_ = make_globals(durations={"warmup": "20m"}, alerts={"warmup": ["-15m"]})

    built = _build(event, globals_=globals_)

    assert built.session_type == SessionType.WARMUP
    assert built.duration_seconds == 1200
    assert built.alarms == ["-15m"]


def test_the_events_location_reaches_every_session_it_holds():
    event = make_event("r", location="Imola, Italy", start="2026-04-19T13:00:00+00:00",
                       note="official timetable")

    built = _build(event)

    assert built.location == "Imola, Italy"
    assert "Note: official timetable" in built.description


def test_the_round_is_named_in_the_description():
    assert "Round: 1" in _build(make_event("r", round=1)).description


def test_a_configured_status_is_honoured():
    assert _build(make_event("mine", status="TENTATIVE")).status == EventStatus.TENTATIVE


def test_a_cancelled_session_stays_cancelled_regardless_of_the_ledger():
    """Status is a field the data directory owns outright -- nothing infers it."""
    previous = VersionState(
        fingerprint="x", sequence=1, dtstamp=NOW.isoformat(),
        last_modified=NOW.isoformat(),
    )

    assert _build(make_event("mine"), previous=previous).status == EventStatus.CONFIRMED
    assert _build(make_event("mine", status="CANCELLED")).status == EventStatus.CANCELLED


def test_an_unchanged_session_keeps_its_sequence_and_dtstamp():
    event = make_event("r", start="2026-04-19T13:00:00+00:00")
    first = _build(event)
    previous = VersionState(
        fingerprint=first.fingerprint, sequence=first.sequence,
        dtstamp=first.dtstamp.isoformat(), last_modified=first.last_modified.isoformat(),
    )

    second = _build(event, previous=previous, now=datetime(2026, 6, 1, tzinfo=UTC))

    assert second.sequence == first.sequence
    assert second.dtstamp == first.dtstamp


def test_a_changed_session_advances_sequence_and_dtstamp():
    event = make_event("r", start="2026-04-19T13:00:00+00:00")
    first = _build(event)
    previous = VersionState(
        fingerprint=first.fingerprint, sequence=first.sequence,
        dtstamp=first.dtstamp.isoformat(), last_modified=first.last_modified.isoformat(),
    )
    event.name = "Renamed"

    later = datetime(2026, 6, 1, tzinfo=UTC)
    second = _build(event, previous=previous, now=later)

    assert second.sequence > first.sequence
    assert second.dtstamp == later


def test_renaming_a_series_advances_every_session_it_holds():
    event = make_event("r", start="2026-04-19T13:00:00+00:00")
    original_series = make_series(name="WEC")
    first = _build(event, series_config=original_series)
    previous = VersionState(
        fingerprint=first.fingerprint, sequence=first.sequence,
        dtstamp=first.dtstamp.isoformat(), last_modified=first.last_modified.isoformat(),
    )

    later = datetime(2026, 6, 1, tzinfo=UTC)
    renamed = _build(event, series_config=make_series(name="FIA WEC"), previous=previous, now=later)

    assert renamed.sequence > first.sequence
    assert renamed.dtstamp == later


def test_renaming_the_event_changes_every_session_it_holds():
    """The point of the shape: the weekend's name is stored once."""
    event = EventConfig(
        name="6 Hours of Emilia",
        sessions=[
            make_session("q", label="Qualifying", type="qualifying",
                         start="2026-04-18T13:00:00+00:00"),
            make_session("extra", label="Warm-Up", type="warmup",
                         start="2026-04-19T09:00:00+00:00"),
        ],
    )

    built = [
        build_published_event(
            event, session, series="wec", series_config=make_series(),
            globals_=make_globals(), previous=None, now=NOW,
        )
        for session in event.sessions
    ]

    assert [b.summary for b in built] == [
        "6 Hours of Emilia Qualifying", "6 Hours of Emilia Warm-Up",
    ]
