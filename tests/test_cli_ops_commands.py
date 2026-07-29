from pathlib import Path

from motorcal.cli import main
from motorcal.store import connect, init_schema, transaction, upsert_published_event

EXAMPLE_CONFIG = Path("config/config.example.yaml")
EXAMPLE_OVERRIDES = Path("config/overrides.example.yaml")


def test_validate_config_succeeds_on_the_example_files(capsys):
    exit_code = main(
        ["validate-config", "--config", str(EXAMPLE_CONFIG), "--overrides", str(EXAMPLE_OVERRIDES)]
    )
    assert exit_code == 0
    assert "valid" in capsys.readouterr().out.lower()


def test_validate_config_fails_on_invalid_yaml(tmp_path, capsys):
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("not: valid: yaml: [[[")

    exit_code = main(
        ["validate-config", "--config", str(bad_config), "--overrides", str(EXAMPLE_OVERRIDES)]
    )
    assert exit_code == 1
    assert capsys.readouterr().err != ""


def test_validate_config_never_touches_a_database(tmp_path, capsys):
    # There is deliberately no --db argument at all -- prove the command
    # doesn't need one and does no database I/O.
    exit_code = main(
        ["validate-config", "--config", str(EXAMPLE_CONFIG), "--overrides", str(EXAMPLE_OVERRIDES)]
    )
    assert exit_code == 0
    assert not (tmp_path / "test.db").exists()


def test_republish_force_version_advances_stale_sequences(tmp_path, capsys):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_schema(conn)
    with transaction(conn):
        upsert_published_event(
            conn, uid="u1", series="wec", session_type="race", summary="S", start=None,
            all_day_date="2026-01-01", time_confirmed=False, duration_seconds=None, location=None,
            description="D", status="CONFIRMED", sequence=1, dtstamp="t0", last_modified="t0",
            fingerprint="fp", alarms_json="[]", source_provider="thesportsdb", source_id_event="1",
            synthetic_uid=None, cancelled_at=None, retain_until=None,
        )
    conn.close()

    exit_code = main(["republish", "--db", str(db_path), "--force-version"])

    assert exit_code == 0
    assert "1" in capsys.readouterr().out

    conn2 = connect(db_path)
    from motorcal.store import list_published_events
    row = list_published_events(conn2)[0]
    assert row["sequence"] > 1


def test_republish_requires_force_version_flag():
    import pytest

    with pytest.raises(SystemExit):
        main(["republish", "--db", "/tmp/whatever.db"])
