from fastapi.testclient import TestClient

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.store import connect, init_schema, transaction, upsert_published_event
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


def _insert_published(conn, uid="u1", series="wec"):
    upsert_published_event(
        conn, uid=uid, series=series, session_type="race", summary="6 Hours of Imola",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=6 * 3600, location="Imola, Italy", description="D", status="CONFIRMED",
        sequence=1, dtstamp="2026-01-01T00:00:00+00:00", last_modified="2026-01-01T00:00:00+00:00",
        fingerprint="fp", alarms_json="[]", source_provider="thesportsdb", source_id_event="1",
        synthetic_uid=None, cancelled_at=None, retain_until=None,
    )


def test_bad_token_returns_404(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/bad-token/wec.ics")

    assert response.status_code == 404


def test_unconfigured_series_returns_404(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/good-token/nonexistent-series.ics")

    assert response.status_code == 404


def test_series_with_no_published_events_returns_503(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/good-token/wec.ics")

    assert response.status_code == 503


def test_valid_request_returns_ics_with_expected_headers(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        _insert_published(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/good-token/wec.ics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.headers["cache-control"] == "private, no-cache"
    assert "etag" in response.headers
    assert "last-modified" in response.headers
    assert b"BEGIN:VCALENDAR" in response.content
    assert b"6 Hours of Imola" in response.content


def test_conditional_request_with_matching_etag_returns_304(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        _insert_published(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    client = TestClient(app)
    first = client.get("/c/good-token/wec.ics")

    second = client.get(
        "/c/good-token/wec.ics", headers={"If-None-Match": first.headers["etag"]}
    )

    assert second.status_code == 304
    assert len(second.content) == 0


def test_conditional_request_with_stale_etag_returns_200(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        _insert_published(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get(
        "/c/good-token/wec.ics", headers={"If-None-Match": '"stale-value"'}
    )

    assert response.status_code == 200
    assert len(response.content) > 0
