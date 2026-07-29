"""Confirms that app.state.root_config is the live source of truth HTTP routes read.

_cmd_serve wires a hot config reload by reassigning app.state.root_config/overrides
(see reload_job in cli.py); this proves the routes pick that up on the very next
request without needing to recreate or restart the app.
"""
from fastapi.testclient import TestClient

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.store import connect, init_schema
from motorcal.web import create_app


def _root_config(series):
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(durations=DurationDefaults(), alerts={}, include_sessions=["race"]),
        unknown_time=UnknownTimeConfig(),
        series=series,
    )


def test_mutating_app_state_root_config_reaches_routes_immediately(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    initial = _root_config({"wec": SeriesConfig(league_id=4413, name="WEC", max_round=20)})
    app = create_app(tmp_path / "test.db", initial, tokens=["good-token"])
    client = TestClient(app)

    assert client.get("/c/good-token/imsa.ics").status_code == 404
    assert "imsa" not in client.get("/c/good-token/status").json()["series"]

    # This is exactly what reload_job does on a successful hot reload: reassign
    # app.state.root_config wholesale to the newly validated bundle.
    app.state.root_config = _root_config({
        "wec": SeriesConfig(league_id=4413, name="WEC", max_round=20),
        "imsa": SeriesConfig(league_id=4488, name="IMSA", max_round=20),
    })

    response = client.get("/c/good-token/imsa.ics")
    assert response.status_code == 503  # now a recognized series, just no published events yet
    assert "imsa" in client.get("/c/good-token/status").json()["series"]
