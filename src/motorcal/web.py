"""Public feed app (port 8000): serves one ICS file per series.

Nothing here reads state or renders anything. The refresh and reload jobs push
already-rendered bytes onto `app.state.feeds`, so a GET is a dict lookup -- it
can never mutate state, block on a fetch, or race a rebuild.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from motorcal.config import Config
from motorcal.ics import compute_content_hash

_access_logger = logging.getLogger("motorcal.access")


def create_app(config: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.feeds = {}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/{series}.ics")
    def get_calendar(series: str, request: Request):
        if series not in app.state.config.series:
            raise HTTPException(status_code=404)

        ics_bytes = app.state.feeds.get(series)
        if not ics_bytes:
            raise HTTPException(status_code=503, detail="no usable events for this series")

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
