from datetime import datetime, timezone

from tests.conftest import manual_event, source_event

from motorcal.models import EventStatus, PublishedEvent, SessionType, event_uid


def test_provider_backed_uid_format():
    assert (
        event_uid(source_event("2421035"), "racing.example.com")
        == "thesportsdb-2421035@racing.example.com"
    )


def test_manual_event_uid_format():
    assert (
        event_uid(manual_event("imsa-2026-rolex-24"), "racing.example.com")
        == "local-imsa-2026-rolex-24@racing.example.com"
    )


def test_published_event_construction():
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    pub = PublishedEvent(
        uid="thesportsdb-2421035@racing.example.com",
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
        event_key="2421035",
    )
    assert pub.session_type is SessionType.RACE
    assert pub.status is EventStatus.CONFIRMED


def test_session_type_values_are_fixed_vocabulary():
    assert {m.value for m in SessionType} == {
        "practice",
        "qualifying",
        "hyperpole",
        "sprint_qualifying",
        "sprint",
        "race",
        "testing",
        "unknown",
    }


def test_event_status_values_are_fixed_vocabulary():
    assert {m.value for m in EventStatus} == {"CONFIRMED", "TENTATIVE", "CANCELLED"}
