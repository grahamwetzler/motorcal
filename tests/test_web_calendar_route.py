from datetime import datetime, timezone

from fastapi.testclient import TestClient
from tests.conftest import make_config, make_series

from motorcal.models import EventStatus, PublishedEvent, SessionType
from motorcal.web import Publication, create_app

CONFIG = make_config(series={"wec": make_series(), "f1": make_series(name="F1")})
ICS = b"BEGIN:VCALENDAR\r\nSUMMARY:6 Hours of Imola\r\nEND:VCALENDAR\r\n"

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _client(feeds=None, published=None):
    app = create_app(CONFIG)
    app.state.publication = Publication(
        config=CONFIG, feeds=feeds or {}, published=published or {}
    )
    return TestClient(app)


def _event(uid, session_type):
    return PublishedEvent(
        uid=uid, series="wec", session_type=session_type, summary=uid,
        start=datetime(2026, 4, 19, 13, tzinfo=timezone.utc), all_day_date=None,
        time_confirmed=True, duration_seconds=3600, location=None, description="D",
        status=EventStatus.CONFIRMED, sequence=1, dtstamp=NOW, last_modified=NOW,
        fingerprint="fp", session_key=uid,
    )


def test_unmatched_path_returns_404():
    assert _client().get("/nonexistent-series.ics").status_code == 404


def test_combined_serves_the_precomputed_combined_feed():
    response = _client({"events": ICS}).get("/events.ics")

    assert response.status_code == 200
    assert response.content == ICS
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.headers["cache-control"] == "public, no-cache"
    assert "etag" in response.headers
    # Deliberately absent: pruning changes the feed without touching any
    # remaining event's timestamp, so a derived Last-Modified would go stale.
    assert "last-modified" not in response.headers


def test_combined_with_no_combined_feed_returns_503():
    assert _client({}, {}).get("/events.ics").status_code == 503


def test_combined_filtered_request_returns_503_when_there_is_no_combined_feed():
    response = _client({}, {}).get("/events.ics", params={"practices": "false"})

    assert response.status_code == 503


def test_conditional_request_with_matching_etag_returns_304():
    client = _client({"events": ICS})
    first = client.get("/events.ics")

    second = client.get("/events.ics", headers={"If-None-Match": first.headers["etag"]})

    assert second.status_code == 304
    assert len(second.content) == 0


def test_conditional_request_with_stale_etag_returns_200():
    response = _client({"events": ICS}).get("/events.ics", headers={"If-None-Match": '"stale-value"'})

    assert response.status_code == 200
    assert response.content == ICS


def test_etag_changes_when_the_feed_content_changes():
    first = _client({"events": ICS}).get("/events.ics")
    second = _client({"events": ICS + b"X"}).get("/events.ics")

    assert first.headers["etag"] != second.headers["etag"]


def test_default_request_serves_the_unfiltered_feed_unchanged():
    published = {"wec": [_event("practice", SessionType.PRACTICE), _event("race", SessionType.RACE)]}
    response = _client({"events": ICS}, published).get("/events.ics")

    assert response.content == ICS


def test_practices_false_excludes_practice_sessions_but_keeps_race():
    published = {"wec": [_event("practice", SessionType.PRACTICE), _event("race", SessionType.RACE)]}
    response = _client({"events": ICS}, published).get("/events.ics", params={"practices": "false"})

    assert b"UID:practice" not in response.content
    assert b"UID:race" in response.content


def test_qualifying_false_excludes_qualifying_hyperpole_and_sprint_qualifying():
    published = {
        "wec": [
            _event("q", SessionType.QUALIFYING),
            _event("hp", SessionType.HYPERPOLE),
            _event("sq", SessionType.SPRINT_QUALIFYING),
            _event("race", SessionType.RACE),
        ]
    }
    response = _client({"events": ICS}, published).get("/events.ics", params={"qualifying": "false"})

    assert b"UID:q\r\n" not in response.content
    assert b"UID:hp" not in response.content
    assert b"UID:sq" not in response.content
    assert b"UID:race" in response.content


def test_combined_filter_applies_across_every_series():
    published = {
        "wec": [_event("wec-practice", SessionType.PRACTICE), _event("wec-race", SessionType.RACE)],
        "f1": [_event("f1-practice", SessionType.PRACTICE), _event("f1-race", SessionType.RACE)],
    }
    response = _client({"events": ICS}, published).get(
        "/events.ics", params={"practices": "false"}
    )

    assert b"UID:wec-practice" not in response.content
    assert b"UID:f1-practice" not in response.content
    assert b"UID:wec-race" in response.content
    assert b"UID:f1-race" in response.content
