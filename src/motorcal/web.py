"""Public feed app (port 8000): one ICS file per series, plus all of them combined.

The refresh and reload jobs each build a fresh `Publication` and swap it onto
`app.state.publication` in one assignment. A request reads that attribute
exactly once at the top of the handler, so it always sees one consistent
generation of config/feeds/published together -- never config from a rebuild
paired with feeds from the one before it (or a series that generation removed).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from motorcal.config import COMBINED_SERIES_KEY, Config
from motorcal.ics import compute_content_hash, render_calendar_bytes, render_combined_bytes
from motorcal.models import PublishedEvent, SessionType

_access_logger = logging.getLogger("motorcal.access")

# Session types excluded by `?qualifying=false`. Different series name their
# pole-setting session differently (WEC's hyperpole, F1's sprint qualifying),
# but they're all "qualifying" from a subscriber's point of view.
_QUALIFYING_TYPES = {
    SessionType.QUALIFYING, SessionType.HYPERPOLE, SessionType.SPRINT_QUALIFYING,
}


@dataclass(frozen=True)
class Publication:
    """One consistent generation of what the app serves.

    Always replaced wholesale (never mutated in place) so assigning it to
    `app.state.publication` is the one atomic handoff a concurrent request can
    observe -- either the whole old generation or the whole new one.
    """

    config: Config
    feeds: dict[str, bytes]
    published: dict[str, list[PublishedEvent]]


def create_app(config: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.publication = Publication(config=config, feeds={}, published={})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # Declared before /{series}.ics: FastAPI matches in declaration order, and the
    # series route's path parameter would otherwise swallow "all". `load_config`
    # rejects a series named "all", so nothing legitimate is shadowed here.
    @app.get("/all.ics")
    def get_combined_calendar(request: Request, practices: bool = True, qualifying: bool = True):
        publication = app.state.publication

        ics_bytes = publication.feeds.get(COMBINED_SERIES_KEY)
        if not ics_bytes:
            raise HTTPException(status_code=503, detail="no usable events")

        if not practices or not qualifying:
            published = {
                series: _filtered(events, practices, qualifying)
                for series, events in publication.published.items()
            }
            ics_bytes = render_combined_bytes(publication.config, published)

        return _conditional_response(ics_bytes, request, COMBINED_SERIES_KEY)

    @app.get("/{series}.ics")
    def get_calendar(series: str, request: Request, practices: bool = True, qualifying: bool = True):
        publication = app.state.publication

        if series not in publication.config.series:
            raise HTTPException(status_code=404)

        ics_bytes = publication.feeds.get(series)
        if not ics_bytes:
            raise HTTPException(status_code=503, detail="no usable events for this series")

        if not practices or not qualifying:
            events = _filtered(publication.published.get(series, []), practices, qualifying)
            ics_bytes = render_calendar_bytes(publication.config.series[series], events)

        return _conditional_response(ics_bytes, request, series)

    return app


def _filtered(
    events: list[PublishedEvent], practices: bool, qualifying: bool
) -> list[PublishedEvent]:
    excluded = set()
    if not practices:
        excluded.add(SessionType.PRACTICE)
    if not qualifying:
        excluded |= _QUALIFYING_TYPES
    return [e for e in events if e.session_type not in excluded]


def _conditional_response(ics_bytes: bytes, request: Request, label: str) -> Response:
    """Serve the feed bytes, answering a matching If-None-Match with a 304.

    ETag over the exact bytes served is the only revalidation signal this feed
    needs. A Last-Modified derived from the events would lie whenever retention
    prunes one, since that changes the feed without touching any remaining
    event's timestamp.
    """
    etag = f'"{compute_content_hash(ics_bytes)}"'
    headers = {"Cache-Control": "public, no-cache", "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    _access_logger.info("GET /%s.ics -> 200 (%d bytes)", label, len(ics_bytes))
    return Response(content=ics_bytes, media_type="text/calendar", headers=headers)
