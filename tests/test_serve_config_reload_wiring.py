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
        config=app.state.publication.config, feeds={"events": ICS}, published={}
    )
    client = TestClient(app)

    assert client.get("/events.ics?series=imsa").status_code == 400  # not in config yet

    # Exactly what reload_job does on a successful hot reload: swap config and feeds
    # in as one new Publication, together, rather than reassigning either alone.
    app.state.publication = Publication(
        config=make_config(series={"wec": WEC, "imsa": IMSA}),
        feeds={"events": ICS + b"X"},
        published={},
    )

    # Both halves of the swap are visible on the very next request.
    response = client.get("/events.ics")
    assert response.status_code == 200
    assert response.content == ICS + b"X"
    assert client.get("/events.ics?series=imsa").status_code == 200
