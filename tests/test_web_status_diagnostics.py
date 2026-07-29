import json

from fastapi.testclient import TestClient

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.store import connect, init_schema, transaction, upsert_refresh_diagnostics
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


def test_status_includes_empty_diagnostics_before_any_refresh(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/c/t/status")

    body = response.json()
    assert body["patch_errors"] == []
    assert body["unknown_events"] == []


def test_status_surfaces_persisted_diagnostics(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        upsert_refresh_diagnostics(
            conn, "t0", json.dumps([{"reason": "no_match", "id_event": "999"}]),
            json.dumps(["thesportsdb-1@x.example.com"]), 5, 1, 0,
        )
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/c/t/status")

    body = response.json()
    assert body["patch_errors"] == [{"reason": "no_match", "id_event": "999"}]
    assert body["unknown_events"] == ["thesportsdb-1@x.example.com"]
