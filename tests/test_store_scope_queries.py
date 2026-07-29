from motorcal.store import (
    connect,
    get_snapshot_meta,
    init_schema,
    list_source_events_by_scope,
    mark_source_event_disappeared,
    transaction,
    upsert_snapshot_meta,
    upsert_source_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert(conn, id_event, series="wec", season="2026", seen_at="2026-07-29T00:00:00+00:00"):
    upsert_source_event(
        conn,
        provider="thesportsdb",
        id_event=id_event,
        series=series,
        season=season,
        round=1,
        name=f"Event {id_event}",
        date="2026-04-19",
        time="00:00:00",
        venue="Venue",
        country="Country",
        raw_json="{}",
        seen_at=seen_at,
    )


def test_list_source_events_by_scope_filters_correctly(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "1", series="wec", season="2026")
        _insert(conn, "2", series="wec", season="2026")
        _insert(conn, "3", series="wec", season="2027")  # different season
        _insert(conn, "4", series="f1", season="2026")  # different series

    rows = list_source_events_by_scope(conn, "thesportsdb", "wec", "2026")
    ids = {row["id_event"] for row in rows}
    assert ids == {"1", "2"}


def test_list_source_events_by_scope_empty_when_none_match(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert list_source_events_by_scope(conn, "thesportsdb", "wec", "2026") == []


def test_snapshot_meta_round_trip(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_snapshot_meta(conn, "thesportsdb", "wec", "2026") is None

    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", "2026", "2026-07-29T00:00:00+00:00", 5)

    row = get_snapshot_meta(conn, "thesportsdb", "wec", "2026")
    assert row["last_complete_at"] == "2026-07-29T00:00:00+00:00"
    assert row["last_event_count"] == 5


def test_snapshot_meta_upsert_replaces_previous_values(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", "2026", "2026-07-29T00:00:00+00:00", 5)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", "2026", "2026-08-01T00:00:00+00:00", 7)

    row = get_snapshot_meta(conn, "thesportsdb", "wec", "2026")
    assert row["last_complete_at"] == "2026-08-01T00:00:00+00:00"
    assert row["last_event_count"] == 7


def test_mark_source_event_disappeared(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "1")

    with transaction(conn):
        mark_source_event_disappeared(conn, "thesportsdb", "1", "2026-08-01T00:00:00+00:00")

    rows = list_source_events_by_scope(conn, "thesportsdb", "wec", "2026")
    assert rows[0]["disappeared_at"] == "2026-08-01T00:00:00+00:00"


def test_upsert_source_event_reactivates_a_disappeared_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "1", seen_at="t1")
    with transaction(conn):
        mark_source_event_disappeared(conn, "thesportsdb", "1", "t2")

    rows = list_source_events_by_scope(conn, "thesportsdb", "wec", "2026")
    assert rows[0]["disappeared_at"] == "t2"

    with transaction(conn):
        _insert(conn, "1", seen_at="t3")  # reappears

    rows = list_source_events_by_scope(conn, "thesportsdb", "wec", "2026")
    assert rows[0]["disappeared_at"] is None  # reactivated (Phase 2's existing ON CONFLICT clause)
