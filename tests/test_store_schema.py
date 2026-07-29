import sqlite3
from pathlib import Path

import pytest

from motorcal.store import SCHEMA_VERSION, check_integrity, connect, init_schema


def _fresh_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_connect_enables_wal_mode(tmp_path):
    conn = _fresh_conn(tmp_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_init_schema_sets_user_version(tmp_path):
    conn = _fresh_conn(tmp_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_init_schema_is_idempotent(tmp_path):
    conn = _fresh_conn(tmp_path)
    init_schema(conn)  # calling twice must not raise or duplicate tables
    init_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert tables == {
        "source_events",
        "source_snapshot_meta",
        "refresh_history",
        "synthetic_events",
        "published_events",
        "refresh_lease",
        "feed_revision",
    }


def test_source_events_table_columns(tmp_path):
    conn = _fresh_conn(tmp_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(source_events)").fetchall()}
    assert columns == {
        "provider",
        "id_event",
        "series",
        "season",
        "round",
        "name",
        "date",
        "time",
        "venue",
        "country",
        "raw_json",
        "first_seen_at",
        "last_seen_at",
        "disappeared_at",
    }


def test_source_events_primary_key_is_provider_and_id_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    conn.execute(
        "INSERT INTO source_events "
        "(provider, id_event, series, season, round, name, date, time, venue, country, "
        " raw_json, first_seen_at, last_seen_at) "
        "VALUES ('thesportsdb', '2421035', 'wec', '2026', 1, '6 Hours of Imola', "
        " '2026-04-19', '00:00:00', 'Imola', 'Italy', '{}', "
        " '2026-07-29T00:00:00+00:00', '2026-07-29T00:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_events "
            "(provider, id_event, series, season, round, name, date, time, venue, country, "
            " raw_json, first_seen_at, last_seen_at) "
            "VALUES ('thesportsdb', '2421035', 'wec', '2026', 1, 'Duplicate', "
            " '2026-04-19', '00:00:00', 'Imola', 'Italy', '{}', "
            " '2026-07-29T00:00:00+00:00', '2026-07-29T00:00:00+00:00')"
        )


def test_published_events_table_columns(tmp_path):
    conn = _fresh_conn(tmp_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(published_events)").fetchall()}
    assert columns == {
        "uid",
        "series",
        "session_type",
        "summary",
        "start",
        "all_day_date",
        "time_confirmed",
        "duration_seconds",
        "location",
        "description",
        "status",
        "sequence",
        "dtstamp",
        "last_modified",
        "fingerprint",
        "alarms_json",
        "source_provider",
        "source_id_event",
        "synthetic_uid",
        "cancelled_at",
        "retain_until",
    }


def test_refresh_lease_table_columns(tmp_path):
    conn = _fresh_conn(tmp_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(refresh_lease)").fetchall()}
    assert columns == {"id", "holder", "acquired_at", "expires_at"}


def test_check_integrity_ok_on_fresh_db(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert check_integrity(conn) is True


def test_check_integrity_detects_corruption(tmp_path):
    db_path = tmp_path / "corrupt.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    # Corrupt the file on disk directly (simulate real corruption).
    with open(db_path, "r+b") as f:
        f.seek(100)
        f.write(b"\xff" * 200)
    conn2 = sqlite3.connect(db_path)
    assert check_integrity(conn2) is False
