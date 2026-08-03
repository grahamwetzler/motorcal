from datetime import UTC, datetime

from motorcal.models import EventStatus, PublishedEvent, SessionType, session_uid
from tests.conftest import make_session


def test_session_uid_format():
    assert (
        session_uid(make_session("imsa-2026-rolex-24"), "racing.example.com")
        == "local-imsa-2026-rolex-24@racing.example.com"
    )


def test_published_event_construction():
    now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    pub = PublishedEvent(
        uid="local-wec-2026-imola-race@racing.example.com",
        series="wec",
        session_type=SessionType.RACE,
        summary="6 Hours of Imola",
        start=now,
        all_day_date=None,
        time_confirmed=True,
        duration_seconds=6 * 3600,
        location="Imola, Italy",
        description="Round 1 of WEC",
        status=EventStatus.CONFIRMED,
        sequence=1,
        dtstamp=now,
        last_modified=now,
        fingerprint="deadbeef",
        alarms=["-1d", "-30m"],
    )
    assert pub.session_type is SessionType.RACE
    assert pub.status is EventStatus.CONFIRMED


def test_session_type_values_are_fixed_vocabulary():
    assert {m.value for m in SessionType} == {
        "practice",
        "warmup",
        "qualifying",
        "hyperpole",
        "sprint_qualifying",
        "sprint",
        "race",
        "testing",
    }


def test_event_status_values_are_fixed_vocabulary():
    assert {m.value for m in EventStatus} == {"CONFIRMED", "TENTATIVE", "CANCELLED"}
