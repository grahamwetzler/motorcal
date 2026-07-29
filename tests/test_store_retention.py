from motorcal.store import (
    connect,
    delete_published_event,
    delete_source_event,
    get_published_event,
    get_source_event,
    get_synthetic_event,
    init_schema,
    list_all_source_events,
    list_published_events,
    purge_synthetic_event,
    transaction,
    upsert_published_event,
    upsert_source_event,
    upsert_synthetic_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert_source(conn, id_event, series="wec", season="2026"):
    upsert_source_event(
        conn, provider="thesportsdb", id_event=id_event, series=series, season=season,
        round=1, name=f"Event {id_event}", date="2026-04-19", time="13:00:00",
        venue="V", country="C", raw_json="{}", seen_at="t0",
    )


def _insert_published(conn, uid):
    upsert_published_event(
        conn, uid=uid, series="wec", session_type="race", summary="S", start="2026-04-19T13:00:00+00:00",
        all_day_date=None, time_confirmed=True, duration_seconds=3600, location="L",
        description="D", status="CONFIRMED", sequence=1, dtstamp="t0", last_modified="t0",
        fingerprint="fp", alarms_json="[]", source_provider="thesportsdb", source_id_event="1",
        synthetic_uid=None, cancelled_at=None, retain_until=None,
    )


def test_list_all_source_events_across_multiple_scopes(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_source(conn, "1", series="wec", season="2026")
        _insert_source(conn, "2", series="f1", season="2026")
        _insert_source(conn, "3", series="wec", season="2027")

    rows = list_all_source_events(conn)
    assert {row["id_event"] for row in rows} == {"1", "2", "3"}


def test_list_published_events_returns_all_rows(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "uid-1")
        _insert_published(conn, "uid-2")

    rows = list_published_events(conn)
    assert {row["uid"] for row in rows} == {"uid-1", "uid-2"}


def test_delete_source_event_removes_only_the_targeted_row(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_source(conn, "1")
        _insert_source(conn, "2")

    with transaction(conn):
        delete_source_event(conn, "thesportsdb", "1")

    assert get_source_event(conn, "thesportsdb", "1") is None
    assert get_source_event(conn, "thesportsdb", "2") is not None


def test_delete_published_event_removes_only_the_targeted_row(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "uid-1")
        _insert_published(conn, "uid-2")

    with transaction(conn):
        delete_published_event(conn, "uid-1")

    assert get_published_event(conn, "uid-1") is None
    assert get_published_event(conn, "uid-2") is not None


def test_purge_synthetic_event_deletes_the_row(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_synthetic_event(
            conn, uid="uid-1", series="imsa", summary="S", start="2026-01-01T00:00:00+00:00",
            date=None, duration_seconds=3600, location=None, status="CONFIRMED", note=None,
            alarms_json="[]",
        )

    with transaction(conn):
        purge_synthetic_event(conn, "uid-1")

    assert get_synthetic_event(conn, "uid-1") is None
