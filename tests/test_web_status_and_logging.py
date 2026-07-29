import logging

from fastapi.testclient import TestClient

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.store import connect, init_schema, transaction, upsert_published_event, upsert_snapshot_meta
from motorcal.web import create_app


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(durations=DurationDefaults(), alerts={}, include_sessions=["race"]),
        unknown_time=UnknownTimeConfig(),
        series={"wec": SeriesConfig(league_id=4413, name="WEC", max_round=20)},
    )


def test_status_bad_token_returns_404(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/bad-token/status")

    assert response.status_code == 404


def test_status_reports_readiness_and_health_per_series(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        upsert_published_event(
            conn, uid="u1", series="wec", session_type="race", summary="S",
            start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
            duration_seconds=3600, location="L", description="D", status="CONFIRMED",
            sequence=1, dtstamp="t0", last_modified="t0", fingerprint="fp", alarms_json="[]",
            source_provider="thesportsdb", source_id_event="1", synthetic_uid=None,
            cancelled_at=None, retain_until=None,
        )
        upsert_snapshot_meta(conn, "thesportsdb", "wec", str(now.year), now.isoformat(), 1)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/good-token/status")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["healthy"] is True
    assert body["series"]["wec"]["ready"] is True
    assert body["series"]["wec"]["stale"] is False


def test_access_log_redacts_the_token(tmp_path, caplog):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["super-secret-token"])
    client = TestClient(app)

    with caplog.at_level(logging.INFO, logger="motorcal.access"):
        client.get("/c/super-secret-token/status")

    access_records = [r for r in caplog.records if r.name == "motorcal.access"]
    assert len(access_records) == 1
    message = access_records[0].getMessage()
    assert "super-secret-token" not in message
    assert "REDACTED" in message


def test_access_log_redacts_the_token_even_on_a_404(tmp_path, caplog):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    client = TestClient(app)

    with caplog.at_level(logging.INFO, logger="motorcal.access"):
        client.get("/c/leaked-guess-token/status")

    access_records = [r for r in caplog.records if r.name == "motorcal.access"]
    assert len(access_records) == 1
    assert "leaked-guess-token" not in access_records[0].getMessage()
