from motorcal.store import (
    connect,
    get_feed_revision,
    init_schema,
    list_published_events_by_series,
    transaction,
    upsert_feed_revision,
    upsert_published_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert_published(conn, uid, series):
    upsert_published_event(
        conn, uid=uid, series=series, session_type="race", summary="S",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=3600, location="L", description="D", status="CONFIRMED",
        sequence=1, dtstamp="t0", last_modified="t0", fingerprint="fp", alarms_json="[]",
        source_provider="thesportsdb", source_id_event="1", synthetic_uid=None,
        cancelled_at=None, retain_until=None,
    )


def test_list_published_events_by_series_filters_correctly(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", series="wec")
        _insert_published(conn, "u2", series="wec")
        _insert_published(conn, "u3", series="f1")

    rows = list_published_events_by_series(conn, "wec")
    assert {row["uid"] for row in rows} == {"u1", "u2"}


def test_feed_revision_round_trip(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_feed_revision(conn, "wec") is None

    with transaction(conn):
        upsert_feed_revision(conn, "wec", "abc123", "t0")

    row = get_feed_revision(conn, "wec")
    assert row["revision"] == "abc123"
    assert row["updated_at"] == "t0"


def test_feed_revision_upsert_replaces_previous_values(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_feed_revision(conn, "wec", "abc123", "t0")
    with transaction(conn):
        upsert_feed_revision(conn, "wec", "def456", "t1")

    row = get_feed_revision(conn, "wec")
    assert row["revision"] == "def456"
    assert row["updated_at"] == "t1"
