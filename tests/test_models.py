from datetime import datetime, timezone

from motorcal.models import (
    EventStatus,
    PublishedEvent,
    SessionType,
    SourceEvent,
    SourceEventKey,
    source_uid,
    synthetic_event_uid,
)


def test_source_uid_format():
    assert source_uid("2421035", "racing.example.com") == "thesportsdb-2421035@racing.example.com"


def test_synthetic_event_uid_format():
    assert (
        synthetic_event_uid("imsa-2026-rolex-24", "racing.example.com")
        == "local-imsa-2026-rolex-24@racing.example.com"
    )


def test_source_event_key_is_hashable_and_frozen():
    key1 = SourceEventKey(provider="thesportsdb", id_event="2421035")
    key2 = SourceEventKey(provider="thesportsdb", id_event="2421035")
    assert key1 == key2
    assert hash(key1) == hash(key2)


def test_source_event_construction():
    ev = SourceEvent(
        key=SourceEventKey(provider="thesportsdb", id_event="2421035"),
        series="wec",
        season="2026",
        round=1,
        name="6 Hours of Imola",
        date="2026-04-19",
        time="00:00:00",
        venue="Autodromo Enzo e Dino Ferrari",
        country="Italy",
        raw={"idEvent": "2421035"},
    )
    assert ev.series == "wec"
    assert ev.time == "00:00:00"


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
        source_id_event="2421035",
        synthetic_uid=None,
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
