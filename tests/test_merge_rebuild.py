from datetime import datetime, timezone

from tests.conftest import UID_DOMAIN, make_config, make_event, make_series, make_state

from motorcal.merge import rebuild_publication
from motorcal.state import VersionState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _config(wec_events=None, imsa_events=None, **kwargs):
    return make_config(
        series={
            "wec": make_series(events=wec_events or []),
            "imsa": make_series(name="IMSA", events=imsa_events or []),
        },
        **kwargs,
    )


def _find(published, uid):
    return next((e for events in published.values() for e in events if e.uid == uid), None)


def test_rebuild_publishes_every_configured_event():
    config = _config(wec_events=[make_event("r1", start="2026-04-19T13:00:00+00:00")])
    state = make_state()

    published, report = rebuild_publication(config, state, now=NOW)

    assert report.events_published == 1
    assert _find(published, f"local-r1@{UID_DOMAIN}") is not None


def test_rebuild_groups_events_by_series():
    config = _config(
        wec_events=[make_event("r1", start="2026-04-19T13:00:00+00:00")],
        imsa_events=[make_event("r2", start="2026-04-19T13:00:00+00:00")],
    )

    published, _ = rebuild_publication(config, make_state(), now=NOW)

    assert len(published["wec"]) == 1
    assert len(published["imsa"]) == 1


def test_a_series_with_no_events_yields_an_empty_list_not_a_missing_key():
    published, _ = rebuild_publication(_config(), make_state(), now=NOW)

    assert published["wec"] == []
    assert published["imsa"] == []


def test_rebuild_records_the_version_ledger():
    config = _config(wec_events=[make_event("r1", start="2026-04-19T13:00:00+00:00")])
    state = make_state()

    published, _ = rebuild_publication(config, state, now=NOW)

    built = published["wec"][0]
    assert state.versions[built.uid] == VersionState(
        fingerprint=built.fingerprint, sequence=built.sequence,
        dtstamp=built.dtstamp.isoformat(), last_modified=built.last_modified.isoformat(),
        status=built.status.value,
    )


def test_rebuild_counts_cancelled_events():
    config = _config(wec_events=[
        make_event("r1", start="2026-04-19T13:00:00+00:00", status="CANCELLED")
    ])

    _, report = rebuild_publication(config, make_state(), now=NOW)

    assert report.events_cancelled == 1


def test_rebuild_is_idempotent_for_unchanged_input():
    config = _config(wec_events=[make_event("r1", start="2026-04-19T13:00:00+00:00")])
    state = make_state()
    first, _ = rebuild_publication(config, state, now=NOW)

    second, _ = rebuild_publication(config, state, now=datetime(2026, 6, 1, tzinfo=timezone.utc))

    assert second["wec"][0].sequence == first["wec"][0].sequence
    assert second["wec"][0].dtstamp == first["wec"][0].dtstamp


def test_a_long_past_session_drops_out_of_the_feed_and_the_ledger():
    config = _config(wec_events=[make_event("r1", start="2026-01-01T13:00:00+00:00")])
    state = make_state()
    rebuild_publication(config, state, now=NOW)

    # >180 days (historical_days) after the event
    published, report = rebuild_publication(
        config, state, now=datetime(2026, 8, 1, tzinfo=timezone.utc)
    )

    assert report.events_pruned == 1
    assert published["wec"] == []
    assert state.versions == {}
    # The data directory is never touched -- this process only reads it.
    assert len(config.series["wec"].events) == 1


def test_a_long_cancelled_event_is_pruned_on_the_shorter_window():
    config = _config(wec_events=[
        make_event("r1", start="2026-01-01T13:00:00+00:00", status="CANCELLED")
    ])
    state = make_state()
    rebuild_publication(config, state, now=datetime(2025, 12, 1, tzinfo=timezone.utc))

    # >90 days (cancelled_after_event_days), but < the 180-day historical window
    published, report = rebuild_publication(
        config, state, now=datetime(2026, 4, 15, tzinfo=timezone.utc)
    )

    assert report.events_pruned == 1
    assert published["wec"] == []


def test_a_future_event_is_never_pruned():
    config = _config(wec_events=[make_event("r1", start="2099-01-01T13:00:00+00:00")])

    published, report = rebuild_publication(config, make_state(), now=NOW)

    assert report.events_pruned == 0
    assert len(published["wec"]) == 1


def test_pruning_one_series_leaves_another_untouched():
    config = _config(
        wec_events=[make_event("r1", start="2026-01-01T13:00:00+00:00")],
        imsa_events=[make_event("r2", start="2099-01-01T13:00:00+00:00")],
    )
    state = make_state()
    rebuild_publication(config, state, now=NOW)

    published, _ = rebuild_publication(config, state, now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert published["wec"] == []
    assert len(published["imsa"]) == 1


def test_a_ledger_entry_for_a_deleted_session_is_dropped():
    """Nothing later revisits an orphan, so the rebuild that orphans it must clean up."""
    config = _config(wec_events=[make_event("r1", start="2026-04-19T13:00:00+00:00")])
    state = make_state()
    rebuild_publication(config, state, now=NOW)
    assert f"local-r1@{UID_DOMAIN}" in state.versions

    config.series["wec"].events = []
    rebuild_publication(config, state, now=NOW)

    assert state.versions == {}
