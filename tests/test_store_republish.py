from motorcal.store import (
    connect,
    force_advance_all_sequences,
    init_schema,
    list_published_events,
    transaction,
    upsert_published_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert(conn, uid, sequence):
    upsert_published_event(
        conn, uid=uid, series="wec", session_type="race", summary="S", start=None,
        all_day_date="2026-01-01", time_confirmed=False, duration_seconds=None, location=None,
        description="D", status="CONFIRMED", sequence=sequence, dtstamp="t0", last_modified="t0",
        fingerprint="fp", alarms_json="[]", source_provider="thesportsdb", source_id_event="1",
        synthetic_uid=None, cancelled_at=None, retain_until=None,
    )


def test_force_advance_bumps_sequences_below_the_target(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "u1", sequence=100)

    count = force_advance_all_sequences(conn, now_unix_minute=500000000, now_iso="2026-08-01T00:00:00+00:00")

    assert count == 1
    rows = {row["uid"]: row for row in list_published_events(conn)}
    assert rows["u1"]["sequence"] == 500000000
    assert rows["u1"]["last_modified"] == "2026-08-01T00:00:00+00:00"


def test_force_advance_leaves_already_ahead_sequences_untouched(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "u1", sequence=99999999999)  # already far ahead of any real "now"

    count = force_advance_all_sequences(conn, now_unix_minute=500000000, now_iso="2026-08-01T00:00:00+00:00")

    assert count == 0
    rows = {row["uid"]: row for row in list_published_events(conn)}
    assert rows["u1"]["sequence"] == 99999999999
    assert rows["u1"]["last_modified"] == "t0"  # untouched


def test_force_advance_handles_multiple_events_independently(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "below", sequence=1)
        _insert(conn, "above", sequence=999999999999)

    count = force_advance_all_sequences(conn, now_unix_minute=500000000, now_iso="t1")

    assert count == 1
    rows = {row["uid"]: row for row in list_published_events(conn)}
    assert rows["below"]["sequence"] == 500000000
    assert rows["above"]["sequence"] == 999999999999
