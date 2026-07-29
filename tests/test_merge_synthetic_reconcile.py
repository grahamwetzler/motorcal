from motorcal.config import SyntheticEventConfig
from motorcal.merge import reconcile_synthetic_events
from motorcal.store import connect, get_synthetic_event, init_schema, list_synthetic_events


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_reconcile_creates_new_synthetic_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24",
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z",
        duration="24h",
        note="official IMSA timetable",
    )

    reconcile_synthetic_events(conn, [cfg], now="t1")

    row = get_synthetic_event(conn, "imsa-2026-rolex-24")
    assert row is not None
    assert row["summary"] == "Rolex 24 at Daytona"
    assert row["duration_seconds"] == 24 * 3600
    assert row["present_in_config"] == 1


def test_reconcile_marks_removed_events_as_no_longer_configured(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24", series="imsa", summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z", duration="24h",
    )
    reconcile_synthetic_events(conn, [cfg], now="t1")

    reconcile_synthetic_events(conn, [], now="t2")  # config no longer declares it

    row = get_synthetic_event(conn, "imsa-2026-rolex-24")
    assert row["present_in_config"] == 0
    assert row["cancelled_at"] == "t2"


def test_reconcile_reactivates_a_previously_removed_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24", series="imsa", summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z", duration="24h",
    )
    reconcile_synthetic_events(conn, [cfg], now="t1")
    reconcile_synthetic_events(conn, [], now="t2")  # removed

    reconcile_synthetic_events(conn, [cfg], now="t3")  # re-added

    row = get_synthetic_event(conn, "imsa-2026-rolex-24")
    assert row["present_in_config"] == 1
    assert row["cancelled_at"] is None


def test_reconcile_does_not_touch_unrelated_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg_a = SyntheticEventConfig(
        uid="event-a", series="imsa", summary="A", start="2026-01-01T00:00:00Z",
    )
    cfg_b = SyntheticEventConfig(
        uid="event-b", series="wec", summary="B", start="2026-02-01T00:00:00Z",
    )
    reconcile_synthetic_events(conn, [cfg_a, cfg_b], now="t1")

    reconcile_synthetic_events(conn, [cfg_a], now="t2")  # only B removed

    row_a = get_synthetic_event(conn, "event-a")
    row_b = get_synthetic_event(conn, "event-b")
    assert row_a["present_in_config"] == 1
    assert row_a["cancelled_at"] is None
    assert row_b["present_in_config"] == 0
    assert row_b["cancelled_at"] == "t2"


def test_reconcile_with_date_only_synthetic_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="event-date-only", series="wec", summary="Test Event", date="2026-06-01",
    )
    reconcile_synthetic_events(conn, [cfg], now="t1")

    row = get_synthetic_event(conn, "event-date-only")
    assert row["date"] == "2026-06-01"
    assert row["start"] is None


def test_reconcile_with_alarms_and_no_duration(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="event-with-alarms", series="wec", summary="Test Event",
        start="2026-06-01T10:00:00Z", alarms=["-1d", "-30m"],
    )
    reconcile_synthetic_events(conn, [cfg], now="t1")

    row = get_synthetic_event(conn, "event-with-alarms")
    assert row["duration_seconds"] is None
    assert row["alarms_json"] == '["-1d", "-30m"]'
