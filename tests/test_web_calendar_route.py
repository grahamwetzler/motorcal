from fastapi.testclient import TestClient
from tests.conftest import make_config, make_series

from motorcal.web import create_app

ROOT_CONFIG = make_config(series={"wec": make_series()})
ICS = b"BEGIN:VCALENDAR\r\nSUMMARY:6 Hours of Imola\r\nEND:VCALENDAR\r\n"


def _client(feeds=None):
    app = create_app(ROOT_CONFIG)
    app.state.feeds = feeds or {}
    return TestClient(app)


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
