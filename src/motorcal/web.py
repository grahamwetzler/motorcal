"""FastAPI application: token-protected feed/status routes and health checks."""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from motorcal.config import RootConfig
from motorcal.ics import render_calendar_bytes, sync_feed_revision
from motorcal.store import (
    check_integrity,
    connect,
    get_feed_revision,
    get_snapshot_meta,
    list_published_events_by_series,
)

DEFAULT_STALE_AFTER_HOURS = 12

_TOKEN_PATH_RE = re.compile(r"^/c/[^/]+")
_access_logger = logging.getLogger("motorcal.access")


class RedactTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        redacted_path = _TOKEN_PATH_RE.sub("/c/REDACTED", request.url.path)
        _access_logger.info("%s %s -> %s", request.method, redacted_path, response.status_code)
        return response


def verify_token(token: str, valid_tokens: list[str]) -> bool:
    """Constant-time-per-comparison check against every configured token."""
    return any(secrets.compare_digest(token, valid) for valid in valid_tokens)


def create_app(db_path: Path, root_config: RootConfig, tokens: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RedactTokenMiddleware)
    app.state.db_path = db_path
    app.state.root_config = root_config
    app.state.tokens = tokens

    @app.get("/livez")
    def livez():
        try:
            conn = connect(app.state.db_path)
        except sqlite3.DatabaseError:
            raise HTTPException(status_code=503, detail="database integrity check failed")
        try:
            if not check_integrity(conn):
                raise HTTPException(status_code=503, detail="database integrity check failed")
        finally:
            conn.close()
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        conn = connect(app.state.db_path)
        try:
            series_ready = {
                series: len(list_published_events_by_series(conn, series)) > 0
                for series in app.state.root_config.series
            }
        finally:
            conn.close()
        all_ready = all(series_ready.values())
        body = {"ready": all_ready, "series": series_ready}
        return JSONResponse(content=body, status_code=200 if all_ready else 503)

    @app.get("/healthz")
    def healthz(stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS):
        conn = connect(app.state.db_path)
        now = datetime.now(timezone.utc)
        season = str(now.year)
        try:
            series_health = {}
            for series in app.state.root_config.series:
                meta = get_snapshot_meta(conn, "thesportsdb", series, season)
                if meta is None:
                    series_health[series] = {
                        "last_complete_at": None, "stale": True, "event_count": 0,
                    }
                    continue
                last_complete_at = datetime.fromisoformat(meta["last_complete_at"])
                age_hours = (now - last_complete_at).total_seconds() / 3600
                series_health[series] = {
                    "last_complete_at": meta["last_complete_at"],
                    "stale": age_hours > stale_after_hours,
                    "event_count": meta["last_event_count"],
                }
        finally:
            conn.close()
        all_healthy = all(not v["stale"] for v in series_health.values())
        body = {"healthy": all_healthy, "series": series_health}
        return JSONResponse(content=body, status_code=200 if all_healthy else 503)

    @app.get("/c/{token}/{series}.ics")
    def get_calendar(token: str, series: str, request: Request):
        if not verify_token(token, app.state.tokens):
            raise HTTPException(status_code=404)
        if series not in app.state.root_config.series:
            raise HTTPException(status_code=404)

        conn = connect(app.state.db_path)
        try:
            rows = list_published_events_by_series(conn, series)
            if not rows:
                raise HTTPException(status_code=503, detail="no usable stored events for this series")

            series_config = app.state.root_config.series[series]
            ics_bytes = render_calendar_bytes(conn, series, series_config)
            now_iso = datetime.now(timezone.utc).isoformat()
            revision = sync_feed_revision(conn, series, ics_bytes, now_iso)
        finally:
            conn.close()

        etag = f'"{revision.revision}"'
        last_modified = format_datetime(datetime.fromisoformat(revision.updated_at), usegmt=True)
        headers = {
            "Cache-Control": "private, no-cache",
            "ETag": etag,
            "Last-Modified": last_modified,
        }

        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)

        return Response(content=ics_bytes, media_type="text/calendar", headers=headers)

    @app.get("/c/{token}/status")
    def get_status(token: str):
        if not verify_token(token, app.state.tokens):
            raise HTTPException(status_code=404)

        conn = connect(app.state.db_path)
        now = datetime.now(timezone.utc)
        season = str(now.year)
        try:
            series_status = {}
            for series in app.state.root_config.series:
                ready = len(list_published_events_by_series(conn, series)) > 0
                meta = get_snapshot_meta(conn, "thesportsdb", series, season)
                if meta is None:
                    stale, last_complete_at = True, None
                else:
                    last_complete_at = meta["last_complete_at"]
                    age_hours = (now - datetime.fromisoformat(last_complete_at)).total_seconds() / 3600
                    stale = age_hours > DEFAULT_STALE_AFTER_HOURS
                revision_row = get_feed_revision(conn, series)
                series_status[series] = {
                    "ready": ready,
                    "stale": stale,
                    "last_complete_at": last_complete_at,
                    "feed_revision": revision_row["revision"] if revision_row else None,
                    "feed_updated_at": revision_row["updated_at"] if revision_row else None,
                }
        finally:
            conn.close()

        body = {
            "ready": all(v["ready"] for v in series_status.values()),
            "healthy": all(not v["stale"] for v in series_status.values()),
            "series": series_status,
        }
        return body

    return app
