from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import (
    UID_DOMAIN,
    make_config,
    make_series,
    make_state,
    manual_event,
    source_event,
    write_config_dir,
)

from motorcal.admin import create_admin_app
from motorcal.config import load_config
from motorcal.merge import rebuild_publication
from motorcal.state import SnapshotState, scope_key

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SOURCE_UID = f"thesportsdb-1@{UID_DOMAIN}"
MANUAL_UID = f"local-mine@{UID_DOMAIN}"


def _setup(tmp_path, events=None, state=None, diagnostics=None):
    config = make_config(
        series={"wec": make_series(events=events if events is not None else [])}
    )
    config_dir = write_config_dir(tmp_path, config)
    state = state or make_state()
    published, _ = rebuild_publication(config, state, now=NOW)

    main_app = FastAPI()
    main_app.state.config = config
    main_app.state.published = published
    main_app.state.data = state
    main_app.state.diagnostics = diagnostics or {}
    return config_dir, TestClient(create_admin_app(config_dir, main_app))


def _events(config_dir):
    return load_config(config_dir).series["wec"].events


def test_list_route_shows_events(tmp_path):
    _, client = _setup(tmp_path, [source_event("1", time="13:00:00")])

    response = client.get("/")

    assert response.status_code == 200
    assert "6 Hours of Imola" in response.text


def test_editing_a_provider_event_writes_the_series_file(tmp_path):
    config_dir, client = _setup(tmp_path, [source_event("1", time="13:00:00")])

    response = client.post(
        "/events/edit", follow_redirects=False,
        data={
            "published_uid": SOURCE_UID, "summary": "6 Hours of Imola (delayed)",
            "start": "2026-04-19T15:00:00+00:00", "duration": "6h",
            "location": "Imola, Italy", "status": "CONFIRMED", "note": "rain delay",
        },
    )

    assert response.status_code == 303
    event = _events(config_dir)[0]
    assert event.summary == "6 Hours of Imola (delayed)"
    assert event.start == "2026-04-19T15:00:00+00:00"
    assert event.note == "rain delay"


def test_editing_preserves_the_provider_identity_and_source_baseline(tmp_path):
    """id_event and source: are the provider's -- a form must never rewrite them."""
    config_dir, client = _setup(tmp_path, [source_event("1", time="13:00:00")])
    original_source = _events(config_dir)[0].source

    client.post(
        "/events/edit", follow_redirects=False,
        data={"published_uid": SOURCE_UID, "summary": "Renamed", "start": "2026-04-19T13:00:00+00:00"},
    )

    event = _events(config_dir)[0]
    assert event.id_event == "1"
    assert event.source == original_source


def test_editing_twice_replaces_rather_than_duplicates(tmp_path):
    config_dir, client = _setup(tmp_path, [source_event("1", time="13:00:00")])

    for summary in ("First", "Second"):
        client.post(
            "/events/edit", follow_redirects=False,
            data={
                "published_uid": SOURCE_UID, "summary": summary,
                "start": "2026-04-19T13:00:00+00:00",
            },
        )

    events = _events(config_dir)
    assert len(events) == 1
    assert events[0].summary == "Second"


def test_creating_a_manual_event(tmp_path):
    config_dir, client = _setup(tmp_path)

    response = client.post(
        "/events/edit", follow_redirects=False,
        data={
            "published_uid": "", "uid": "mine", "series": "wec", "summary": "Test Day",
            "start": "2026-05-01T10:00:00+00:00", "duration": "2h", "status": "CONFIRMED",
        },
    )

    assert response.status_code == 303
    event = _events(config_dir)[0]
    assert event.uid == "mine"
    assert event.id_event is None
    assert event.source is None


