from pathlib import Path

from motorcal.store import (
    backup_database,
    check_integrity,
    connect,
    get_source_event,
    init_schema,
    transaction,
    upsert_source_event,
)


def test_backup_database_creates_a_working_copy(tmp_path):
    source_path = tmp_path / "source.db"
    dest_path = tmp_path / "backup.db"

    conn = connect(source_path)
    init_schema(conn)
    with transaction(conn):
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
    conn.close()

    backup_database(source_path, dest_path)

    assert dest_path.exists()
    backup_conn = connect(dest_path)
    row = get_source_event(backup_conn, "thesportsdb", "2421035")
    assert row is not None
    assert row["name"] == "6 Hours of Imola"
    assert check_integrity(backup_conn) is True


def test_backup_database_overwrites_existing_destination(tmp_path):
    source_path = tmp_path / "source.db"
    dest_path = tmp_path / "backup.db"
    dest_path.write_bytes(b"not a real database")

    conn = connect(source_path)
    init_schema(conn)
    conn.close()

    backup_database(source_path, dest_path)

    backup_conn = connect(dest_path)
    assert check_integrity(backup_conn) is True
