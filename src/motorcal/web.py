"""FastAPI application: token-protected feed/status routes and health checks."""
from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException

from motorcal.config import RootConfig
from motorcal.store import check_integrity, connect


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

    return app
