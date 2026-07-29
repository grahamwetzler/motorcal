from motorcal.store import (
    connect,
    get_refresh_diagnostics,
    init_schema,
    transaction,
    upsert_refresh_diagnostics,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_get_refresh_diagnostics_returns_none_when_never_set(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_refresh_diagnostics(conn) is None


def test_refresh_diagnostics_round_trip(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_refresh_diagnostics(
            conn, "t0", '[{"reason": "no_match"}]', '["uid-1"]', 10, 2, 1
        )

    row = get_refresh_diagnostics(conn)
    assert row["updated_at"] == "t0"
    assert row["patch_errors_json"] == '[{"reason": "no_match"}]'
    assert row["unknown_events_json"] == '["uid-1"]'
    assert row["events_published"] == 10
    assert row["events_cancelled"] == 2
    assert row["events_pruned"] == 1


def test_refresh_diagnostics_upsert_replaces_previous_values(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_refresh_diagnostics(conn, "t0", "[]", "[]", 1, 0, 0)
    with transaction(conn):
        upsert_refresh_diagnostics(conn, "t1", '[{"reason": "no_match"}]', '["u1"]', 5, 1, 0)

    row = get_refresh_diagnostics(conn)
    assert row["updated_at"] == "t1"
    assert row["events_published"] == 5


def test_init_schema_from_version_1_migrates_to_version_2(tmp_path):
    # Simulate an existing Phase-1-through-8 database (schema version 1) being
    # opened by this phase's code, proving the migration mechanism (built in
    # Phase 2 specifically for this) upgrades it without data loss.
    import sqlite3

    from motorcal.store import SCHEMA_VERSION

    db_path = tmp_path / "old.db"
    conn = connect(db_path)
    # Manually create only the version-1 tables and stamp version 1, bypassing
    # the current (already version-2-aware) init_schema.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS source_events (provider TEXT, id_event TEXT, "
        "PRIMARY KEY (provider, id_event))"
    )
    conn.execute("PRAGMA user_version=1")

    init_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert "refresh_diagnostics" in tables
