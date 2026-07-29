from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.store import (
    connect,
    init_schema,
    transaction,
    upsert_published_event,
    upsert_snapshot_meta,
)
from motorcal.web import create_app


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(durations=DurationDefaults(), alerts={}, include_sessions=["race"]),
        unknown_time=UnknownTimeConfig(),
        series={
            "wec": SeriesConfig(league_id=4413, name="WEC", max_round=20),
            "f1": SeriesConfig(league_id=4370, name="Formula 1", max_round=30),
        },
    )


def _insert_published(conn, uid, series):
    upsert_published_event(
        conn, uid=uid, series=series, session_type="race", summary="S",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=3600, location="L", description="D", status="CONFIRMED",
        sequence=1, dtstamp="t0", last_modified="t0", fingerprint="fp", alarms_json="[]",
        source_provider="thesportsdb", source_id_event="1", synthetic_uid=None,
        cancelled_at=None, retain_until=None,
    )


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_readyz_returns_503_when_a_series_has_no_published_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", "wec")  # f1 has nothing
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["series"]["wec"] is True
    assert body["series"]["f1"] is False


def test_readyz_returns_200_when_every_series_has_published_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", "wec")
        _insert_published(conn, "u2", "f1")
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/readyz")

    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_healthz_returns_503_when_a_series_has_never_been_refreshed(tmp_path):
    conn = _fresh_conn(tmp_path)
    now = datetime.now(timezone.utc)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", str(now.year), now.isoformat(), 5)
        # f1 has no snapshot_meta row at all
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["healthy"] is False
    assert body["series"]["f1"]["stale"] is True
    assert body["series"]["f1"]["last_complete_at"] is None


def test_healthz_returns_503_when_a_series_is_stale(tmp_path):
    conn = _fresh_conn(tmp_path)
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(hours=48)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", str(now.year), stale_time.isoformat(), 5)
        upsert_snapshot_meta(conn, "thesportsdb", "f1", str(now.year), now.isoformat(), 5)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["series"]["wec"]["stale"] is True
    assert body["series"]["f1"]["stale"] is False


def test_healthz_returns_200_when_every_series_is_fresh(tmp_path):
    conn = _fresh_conn(tmp_path)
    now = datetime.now(timezone.utc)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", str(now.year), now.isoformat(), 5)
        upsert_snapshot_meta(conn, "thesportsdb", "f1", str(now.year), now.isoformat(), 3)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json()["healthy"] is True
