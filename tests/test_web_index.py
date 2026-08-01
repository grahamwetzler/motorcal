"""The feed-builder page at `/`.

The page's own logic is JavaScript and checks itself in the browser console.
What is worth pinning down here is the contract between the two: the handler
hands the page a series list and a set of real upcoming events, and it has to
hand them over correctly -- right series, right events, no way for a value in
the data to break out of the <script> block it is embedded in.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from tests.conftest import make_config, make_series

from motorcal.models import EventStatus, PublishedEvent, SessionType
from motorcal.web import Publication, _example_events, create_app

NOW = datetime(2026, 4, 1, 9, tzinfo=timezone.utc)
CONFIG = make_config(series={"wec": make_series()})


def _event(
    uid, *, series="wec", session_type=SessionType.RACE, start=NOW + timedelta(days=1),
    all_day_date=None, summary=None, alarms=(), status=EventStatus.CONFIRMED,
    location="Imola", time_confirmed=True,
):
    return PublishedEvent(
        uid=uid, series=series, session_type=session_type, summary=summary or uid,
        start=start, all_day_date=all_day_date, time_confirmed=time_confirmed,
        duration_seconds=3600, location=location, description="D", status=status,
        sequence=1, dtstamp=NOW, last_modified=NOW, fingerprint="fp",
        session_key=uid, alarms=list(alarms),
    )


def _client(config=CONFIG, published=None):
    app = create_app(config)
    app.state.publication = Publication(config=config, feeds={}, published=published or {})
    return TestClient(app)


def _injected(body: str, name: str):
    """Pull one of the two injected JSON literals back out of the page."""
    match = re.search(rf"^const {name} = (.*);$", body, re.MULTILINE)
    assert match, f"{name} was not injected"
    return json.loads(match.group(1))


# --------------------------------------------------------------------- the route


def test_index_is_html_that_revalidates():
    response = _client().get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # The page carries live event times, so it must not sit in a browser cache.
    assert response.headers["cache-control"] == "public, no-cache"


def test_index_offers_exactly_the_configured_series():
    config = make_config(series={
        "wec": make_series(name="WEC"),
        "f1": make_series(name="Formula 1"),
    })

    series = _injected(_client(config).get("/").text, "SERIES")

    assert series == [{"key": "wec", "name": "WEC"}, {"key": "f1", "name": "Formula 1"}]


def test_both_placeholders_are_substituted():
    body = _client().get("/").text

    assert "__SERIES_JSON__" not in body
    assert "__UPCOMING_JSON__" not in body
    assert _injected(body, "UPCOMING") == []


def test_a_series_name_cannot_break_out_of_the_script_block():
    config = make_config(series={"wec": make_series(name="</script><b>pwned")})

    body = _client(config).get("/").text

    assert "</script><b>pwned" not in body
    assert _injected(body, "SERIES") == [{"key": "wec", "name": "</script><b>pwned"}]


# ------------------------------------------------------------- the example events


def test_next_event_of_each_series_and_type_is_offered_soonest_first():
    published = {"wec": [
        _event("past", start=NOW - timedelta(days=1)),
        _event("next-race", start=NOW + timedelta(days=1)),
        _event("later-race", start=NOW + timedelta(days=2)),
        _event("quali", session_type=SessionType.QUALIFYING, start=NOW + timedelta(hours=12)),
    ]}

    upcoming = _example_events(CONFIG, published, NOW)

    assert [event["title"] for event in upcoming] == ["WEC: quali", "WEC: next-race"]
    assert [event["type"] for event in upcoming] == ["qualifying", "race"]


def test_an_event_carries_everything_the_preview_shows():
    published = {"wec": [_event(
        "race", summary="6 Hours of Imola", alarms=["-15m"], location="Imola, Italy",
    )]}

    (event,) = _example_events(CONFIG, published, NOW)

    assert event == {
        "series": "wec",
        "type": "race",
        "title": "WEC: 6 Hours of Imola",
        "start": "2026-04-02T09:00:00+00:00",
        "date": None,
        "duration": 3600,
        "location": "Imola, Italy",
        "time_confirmed": True,
        "alarms": ["-15m"],
    }


def test_postponed_event_is_titled_the_way_the_ics_titles_it():
    published = {"wec": [_event("race", summary="Imola", status=EventStatus.TENTATIVE)]}

    (event,) = _example_events(CONFIG, published, NOW)

    assert event["title"] == "[Postponed] WEC: Imola"


def test_all_day_event_stays_upcoming_for_the_whole_of_its_day():
    # An all-day session has no time of its own, so it must not drop off the
    # page at midnight UTC on the morning it happens.
    today = {"wec": [_event("test", start=None, all_day_date="2026-04-01")]}
    yesterday = {"wec": [_event("test", start=None, all_day_date="2026-03-31")]}

    assert len(_example_events(CONFIG, today, NOW)) == 1
    assert _example_events(CONFIG, yesterday, NOW) == []
