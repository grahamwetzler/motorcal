from motorcal.store import (
    connect,
    get_synthetic_event,
    init_schema,
    list_synthetic_events,
    mark_synthetic_event_removed,
    transaction,
    upsert_synthetic_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert(conn, uid, **overrides):
    defaults = dict(
        uid=uid,
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00+00:00",
        date=None,
        duration_seconds=24 * 3600,
        location=None,
        status="CONFIRMED",
        note="official IMSA timetable",
        alarms_json="[]",
    )
    defaults.update(overrides)
    upsert_synthetic_event(conn, **defaults)


def test_upsert_and_get_synthetic_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "imsa-2026-rolex-24")

    row = get_synthetic_event(conn, "imsa-2026-rolex-24")
    assert row["summary"] == "Rolex 24 at Daytona"
    assert row["duration_seconds"] == 24 * 3600
    assert row["present_in_config"] == 1
    assert row["cancelled_at"] is None


def test_get_synthetic_event_returns_none_when_missing(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_synthetic_event(conn, "does-not-exist") is None


def test_list_synthetic_events_returns_all_rows(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "uid-1")
        _insert(conn, "uid-2")

    rows = list_synthetic_events(conn)
    assert {row["uid"] for row in rows} == {"uid-1", "uid-2"}


def test_mark_synthetic_event_removed_sets_flags(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "uid-1")

    with transaction(conn):
        mark_synthetic_event_removed(conn, "uid-1", "2026-08-01T00:00:00+00:00")

    row = get_synthetic_event(conn, "uid-1")
    assert row["present_in_config"] == 0
    assert row["cancelled_at"] == "2026-08-01T00:00:00+00:00"


def test_mark_synthetic_event_removed_does_not_overwrite_existing_cancellation(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "uid-1")
    with transaction(conn):
        mark_synthetic_event_removed(conn, "uid-1", "2026-08-01T00:00:00+00:00")
    with transaction(conn):
        mark_synthetic_event_removed(conn, "uid-1", "2026-09-01T00:00:00+00:00")  # should be a no-op

    row = get_synthetic_event(conn, "uid-1")
    assert row["cancelled_at"] == "2026-08-01T00:00:00+00:00"  # unchanged


def test_upsert_reactivates_a_removed_synthetic_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "uid-1")
    with transaction(conn):
        mark_synthetic_event_removed(conn, "uid-1", "2026-08-01T00:00:00+00:00")

    row = get_synthetic_event(conn, "uid-1")
    assert row["present_in_config"] == 0
    assert row["cancelled_at"] == "2026-08-01T00:00:00+00:00"

    with transaction(conn):
        _insert(conn, "uid-1")  # reappears in config

    row = get_synthetic_event(conn, "uid-1")
    assert row["present_in_config"] == 1
    assert row["cancelled_at"] is None  # reactivated
