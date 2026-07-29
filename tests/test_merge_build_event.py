from datetime import datetime, timezone

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    PatchConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.merge import (
    PreviousPublishedState,
    build_published_event_from_source,
    build_published_event_from_synthetic,
)
from motorcal.models import EventStatus, SessionType, SourceEvent, SourceEventKey

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention={},
        defaults=DefaultsConfig(
            durations=DurationDefaults(),
            alerts={"race": ["-1d", "-30m"], "qualifying": ["-15m"]},
            include_sessions=["race", "qualifying"],
        ),
        unknown_time=UnknownTimeConfig(),
        series={},
    )


def _wec_race_event(time="00:00:00"):
    return SourceEvent(
        key=SourceEventKey(provider="thesportsdb", id_event="2421035"),
        series="wec",
        season="2026",
        round=1,
        name="6 Hours of Imola",
        date="2026-04-19",
        time=time,
        venue="Imola",
        country="Italy",
        raw={},
    )


def test_unconfirmed_time_produces_all_day_tbc_event_with_no_alarms():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.summary == "6 Hours of Imola (time TBC)"
    assert event.all_day_date == "2026-04-19"
    assert event.start is None
    assert event.time_confirmed is False
    assert event.alarms == []
    assert event.duration_seconds is None
    assert event.status is EventStatus.CONFIRMED
    assert event.uid == "thesportsdb-2421035@x.example.com"


def test_patch_confirms_start_and_sets_duration():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(
        id_event="2421035", start="2026-04-19T13:00:00Z", duration="6h", note="official WEC timetable"
    )
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.time_confirmed is True
    assert event.start.isoformat() == "2026-04-19T13:00:00+00:00"
    assert event.all_day_date is None
    assert event.duration_seconds == 6 * 3600
    assert "(time TBC)" not in event.summary
    assert event.alarms == ["-1d", "-30m"]  # global race defaults, since the patch has no alarms field


def test_patch_can_explicitly_reject_confirmation_of_a_non_midnight_start():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(id_event="2421035", start="2026-04-19T13:00:00Z", time_confirmed=False)
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.time_confirmed is False
    assert event.all_day_date == "2026-04-19"
    assert event.alarms == []


def test_confirmed_source_time_needs_no_patch():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.time_confirmed is True
    assert event.start.isoformat() == "2026-04-19T13:00:00+00:00"


def test_disappeared_future_event_becomes_cancelled():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),  # 2026-04-19, well after NOW (2026-07-29)...
        session_type=SessionType.RACE,
        is_disappeared=True,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),  # NOW is before the event's date
    )
    assert event.status is EventStatus.CANCELLED


def test_disappeared_past_event_is_not_retroactively_cancelled():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    previous = PreviousPublishedState(
        fingerprint="irrelevant-for-this-test", sequence=5, dtstamp="t0", last_modified="t0",
        status="CONFIRMED",
    )
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),  # 2026-04-19
        session_type=SessionType.RACE,
        is_disappeared=True,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=previous,
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),  # well after the event's own scheduled time
    )
    assert event.status is EventStatus.CONFIRMED  # NOT cancelled — it's in the past


def test_cancellation_is_sticky_across_a_later_rebuild_after_the_event_has_passed():
    # An event cancelled while it was still in the future must STAY cancelled on a later
    # rebuild, even after its own scheduled time has since passed -- it must never flip
    # back to CONFIRMED just because "is this still future/active" would now say no.
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    previously_cancelled = PreviousPublishedState(
        fingerprint="irrelevant-for-this-test", sequence=5, dtstamp="t0", last_modified="t0",
        status="CANCELLED",
    )
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),  # 2026-04-19
        session_type=SessionType.RACE,
        is_disappeared=True,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=previously_cancelled,
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),  # long after the event's own scheduled time
    )
    assert event.status is EventStatus.CANCELLED


def test_disappearance_cancellation_overrides_a_patch_status():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(id_event="2421035", status="TENTATIVE", note="postponed")
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=True,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert event.status is EventStatus.CANCELLED  # disappearance wins over the patch's TENTATIVE


def test_patch_status_applies_when_not_disappeared():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(id_event="2421035", status="TENTATIVE", note="postponed")
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.status is EventStatus.TENTATIVE


