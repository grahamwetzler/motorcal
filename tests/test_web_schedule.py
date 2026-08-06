"""The schedule page at `/schedule` and the data behind it at `/sessions.json`.

The page's own logic is JavaScript and checks itself in the browser console.
What matters here is the endpoint it reads: every weekend in the data directory,
in the order they run, with durations already resolved -- the page does not have
the config and cannot work any of that out for itself.
"""

from pathlib import Path

from fastapi.testclient import TestClient

import motorcal.web
from motorcal.config import EventConfig
from motorcal.web import Publication, create_app
from tests.conftest import make_config, make_event, make_series, make_session


def _client(config):
    app = create_app(config)
    app.state.publication = Publication(config=config, feed=b"", published={})
    return TestClient(app)


CONFIG = make_config(
    series={
        "wec": make_series(
            name="WEC",
            durations={"race": "6h"},
            events=[
                EventConfig(
                    name="6 Hours of Imola",
                    url="https://example.com/imola",
                    location="Imola, Italy",
                    round=1,
                    sessions=[
                        # Out of order on purpose: the endpoint sorts, the page
                        # renders what it is handed.
                        make_session(
                            "wec-race", type="race", start="2026-04-19T13:00:00+00:00"
                        ),
                        make_session(
                            "wec-qualifying",
                            type="qualifying",
                            label="Qualifying",
                            start="2026-04-18T12:30:00+00:00",
                            duration="30m",
                        ),
                    ],
                )
            ],
        ),
        "f1": make_series(
            name="Formula 1",
            events=[
                make_event(
                    "f1-test",
                    name="Pre-season testing",
                    type="testing",
                    date="2026-02-11",
                ),
                make_event(
                    "f1-quali",
                    name="Bahrain Grand Prix",
                    type="qualifying",
                    date="2026-10-09",
                    tbc=True,
                ),
            ],
        ),
    }
)


def test_sessions_json_lists_every_series_and_weekend():
    body = _client(CONFIG).get("/sessions.json").json()

    assert body["series"] == [
        {"key": "wec", "name": "WEC"},
        {"key": "f1", "name": "Formula 1"},
    ]
    assert [(event["series"], event["name"]) for event in body["events"]] == [
        ("f1", "Pre-season testing"),
        ("wec", "6 Hours of Imola"),
        ("f1", "Bahrain Grand Prix"),
    ]


def test_a_weekend_carries_what_the_whole_weekend_shares():
    """The page groups by weekend, so name/round/location/url have to survive --
    publishing flattens them into one summary string, which is why this is built
    from the config instead."""
    body = _client(CONFIG).get("/sessions.json").json()
    imola = next(
        event for event in body["events"] if event["name"] == "6 Hours of Imola"
    )

    assert imola["round"] == 1
    assert imola["location"] == "Imola, Italy"
    assert imola["url"] == "https://example.com/imola"


def test_sessions_are_in_running_order_with_durations_resolved():
    body = _client(CONFIG).get("/sessions.json").json()
    imola = next(
        event for event in body["events"] if event["name"] == "6 Hours of Imola"
    )

    assert [session["label"] for session in imola["sessions"]] == ["Qualifying", ""]
    # The session's own duration, then the series default -- resolve_duration's
    # priority, not something the page could work out.
    assert [session["duration"] for session in imola["sessions"]] == [1800, 6 * 3600]


def test_a_withdrawn_session_is_served_with_its_status():
    """The page marks these; it can only do that if the status reaches it. A
    cancelled race dropped to CONFIRMED here would be published as an ordinary
    upcoming session, which is worse than not listing it at all."""
    config = make_config(
        series={
            "wec": make_series(
                events=[
                    EventConfig(
                        name="Cancelled weekend",
                        sessions=[
                            make_session(
                                "wec-off",
                                type="race",
                                start="2026-04-19T13:00:00+00:00",
                                status="CANCELLED",
                            ),
                            make_session(
                                "wec-moved",
                                type="qualifying",
                                start="2026-04-18T13:00:00+00:00",
                                status="TENTATIVE",
                            ),
                        ],
                    )
                ]
            )
        }
    )
    body = _client(config).get("/sessions.json").json()

    assert [session["status"] for session in body["events"][0]["sessions"]] == [
        "TENTATIVE",
        "CANCELLED",
    ]
    # Nothing else in the config says CONFIRMED, so the default has to survive too.
    imola = next(
        event
        for event in _client(CONFIG).get("/sessions.json").json()["events"]
        if event["name"] == "6 Hours of Imola"
    )
    assert {session["status"] for session in imola["sessions"]} == {"CONFIRMED"}


def test_an_unannounced_time_keeps_its_date_and_says_so():
    body = _client(CONFIG).get("/sessions.json").json()
    events = {event["name"]: event for event in body["events"]}

    testing = events["Pre-season testing"]["sessions"][0]
    assert (testing["start"], testing["date"], testing["tbc"]) == (
        None,
        "2026-02-11",
        False,
    )
    unannounced = events["Bahrain Grand Prix"]["sessions"][0]
    assert (unannounced["start"], unannounced["date"], unannounced["tbc"]) == (
        None,
        "2026-10-09",
        True,
    )


def test_the_schedule_page_and_stylesheet_are_served():
    client = _client(CONFIG)

    page = client.get("/schedule")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "public, no-cache"
    assert '<link rel="stylesheet" href="/motorcal.css">' in page.text

    css = client.get("/motorcal.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    # Both pages depend on this having really moved out of index.html.
    assert "--series-1" in css.text


def test_both_pages_link_to_each_other():
    client = _client(CONFIG)

    assert 'href="/schedule"' in client.get("/").text
    assert 'href="/"' in client.get("/schedule").text


def test_the_new_routes_reject_query_parameters():
    client = _client(CONFIG)

    for path in ("/schedule", "/sessions.json", "/motorcal.css"):
        assert client.get(path, params={"series": "wec"}).status_code == 400, path


def test_the_page_is_served_from_disk_each_request():
    """Same contract as index.html: edit the file, reload the browser, no restart."""
    path = Path(motorcal.web.__file__).parent / "schedule.html"
    original = path.read_text()
    client = _client(CONFIG)
    try:
        path.write_text(original.replace("Motor<b>cal</b>", "Edited"))
        assert "Edited" in client.get("/schedule").text
    finally:
        path.write_text(original)
