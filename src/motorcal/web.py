"""FastAPI application: token-protected feed/status routes and health checks."""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from motorcal.config import RootConfig
from motorcal.store import (
    check_integrity,
    connect,
    get_snapshot_meta,
    list_published_events_by_series,
)

DEFAULT_STALE_AFTER_HOURS = 12


def verify_token(token: str, valid_tokens: list[str]) -> bool:
    """Constant-time-per-comparison check against every configured token."""
    return any(secrets.compare_digest(token, valid) for valid in valid_tokens)


def create_app(db_path: Path, root_config: RootConfig, tokens: list[str]) -> FastAPI:
    app = FastAPI()
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

    return app