def test_unchanged_rebuild_preserves_sequence_and_timestamps():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(id_event="2421035", start="2026-04-19T13:00:00Z", duration="6h")
    first = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    previous = PreviousPublishedState(
        fingerprint=first.fingerprint,
        sequence=first.sequence,
        dtstamp=first.dtstamp.isoformat(),
        last_modified=first.last_modified.isoformat(),
        status=first.status.value,
    )
    later = datetime(2026, 8, 1, tzinfo=timezone.utc)
    second = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=previous,
        now=later,
    )
    assert second.sequence == first.sequence
    assert second.dtstamp == first.dtstamp
    assert second.last_modified == first.last_modified
    assert second.fingerprint == first.fingerprint


def test_changed_rebuild_bumps_sequence_and_updates_timestamps():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch1 = PatchConfig(id_event="2421035", start="2026-04-19T13:00:00Z", duration="6h")
    first = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch1,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    previous = PreviousPublishedState(
        fingerprint=first.fingerprint,
        sequence=first.sequence,
        dtstamp=first.dtstamp.isoformat(),
        last_modified=first.last_modified.isoformat(),
        status=first.status.value,
    )
    patch2 = PatchConfig(id_event="2421035", start="2026-04-19T14:00:00Z", duration="6h")  # time changed
    later = datetime(2026, 8, 1, tzinfo=timezone.utc)
    second = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch2,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=previous,
        now=later,
    )
    assert second.sequence > first.sequence
    assert second.dtstamp != first.dtstamp
    assert second.fingerprint != first.fingerprint


def test_race_only_series_note_appears_in_description():
    series_cfg = SeriesConfig(league_id=4373, name="IndyCar", max_round=30, race_only=True)
    event = build_published_event_from_source(
        source_event=SourceEvent(
            key=SourceEventKey(provider="thesportsdb", id_event="1"),
            series="indycar", season="2026", round=1,
            name="Firestone Grand Prix of St. Petersburg", date="2026-03-01",
            time="17:00:00", venue="St. Petersburg", country="USA", raw={},
        ),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=True,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert "race" in event.description.lower()


def test_synthetic_event_uses_local_uid_format_and_own_alarms():
    event = build_published_event_from_synthetic(
        uid="imsa-2026-rolex-24",
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z",
        date=None,
        duration_seconds=24 * 3600,
        location=None,
        note="official IMSA timetable",
        alarms=["-1d", "-30m"],
        configured_status="CONFIRMED",
        is_removed=False,
        uid_domain="x.example.com",
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.uid == "local-imsa-2026-rolex-24@x.example.com"
    assert event.status is EventStatus.CONFIRMED
    assert event.duration_seconds == 24 * 3600
    assert event.alarms == ["-1d", "-30m"]
    assert event.synthetic_uid == "imsa-2026-rolex-24"
    assert event.source_id_event is None


def test_synthetic_event_removed_from_config_produces_cancelled_status():
    event = build_published_event_from_synthetic(
        uid="imsa-2026-rolex-24",
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z",
        date=None,
        duration_seconds=24 * 3600,
        location=None,
        note=None,
        alarms=[],
        configured_status="CONFIRMED",
        is_removed=True,
        uid_domain="x.example.com",
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.status is EventStatus.CANCELLED


def test_synthetic_event_honors_configured_tentative_status():
    event = build_published_event_from_synthetic(
        uid="imsa-2026-rolex-24",
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z",
        date=None,
        duration_seconds=24 * 3600,
        location=None,
        note=None,
        alarms=[],
        configured_status="TENTATIVE",
        is_removed=False,
        uid_domain="x.example.com",
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.status is EventStatus.TENTATIVE


def test_synthetic_event_honors_configured_cancelled_status_while_still_present():
    event = build_published_event_from_synthetic(
        uid="imsa-2026-rolex-24",
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z",
        date=None,
        duration_seconds=24 * 3600,
        location=None,
        note=None,
        alarms=[],
        configured_status="CANCELLED",
        is_removed=False,
        uid_domain="x.example.com",
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.status is EventStatus.CANCELLED


def test_synthetic_event_with_date_only_is_all_day():
    event = build_published_event_from_synthetic(
        uid="event-date-only",
        series="wec",
        summary="Test Event",
        start=None,
        date="2026-06-01",
        duration_seconds=None,
        location=None,
        note=None,
        alarms=[],
        configured_status="CONFIRMED",
        is_removed=False,
        uid_domain="x.example.com",
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.all_day_date == "2026-06-01"
    assert event.start is None
    assert event.time_confirmed is True  # a synthetic date-only event is deliberately configured, not TBC
