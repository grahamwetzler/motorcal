from datetime import datetime, timezone

from tests.conftest import (
    UID_DOMAIN,
    make_config,
    make_series,
    make_state,
    manual_event,
    source_event,
)

from motorcal.merge import rebuild_publication
from motorcal.state import VersionState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _config(wec_events=None, imsa_events=None, **kwargs):
    return make_config(
        series={
            "wec": make_series(events=wec_events or []),
            "imsa": make_series(
                league_id=4488, name="IMSA", max_round=30, race_only=True,
                events=imsa_events or [],
            ),
        },
        **kwargs,
    )


def _find(published, uid):
    return next((e for events in published.values() for e in events if e.uid == uid), None)


def test_rebuild_publishes_every_configured_event():
    config = _config(wec_events=[source_event("1", time="13:00:00")])
    state = make_state()

    published, report = rebuild_publication(config, state, now=NOW)

    assert report.events_published == 1
    assert _find(published, f"thesportsdb-1@{UID_DOMAIN}") is not None


def test_rebuild_groups_events_by_series():
    config = _config(
        wec_events=[source_event("1", time="13:00:00")],
        imsa_events=[source_event("2", time="13:00:00")],
    )

    published, _ = rebuild_publication(config, make_state(), now=NOW)

    assert len(published["wec"]) == 1
    assert len(published["imsa"]) == 1


def test_a_series_with_no_events_yields_an_empty_list_not_a_missing_key():
    published, _ = rebuild_publication(_config(), make_state(), now=NOW)

    assert published["wec"] == []
    assert published["imsa"] == []


def test_rebuild_records_the_version_ledger():
    config = _config(wec_events=[source_event("1", time="13:00:00")])
    state = make_state()

    published, _ = rebuild_publication(config, state, now=NOW)

    built = published["wec"][0]
    assert state.versions[built.uid] == VersionState(
        fingerprint=built.fingerprint, sequence=built.sequence,
        dtstamp=built.dtstamp.isoformat(), last_modified=built.last_modified.isoformat(),
        status=built.status.value,
    )


def test_rebuild_publishes_manual_events_alongside_provider_ones():
    config = _config(wec_events=[source_event("1", time="13:00:00"), manual_event("mine")])

    published, report = rebuild_publication(config, make_state(), now=NOW)

    assert report.events_published == 2
    assert _find(published, f"local-mine@{UID_DOMAIN}") is not None


def test_rebuild_reports_unknown_classified_sessions():
    config = _config(wec_events=[
        source_event("1", name="Drivers Parade", event_name="6 Hours of Imola",
                     time="13:00:00")
    ])

    published, report = rebuild_publication(config, make_state(), now=NOW)

    assert len(report.unknown_events) == 1
    assert report.events_published == 1  # still published, not dropped


def test_rebuild_counts_cancelled_events():
    config = _config(wec_events=[source_event("1", time="13:00:00", disappeared_at="t1")])

    _, report = rebuild_publication(config, make_state(), now=NOW)

    assert report.events_cancelled == 1


def test_rebuild_is_idempotent_for_unchanged_input():
    config = _config(wec_events=[source_event("1", time="13:00:00")])
    state = make_state()
    first, _ = rebuild_publication(config, state, now=NOW)

    second, _ = rebuild_publication(config, state, now=datetime(2026, 6, 1, tzinfo=timezone.utc))

    assert second["wec"][0].sequence == first["wec"][0].sequence
    assert second["wec"][0].dtstamp == first["wec"][0].dtstamp


def test_a_long_past_session_takes_its_emptied_event_with_it():
    config = _config(wec_events=[source_event("1", date="2026-01-01", time="13:00:00")])
    state = make_state()
    rebuild_publication(config, state, now=NOW)

    # >180 days (historical_days) after the event
    published, report = rebuild_publication(
        config, state, now=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )

    assert report.events_pruned == 1
    assert published["wec"] == []
    assert config.series["wec"].events == []
    assert state.versions == {}


def test_a_long_cancelled_event_is_pruned_on_the_shorter_window():
    config = _config(
        wec_events=[source_event("1", date="2026-01-01", time="13:00:00", disappeared_at="t1")]
    )
    state = make_state()
    rebuild_publication(config, state, now=datetime(2025, 12, 1, tzinfo=timezone.utc))

    # >90 days (cancelled_after_event_days), but < the 180-day historical window
    _, report = rebuild_publication(config, state, now=datetime(2026, 4, 15, tzinfo=timezone.utc))

    assert report.events_pruned == 1
    assert config.series["wec"].events == []


def test_a_future_event_is_never_pruned():
    config = _config(wec_events=[source_event("1", date="2099-01-01", time="13:00:00")])
    state = make_state()

    _, report = rebuild_publication(config, state, now=NOW)

    assert report.events_pruned == 0
    assert len(config.series["wec"].events) == 1


def test_pruning_one_series_leaves_another_untouched():
    config = _config(
        wec_events=[source_event("1", date="2026-01-01", time="13:00:00")],
        imsa_events=[source_event("2", date="2099-01-01", time="13:00:00")],
    )
    state = make_state()
    rebuild_publication(config, state, now=NOW)

    rebuild_publication(config, state, now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert config.series["wec"].events == []
    assert len(config.series["imsa"].events) == 1
