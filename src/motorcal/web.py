"""Public feed app (port 8000): serves one ICS file per series.

The refresh and reload jobs push already-rendered bytes onto `app.state.feeds`,
so the default (unfiltered) GET is a dict lookup -- it can never mutate state,
block on a fetch, or race a rebuild. A request with `?practices=false` and/or
`?qualifying=false` re-renders from `app.state.published` on the fly instead,
since the pre-rendered bytes only cover the everything-included case.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from motorcal.config import Config
from motorcal.ics import compute_content_hash, render_calendar_bytes
from motorcal.models import SessionType

_access_logger = logging.getLogger("motorcal.access")

# Session types excluded by `?qualifying=false`. Different series name their
# pole-setting session differently (WEC's hyperpole, F1's sprint qualifying),
# but they're all "qualifying" from a subscriber's point of view.
_QUALIFYING_TYPES = {
    SessionType.QUALIFYING, SessionType.HYPERPOLE, SessionType.SPRINT_QUALIFYING,
}


def create_app(config: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.feeds = {}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/{series}.ics")
    def get_calendar(series: str, request: Request, practices: bool = True, qualifying: bool = True):
        if series not in app.state.config.series:
            raise HTTPException(status_code=404)

        ics_bytes = app.state.feeds.get(series)
        if not ics_bytes:
            raise HTTPException(status_code=503, detail="no usable events for this series")

        if not practices or not qualifying:
            excluded = set()
            if not practices:
                excluded.add(SessionType.PRACTICE)
            if not qualifying:
                excluded |= _QUALIFYING_TYPES
            events = [e for e in app.state.published.get(series, []) if e.session_type not in excluded]
            ics_bytes = render_calendar_bytes(app.state.config.series[series], events)

        # ETag over the exact bytes served: the only revalidation signal this feed
        # needs. A Last-Modified derived from the events would lie whenever
        # retention prunes one, since that changes the feed without touching any
        # remaining event's timestamp.
        etag = f'"{compute_content_hash(ics_bytes)}"'
        headers = {"Cache-Control": "public, no-cache", "ETag": etag}

        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)

        _access_logger.info("GET /%s.ics -> 200 (%d bytes)", series, len(ics_bytes))
        return Response(content=ics_bytes, media_type="text/calendar", headers=headers)

    return app
