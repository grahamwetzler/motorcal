from datetime import datetime, timezone

from fastapi.testclient import TestClient
from tests.conftest import make_config, make_series

from motorcal.models import EventStatus, PublishedEvent, SessionType
from motorcal.web import Publication, create_app

ROOT_CONFIG = make_config(series={"wec": make_series()})
ICS = b"BEGIN:VCALENDAR\r\nSUMMARY:6 Hours of Imola\r\nEND:VCALENDAR\r\n"

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _client(feeds=None, published=None):
    app = create_app(ROOT_CONFIG)
    app.state.publication = Publication(
        config=ROOT_CONFIG, feeds=feeds or {}, published=published or {}
    )
    return TestClient(app)


def _event(uid, session_type):
    return PublishedEvent(
        uid=uid, series="wec", session_type=session_type, summary=uid,
        start=datetime(2026, 4, 19, 13, tzinfo=timezone.utc), all_day_date=None,
        time_confirmed=True, duration_seconds=3600, location=None, description="D",
        status=EventStatus.CONFIRMED, sequence=1, dtstamp=NOW, last_modified=NOW,
        fingerprint="fp", event_key=uid,
    )


def test_unconfigured_series_returns_404():
    assert _client().get("/nonexistent-series.ics").status_code == 404


def test_series_with_an_empty_feed_returns_503():
    assert _client({"wec": b""}).get("/wec.ics").status_code == 503


def test_series_missing_from_feeds_returns_503():
    assert _client({}).get("/wec.ics").status_code == 503


def test_valid_request_returns_ics_with_expected_headers():
    response = _client({"wec": ICS}).get("/wec.ics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.headers["cache-control"] == "public, no-cache"
    assert "etag" in response.headers
    # Deliberately absent: pruning changes the feed without touching any
    # remaining event's timestamp, so a derived Last-Modified would go stale.
    assert "last-modified" not in response.headers
    assert response.content == ICS


def test_conditional_request_with_matching_etag_returns_304():
    client = _client({"wec": ICS})
    first = client.get("/wec.ics")

    second = client.get("/wec.ics", headers={"If-None-Match": first.headers["etag"]})

    assert second.status_code == 304
    assert len(second.content) == 0


def test_conditional_request_with_stale_etag_returns_200():
    response = _client({"wec": ICS}).get("/wec.ics", headers={"If-None-Match": '"stale-value"'})

    assert response.status_code == 200
    assert response.content == ICS


def test_etag_changes_when_the_feed_content_changes():
    first = _client({"wec": ICS}).get("/wec.ics")
    second = _client({"wec": ICS + b"X"}).get("/wec.ics")

    assert first.headers["etag"] != second.headers["etag"]


def test_default_request_serves_the_unfiltered_feed_unchanged():
    events = {"wec": [_event("practice", SessionType.PRACTICE), _event("race", SessionType.RACE)]}
    response = _client({"wec": ICS}, events).get("/wec.ics")

    assert response.content == ICS


def test_practices_false_excludes_practice_sessions_but_keeps_race():
    events = {"wec": [_event("practice", SessionType.PRACTICE), _event("race", SessionType.RACE)]}
    response = _client({"wec": ICS}, events).get("/wec.ics", params={"practices": "false"})

    assert b"UID:practice" not in response.content
    assert b"UID:race" in response.content


def test_qualifying_false_excludes_qualifying_hyperpole_and_sprint_qualifying():
    events = {
        "wec": [
            _event("q", SessionType.QUALIFYING),
            _event("hp", SessionType.HYPERPOLE),
            _event("sq", SessionType.SPRINT_QUALIFYING),
            _event("race", SessionType.RACE),
        ]
    }
    response = _client({"wec": ICS}, events).get("/wec.ics", params={"qualifying": "false"})

    assert b"UID:q\r\n" not in response.content
    assert b"UID:hp" not in response.content
    assert b"UID:sq" not in response.content
    assert b"UID:race" in response.content


def test_filtered_request_returns_503_when_series_has_no_feed_at_all():
    response = _client({}, {}).get("/wec.ics", params={"practices": "false"})

    assert response.status_code == 503
