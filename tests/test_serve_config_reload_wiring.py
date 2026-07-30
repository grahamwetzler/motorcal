"""Confirms app.state is the live source of truth the HTTP routes read.

_cmd_serve wires a hot config reload by reassigning app.state.config and
app.state.feeds (see reload_job in cli.py); this proves the routes pick that up
on the very next request without recreating or restarting the app.
"""
from fastapi.testclient import TestClient
from tests.conftest import make_config, make_series

from motorcal.web import create_app

WEC = make_series()
IMSA = make_series(league_id=4488, name="IMSA")


def test_mutating_app_state_reaches_routes_immediately():
    app = create_app(make_config(series={"wec": WEC}))
    app.state.feeds = {"wec": b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"}
    client = TestClient(app)

    assert client.get("/imsa.ics").status_code == 404

    # Exactly what reload_job does on a successful hot reload: reassign both
    # wholesale to the newly validated bundle and its freshly rendered feeds.
    app.state.config = make_config(series={"wec": WEC, "imsa": IMSA})

    # Recognized series now, just no rendered feed yet.
    assert client.get("/imsa.ics").status_code == 503

    app.state.feeds = {**app.state.feeds, "imsa": b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"}
    assert client.get("/imsa.ics").status_code == 200
