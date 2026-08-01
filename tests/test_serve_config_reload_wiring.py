"""Confirms app.state is the live source of truth the HTTP routes read.

_cmd_serve wires a hot config reload by reassigning app.state.publication wholesale
(see reload_job in cli.py); this proves the routes pick that up on the very next
request without recreating or restarting the app -- and that config and feeds
always arrive together, never as two separate reassignments a request could land
between.
"""
from fastapi.testclient import TestClient
from tests.conftest import make_config, make_series

from motorcal.web import Publication, create_app

WEC = make_series()
IMSA = make_series(name="IMSA")
ICS = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"


def test_mutating_app_state_reaches_routes_immediately():
    app = create_app(make_config(series={"wec": WEC}))
    app.state.publication = Publication(
        config=app.state.publication.config, feeds={"wec": ICS}, published={}
    )
    client = TestClient(app)

    assert client.get("/imsa.ics").status_code == 404

    # Exactly what reload_job does on a successful hot reload: swap config and feeds
    # in as one new Publication, together, rather than reassigning either alone.
    app.state.publication = Publication(
        config=make_config(series={"wec": WEC, "imsa": IMSA}),
        feeds=app.state.publication.feeds,
        published={},
    )

    # Recognized series now, just no rendered feed yet.
    assert client.get("/imsa.ics").status_code == 503

    app.state.publication = Publication(
        config=app.state.publication.config,
        feeds={**app.state.publication.feeds, "imsa": ICS},
        published={},
    )
    assert client.get("/imsa.ics").status_code == 200