def test_creating_a_duplicate_uid_is_rejected(tmp_path):
    config_dir, client = _setup(tmp_path, [manual_event("mine")])
    before = (config_dir / "wec.yaml").read_text()

    response = client.post(
        "/events/edit", follow_redirects=False,
        data={
            "published_uid": "", "uid": "mine", "series": "wec", "summary": "Clash",
            "date": "2026-06-01",
        },
    )

    assert response.status_code == 400
    assert "already exists" in response.text
    assert (config_dir / "wec.yaml").read_text() == before


def test_an_unknown_series_is_rejected(tmp_path):
    config_dir, client = _setup(tmp_path)

    response = client.post(
        "/events/edit", follow_redirects=False,
        data={
            "published_uid": "", "uid": "x", "series": "not-a-series",
            "summary": "Nope", "date": "2026-06-01",
        },
    )

    assert response.status_code == 400
    assert "Unknown series" in response.text


def test_an_invalid_submission_leaves_the_file_unchanged(tmp_path):
    config_dir, client = _setup(tmp_path, [source_event("1", time="13:00:00")])
    before = (config_dir / "wec.yaml").read_text()

    # Neither start nor date violates the "exactly one" rule.
    response = client.post(
        "/events/edit", follow_redirects=False,
        data={"published_uid": SOURCE_UID, "summary": "No timing", "start": "", "date": ""},
    )

    assert response.status_code == 400
    assert (config_dir / "wec.yaml").read_text() == before


def test_a_save_does_not_drop_events_added_since_the_page_loaded(tmp_path):
    """The form re-reads from disk, so a concurrent refresh's writes survive."""
    config_dir, client = _setup(tmp_path, [source_event("1", time="13:00:00")])

    # Simulate a refresh cycle appending an event while the edit form was open.
    from motorcal.config import save_series
    on_disk = load_config(config_dir).series["wec"]
    on_disk.events.append(source_event("2", time="14:00:00"))
    save_series(config_dir, "wec", on_disk)

    client.post(
        "/events/edit", follow_redirects=False,
        data={"published_uid": SOURCE_UID, "summary": "Edited", "start": "2026-04-19T13:00:00+00:00"},
    )

    assert {e.key for e in _events(config_dir)} == {"1", "2"}


def test_editing_an_unknown_uid_returns_404(tmp_path):
    _, client = _setup(tmp_path)

    assert client.get("/events/edit?uid=nope").status_code == 404


def test_status_reports_ready_and_healthy_for_a_fresh_series(tmp_path):
    season = str(datetime.now(timezone.utc).year)
    state = make_state(snapshots={
        scope_key("wec", season): SnapshotState(
            last_complete_at=datetime.now(timezone.utc).isoformat(), count=1
        )
    })
    _, client = _setup(tmp_path, [source_event("1", time="13:00:00")], state=state)

    body = client.get("/status").json()

    assert body["ready"] is True
    assert body["healthy"] is True
    assert body["series"]["wec"]["events"] == 1
    assert body["series"]["wec"]["stale"] is False


def test_status_reports_stale_without_failing(tmp_path):
    season = str(datetime.now(timezone.utc).year)
    state = make_state(snapshots={
        scope_key("wec", season): SnapshotState(
            last_complete_at="2020-01-01T00:00:00+00:00", count=1
        )
    })
    _, client = _setup(tmp_path, [source_event("1", time="13:00:00")], state=state)

    response = client.get("/status")

    # Always 200: this is the container healthcheck, and an upstream outage must
    # not restart-loop a process serving its last-known-good feeds.
    assert response.status_code == 200
    assert response.json()["healthy"] is False
    assert response.json()["series"]["wec"]["stale"] is True


def test_status_reports_never_refreshed_as_not_ready(tmp_path):
    _, client = _setup(tmp_path)

    body = client.get("/status").json()

    assert body["ready"] is False
    assert body["series"]["wec"]["last_complete_at"] is None


def test_status_surfaces_unknown_events(tmp_path):
    _, client = _setup(tmp_path, diagnostics={"unknown_events": ["u1"]})

    assert client.get("/status").json()["unknown_events"] == ["u1"]
