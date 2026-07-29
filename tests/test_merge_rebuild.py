import json
from datetime import datetime, timezone

from motorcal.config import (
    DefaultsConfig,
    OverridesConfig,
    PatchConfig,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    SyntheticEventConfig,
    UnknownTimeConfig,
)
from motorcal.merge import rebuild_publication, reconcile_synthetic_events
from motorcal.models import source_uid, synthetic_event_uid
from motorcal.store import (
    connect,
    get_published_event,
    get_source_event,
    init_schema,
    transaction,
    upsert_source_event,
)

UID_DOMAIN = "x.example.com"


def _root_config(series=None):
    return RootConfig(
        server={"base_url": f"https://{UID_DOMAIN}", "uid_domain": UID_DOMAIN},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(historical_days=180, cancelled_after_event_days=90),
        defaults=DefaultsConfig(
            durations={},
            alerts={"race": ["-1d"]},
            include_sessions=["race"],
        ),
        unknown_time=UnknownTimeConfig(),
        series=series or {"wec": SeriesConfig(league_id=4413, name="WEC", max_round=20)},
    )


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_rebuild_publishes_a_confirmed_source_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-04-19", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert report.events_published == 1
    uid = source_uid("1", UID_DOMAIN)
    row = get_published_event(conn, uid)
    assert row is not None
    assert row["status"] == "CONFIRMED"


def test_rebuild_applies_a_matched_patch(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-04-19", time="00:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )
    overrides = OverridesConfig(
        patches=[PatchConfig(id_event="1", start="2026-04-19T13:00:00Z", duration="6h")]
    )

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=overrides,
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert report.patch_errors == []
    row = get_published_event(conn, source_uid("1", UID_DOMAIN))
    assert row["time_confirmed"] == 1
    assert row["duration_seconds"] == 6 * 3600


def test_rebuild_reports_an_unmatched_patch_as_an_error_without_crashing(tmp_path):
    conn = _fresh_conn(tmp_path)
    overrides = OverridesConfig(patches=[PatchConfig(id_event="does-not-exist")])

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=overrides,
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert len(report.patch_errors) == 1
    assert report.patch_errors[0].reason == "no_match"


def test_rebuild_cancels_a_disappeared_future_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-04-19", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )
    rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with transaction(conn):
        from motorcal.store import mark_source_event_disappeared
        mark_source_event_disappeared(conn, "thesportsdb", "1", "t1")

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 2, tzinfo=timezone.utc),  # still before the event
    )

    assert report.events_cancelled == 1
    row = get_published_event(conn, source_uid("1", UID_DOMAIN))
    assert row["status"] == "CANCELLED"


def test_rebuild_publishes_a_synthetic_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24", series="imsa", summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z", duration="24h",
    )
    with transaction(conn):
        reconcile_synthetic_events(conn, [cfg], now="t0")

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(events=[cfg]),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert report.events_published == 1
    uid = synthetic_event_uid("imsa-2026-rolex-24", UID_DOMAIN)
    row = get_published_event(conn, uid)
    assert row is not None
    assert row["duration_seconds"] == 24 * 3600


def test_rebuild_prunes_a_long_cancelled_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-01-01", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )
    rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2025, 12, 1, tzinfo=timezone.utc),
    )
    with transaction(conn):
        from motorcal.store import mark_source_event_disappeared
        mark_source_event_disappeared(conn, "thesportsdb", "1", "t1")
    rebuild_publication(  # this rebuild cancels it (event was still in the future relative to this `now`)
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2025, 12, 2, tzinfo=timezone.utc),
    )

    # Now simulate 91+ days after the (cancelled) event's own scheduled end (cancelled_after_event_days=90)
    far_future = datetime(2026, 4, 15, tzinfo=timezone.utc)  # >90 days after 2026-01-01
    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=far_future,
    )

    assert report.events_pruned >= 1
    assert get_published_event(conn, source_uid("1", UID_DOMAIN)) is None


def test_rebuild_prunes_a_long_past_non_cancelled_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-01-01", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )
    rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    far_future = datetime(2026, 7, 1, tzinfo=timezone.utc)  # >180 days after 2026-01-01
    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=far_future,
    )

    assert report.events_pruned >= 1
    assert get_published_event(conn, source_uid("1", UID_DOMAIN)) is None
    assert get_source_event(conn, "thesportsdb", "1") is None


def test_rebuild_reports_unknown_classified_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="Drivers Parade", date="2026-04-19", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert len(report.unknown_events) == 1
    assert report.events_published == 1  # still published — unknown is visible, not dropped
