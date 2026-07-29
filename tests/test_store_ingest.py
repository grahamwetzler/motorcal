import json

from motorcal.providers.thesportsdb import ProviderEvent, SnapshotResult
from motorcal.store import (
    connect,
    get_source_event,
    init_schema,
    ingest_snapshot,
    transaction,
    upsert_source_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _event(id_event, name="Event", round_number=1, season="2026", series="wec"):
    return ProviderEvent(
        id_event=id_event,
        name=name,
        date="2026-04-19",
        time="00:00:00",
        round=round_number,
        season=season,
        series=series,
        venue="Venue",
        country="Country",
        raw={"idEvent": id_event},
    )


def test_incomplete_snapshot_is_discarded_in_full(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="Original", date="2026-04-19", time="00:00:00",
            venue="V", country="C", raw_json="{}", seen_at="t0",
        )

    snapshot = SnapshotResult(complete=False, events=[_event("1", name="Changed")], diagnostics=["round 2: boom"], rounds_attempted=2, rounds_failed=1)
    result = ingest_snapshot(
        conn, snapshot, provider="thesportsdb", series="wec", season="2026",
        now="t1", is_current_season=True,
    )

    assert result.committed is False
    assert result.reason == "incomplete_snapshot"
    assert result.events_written == 0
    row = get_source_event(conn, "thesportsdb", "1")
    assert row["name"] == "Original"  # untouched


def test_complete_snapshot_with_events_is_committed(tmp_path):
    conn = _fresh_conn(tmp_path)
    snapshot = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)

    result = ingest_snapshot(
        conn, snapshot, provider="thesportsdb", series="wec", season="2026",
        now="t1", is_current_season=True,
    )

    assert result.committed is True
    assert result.reason is None
    assert result.events_written == 2
    assert get_source_event(conn, "thesportsdb", "1") is not None
    assert get_source_event(conn, "thesportsdb", "2") is not None


def test_disappearance_marks_source_event_but_does_not_delete_it(tmp_path):
    conn = _fresh_conn(tmp_path)
    first = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    ingest_snapshot(conn, first, provider="thesportsdb", series="wec", season="2026", now="t1", is_current_season=True)

    second = SnapshotResult(complete=True, events=[_event("1")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    result = ingest_snapshot(conn, second, provider="thesportsdb", series="wec", season="2026", now="t2", is_current_season=True)

    assert result.committed is True
    row1 = get_source_event(conn, "thesportsdb", "1")
    row2 = get_source_event(conn, "thesportsdb", "2")
    assert row1["disappeared_at"] is None
    assert row2["disappeared_at"] == "t2"  # marked, not deleted


def test_reappearance_reactivates_the_same_source_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    first = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    ingest_snapshot(conn, first, provider="thesportsdb", series="wec", season="2026", now="t1", is_current_season=True)
    second = SnapshotResult(complete=True, events=[_event("1")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    ingest_snapshot(conn, second, provider="thesportsdb", series="wec", season="2026", now="t2", is_current_season=True)

    third = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    result = ingest_snapshot(conn, third, provider="thesportsdb", series="wec", season="2026", now="t3", is_current_season=True)

    assert result.committed is True
    row2 = get_source_event(conn, "thesportsdb", "2")
    assert row2["disappeared_at"] is None  # reactivated


def test_incomplete_snapshot_does_not_touch_disappearance_state(tmp_path):
    conn = _fresh_conn(tmp_path)
    first = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    ingest_snapshot(conn, first, provider="thesportsdb", series="wec", season="2026", now="t1", is_current_season=True)

    incomplete = SnapshotResult(complete=False, events=[_event("1")], diagnostics=["round 2: boom"], rounds_attempted=2, rounds_failed=1)
    ingest_snapshot(conn, incomplete, provider="thesportsdb", series="wec", season="2026", now="t2", is_current_season=True)

    row2 = get_source_event(conn, "thesportsdb", "2")
    assert row2["disappeared_at"] is None  # not marked — incomplete snapshots never touch disappearance


def test_empty_snapshot_for_current_season_is_always_suspicious(tmp_path):
    conn = _fresh_conn(tmp_path)
    empty = SnapshotResult(complete=True, events=[], diagnostics=[], rounds_attempted=5, rounds_failed=0)

    result = ingest_snapshot(
        conn, empty, provider="thesportsdb", series="wec", season="2026",
        now="t1", is_current_season=True,
    )

    assert result.committed is False
    assert result.reason == "suspicious_empty_current_season"


def test_empty_snapshot_for_brand_new_future_season_is_accepted(tmp_path):
    conn = _fresh_conn(tmp_path)
    empty = SnapshotResult(complete=True, events=[], diagnostics=[], rounds_attempted=5, rounds_failed=0)

    result = ingest_snapshot(
        conn, empty, provider="thesportsdb", series="wec", season="2027",
        now="t1", is_current_season=False,
    )

    assert result.committed is True
    assert result.events_written == 0


def test_empty_snapshot_for_previously_populated_future_season_is_suspicious(tmp_path):
    conn = _fresh_conn(tmp_path)
    populated = SnapshotResult(complete=True, events=[_event("1", season="2027")], diagnostics=[], rounds_attempted=5, rounds_failed=0)
    ingest_snapshot(conn, populated, provider="thesportsdb", series="wec", season="2027", now="t1", is_current_season=False)

    empty = SnapshotResult(complete=True, events=[], diagnostics=[], rounds_attempted=5, rounds_failed=0)
    result = ingest_snapshot(
        conn, empty, provider="thesportsdb", series="wec", season="2027",
        now="t2", is_current_season=False,
    )

    assert result.committed is False
    assert result.reason == "suspicious_empty_future_season"
    assert get_source_event(conn, "thesportsdb", "1") is not None  # untouched, not disappeared
