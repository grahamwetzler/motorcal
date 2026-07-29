import pytest

from motorcal.store import (
    IntentionalRollback,
    connect,
    get_published_event,
    get_source_event,
    init_schema,
    transaction,
    upsert_published_event,
    upsert_source_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert_sample_source_event(conn):
    upsert_source_event(
        conn,
        provider="thesportsdb",
        id_event="2421035",
        series="wec",
        season="2026",
        round=1,
        name="6 Hours of Imola",
        date="2026-04-19",
        time="00:00:00",
        venue="Imola",
        country="Italy",
        raw_json="{}",
        seen_at="2026-07-29T00:00:00+00:00",
    )


def _insert_sample_published_event(conn):
    upsert_published_event(
        conn,
        uid="thesportsdb-2421035@racing.example.com",
        series="wec",
        session_type="race",
        summary="6 Hours of Imola",
        start=None,
        all_day_date="2026-04-19",
        time_confirmed=False,
        duration_seconds=None,
        location="Imola, Italy",
        description="Round 1 of WEC",
        status="CONFIRMED",
        sequence=1,
        dtstamp="2026-07-29T00:00:00+00:00",
        last_modified="2026-07-29T00:00:00+00:00",
        fingerprint="abc123",
        alarms_json="[]",
        source_provider="thesportsdb",
        source_id_event="2421035",
        synthetic_uid=None,
        cancelled_at=None,
        retain_until=None,
    )


def test_upsert_and_get_source_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_sample_source_event(conn)
    row = get_source_event(conn, "thesportsdb", "2421035")
    assert row["name"] == "6 Hours of Imola"
    assert row["first_seen_at"] == "2026-07-29T00:00:00+00:00"
    assert row["disappeared_at"] is None


def test_upsert_source_event_updates_without_resetting_first_seen(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_sample_source_event(conn)
    with transaction(conn):
        upsert_source_event(
            conn,
            provider="thesportsdb",
            id_event="2421035",
            series="wec",
            season="2026",
            round=1,
            name="6 Hours of Imola (Updated)",
            date="2026-04-19",
            time="13:00:00",
            venue="Imola",
            country="Italy",
            raw_json="{}",
            seen_at="2026-08-01T00:00:00+00:00",
        )
    row = get_source_event(conn, "thesportsdb", "2421035")
    assert row["name"] == "6 Hours of Imola (Updated)"
    assert row["time"] == "13:00:00"
    assert row["first_seen_at"] == "2026-07-29T00:00:00+00:00"  # unchanged
    assert row["last_seen_at"] == "2026-08-01T00:00:00+00:00"  # updated


def test_upsert_and_get_published_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_sample_published_event(conn)
    row = get_published_event(conn, "thesportsdb-2421035@racing.example.com")
    assert row["summary"] == "6 Hours of Imola"
    assert row["status"] == "CONFIRMED"
    assert row["sequence"] == 1


def test_get_source_event_returns_none_when_missing(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_source_event(conn, "thesportsdb", "does-not-exist") is None


def test_get_published_event_returns_none_when_missing(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_published_event(conn, "does-not-exist@racing.example.com") is None


def test_transaction_rolls_back_both_tables_on_exception(tmp_path):
    conn = _fresh_conn(tmp_path)
    with pytest.raises(IntentionalRollback):
        with transaction(conn):
            _insert_sample_source_event(conn)
            _insert_sample_published_event(conn)
            raise IntentionalRollback("simulated failure mid-transaction")
    # Neither write should have survived — this is the "one atomic transaction"
    # guarantee the spec requires for snapshot replace + publication rebuild.
    assert get_source_event(conn, "thesportsdb", "2421035") is None
    assert get_published_event(conn, "thesportsdb-2421035@racing.example.com") is None


def test_transaction_commits_both_tables_together_on_success(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_sample_source_event(conn)
        _insert_sample_published_event(conn)
    assert get_source_event(conn, "thesportsdb", "2421035") is not None
    assert get_published_event(conn, "thesportsdb-2421035@racing.example.com") is not None


def test_transaction_is_composable_with_lease_functions(tmp_path):
    from motorcal.store import acquire_lease, current_lease_holder

    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        assert acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0) is True
        _insert_sample_source_event(conn)
    assert current_lease_holder(conn, now=1000.0) == "worker-a"
    assert get_source_event(conn, "thesportsdb", "2421035") is not None
