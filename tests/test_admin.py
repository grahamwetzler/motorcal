from fastapi import FastAPI
from fastapi.testclient import TestClient

from motorcal.admin import create_admin_app
from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
    load_overrides,
)
from motorcal.store import connect, init_schema, upsert_published_event


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(durations=DurationDefaults(), alerts={}, include_sessions=["race"]),
        unknown_time=UnknownTimeConfig(),
        series={"wec": SeriesConfig(league_id=4413, name="WEC", max_round=20)},
    )


def _insert_source_backed(conn, uid="thesportsdb-1@x.example.com"):
    upsert_published_event(
        conn, uid=uid, series="wec", session_type="race", summary="6 Hours of Imola",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=6 * 3600, location="Imola, Italy", description="D", status="CONFIRMED",
        sequence=1, dtstamp="2026-01-01T00:00:00+00:00", last_modified="2026-01-01T00:00:00+00:00",
        fingerprint="fp", alarms_json="[]", source_provider="thesportsdb", source_id_event="1",
        synthetic_uid=None, cancelled_at=None, retain_until=None,
    )


def _insert_synthetic(conn, uid="local-my-event@x.example.com", synthetic_uid="my-event"):
    upsert_published_event(
        conn, uid=uid, series="wec", session_type="unknown", summary="Test Day",
        start="2026-05-01T10:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=2 * 3600, location=None, description="D", status="CONFIRMED",
        sequence=1, dtstamp="2026-01-01T00:00:00+00:00", last_modified="2026-01-01T00:00:00+00:00",
        fingerprint="fp", alarms_json="[]", source_provider=None, source_id_event=None,
        synthetic_uid=synthetic_uid, cancelled_at=None, retain_until=None,
    )


def _setup(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    overrides_path = tmp_path / "overrides.yaml"
    overrides_path.write_text("patches: []\nevents: []\n")
    main_app = FastAPI()
    main_app.state.root_config = _root_config()
    admin_app = create_admin_app(tmp_path / "test.db", overrides_path, main_app)
    return conn, overrides_path, TestClient(admin_app)


def test_list_route_shows_events(tmp_path):
    conn, _, client = _setup(tmp_path)
    _insert_source_backed(conn)
    conn.close()

    response = client.get("/")

    assert response.status_code == 200
    assert "6 Hours of Imola" in response.text


def test_edit_source_backed_event_creates_then_replaces_patch(tmp_path):
    conn, overrides_path, client = _setup(tmp_path)
    _insert_source_backed(conn)
    conn.close()

    uid = "thesportsdb-1@x.example.com"
    response = client.post(
        "/events/edit",
        follow_redirects=False,
        data={
            "published_uid": uid, "start": "2026-04-19T14:00:00Z", "duration": "6h",
            "summary": "6 Hours of Imola (delayed)", "location": "Imola, Italy",
            "status": "CONFIRMED", "note": "rain delay",
        },
    )
    assert response.status_code == 303

    overrides = load_overrides(overrides_path)
    assert len(overrides.patches) == 1
    assert overrides.patches[0].id_event == "1"
    assert overrides.patches[0].start == "2026-04-19T14:00:00Z"
    assert overrides.patches[0].note == "rain delay"

    # Editing again replaces, rather than duplicates, the same patch.
    response = client.post(
        "/events/edit",
        follow_redirects=False,
        data={
            "published_uid": uid, "start": "2026-04-19T15:00:00Z", "duration": "6h",
            "summary": "6 Hours of Imola (delayed)", "location": "Imola, Italy",
            "status": "CONFIRMED", "note": "rain delay, take two",
        },
    )
    assert response.status_code == 303

    overrides = load_overrides(overrides_path)
    assert len(overrides.patches) == 1
    assert overrides.patches[0].start == "2026-04-19T15:00:00Z"
    assert overrides.patches[0].note == "rain delay, take two"


def test_create_synthetic_event_then_edit_it(tmp_path):
    conn, overrides_path, client = _setup(tmp_path)

    response = client.post(
        "/events/edit",
        follow_redirects=False,
        data={
            "published_uid": "", "uid": "my-event", "series": "wec",
            "start": "2026-05-01T10:00:00Z", "duration": "2h",
            "summary": "Test Day", "status": "CONFIRMED",
        },
    )
    assert response.status_code == 303

    overrides = load_overrides(overrides_path)
    assert len(overrides.events) == 1
    assert overrides.events[0].uid == "my-event"

    # Simulate a refresh cycle having since published this synthetic event.
    _insert_synthetic(conn)
    conn.close()

    response = client.post(
        "/events/edit",
        follow_redirects=False,
        data={
            "published_uid": "local-my-event@x.example.com", "start": "2026-05-01T11:00:00Z",
            "duration": "3h", "summary": "Test Day (updated)", "status": "CONFIRMED",
        },
    )
    assert response.status_code == 303

    overrides = load_overrides(overrides_path)
    assert len(overrides.events) == 1
    assert overrides.events[0].start == "2026-05-01T11:00:00Z"
    assert overrides.events[0].summary == "Test Day (updated)"


def test_invalid_submission_leaves_overrides_file_unchanged(tmp_path):
    conn, overrides_path, client = _setup(tmp_path)
    conn.close()
    before = overrides_path.read_text()

    response = client.post(
        "/events/edit",
        follow_redirects=False,
        data={
            "published_uid": "", "uid": "bad-event", "series": "not-a-real-series",
            "start": "2026-05-01T10:00:00Z", "summary": "Nope",
        },
    )

    assert response.status_code == 400
    assert overrides_path.read_text() == before
