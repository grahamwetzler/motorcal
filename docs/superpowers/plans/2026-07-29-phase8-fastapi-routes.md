# Motorsports Calendar — Phase 8: FastAPI Feed/Status/Liveness/Readiness/Health Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/motorcal/web.py`: a FastAPI application exposing the token-protected calendar feed and status routes, plus liveness/readiness/health checks — all reading only from SQLite (never calling the upstream provider), per the spec's "serving never calls the upstream API" architecture.

**Architecture:** `create_app(db_path, root_config, tokens)` is a factory function (not a module-level global `app`), so tests can construct isolated app instances against a temp database. Every route opens its own short-lived `store.connect()` per request (SQLite WAL mode supports this cheaply) and closes it before returning. Token verification is a small, constant-time-per-token helper reused by every token-protected route.

**Tech Stack:** `fastapi` and its bundled `starlette` (already Phase 1 dependencies). `fastapi.testclient.TestClient` for tests (no real network/server needed).

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous. The "HTTP routes" section is this phase's primary spec.
- Bad calendar tokens return **404** (not 401/403), compared with `secrets.compare_digest` against every configured token (never short-circuit in a way that skips comparing against a valid token that happens to differ in length from the supplied one — `compare_digest` handles that safely per-comparison already).
- Responses from token-protected routes use `Cache-Control: private, no-cache`.
- Serving never calls the upstream provider API — every route reads only from SQLite via `motorcal.store`/`motorcal.ics`/`motorcal.classify` functions already built in Phases 1-7.
- `/livez`: process and database liveness only (no freshness/data checks) — used by the Docker health check specifically so an API outage never causes a restart loop.
- `/readyz`: 200 only after **every** series in `root_config.series` has at least one row in `published_events` (this is "usable stored data" — the exact thing `/c/{token}/{series}.ics` would serve); 503 otherwise, with a per-series breakdown.
- `/healthz`: a more detailed freshness report using `source_snapshot_meta.last_complete_at` (from Phase 4) per series; 503 if any series is stale or has never been successfully refreshed. Monitoring uses this route, not `/livez`.
- A feed with no usable stored events (`published_events` for that series is empty) returns **503**, never an empty `VCALENDAR`.
- Conditional requests: `/c/{token}/{series}.ics` supports `If-None-Match` against the stored feed revision (Phase 7's `sync_feed_revision`) and returns a bodyless **304** on a match. `Last-Modified` is derived from the stored revision's `updated_at`, formatted as a proper RFC 7231 HTTP date (confirmed via prototyping: `email.utils.format_datetime(dt, usegmt=True)` produces the correct format from the UTC-aware datetimes this project already stores everywhere).
- `/c/{token}/status` diagnostics are scoped to what Phases 1-7 actually persist: readiness, per-series freshness, and feed revision info. Phase 6's `RebuildReport` (patch-match errors, unknown-classification UIDs) is **not yet persisted anywhere** — it's an in-memory return value today. Surfacing patch errors and unknown-classification events on `/status` is deferred to Phase 9, which will be the first thing to actually call `rebuild_publication` on a schedule and can persist that report at the same time. Do not invent a diagnostics table in this phase to work around that gap — it belongs with the phase that produces the data.
- Freshness/season simplification: this phase checks `source_snapshot_meta` for the **current calendar year** season only (`str(now.year)`), since full current-vs-future season logic belongs to Phase 9's scheduler. This is a known, documented simplification, not a spec gap this phase needs to fully resolve.
- Application access logs must redact the token-bearing path segment (e.g. `/c/{token}/wec.ics` → `/c/REDACTED/wec.ics`) — implemented as ASGI middleware that logs its own redacted line; production deployment (Phase 10) must also disable `uvicorn`'s own default access log, since that logs the raw path independently of anything this phase builds.
- No pip: dependency management is `uv` only.

---

### Task 1: Token verification, app factory, and `/livez`

**Files:**
- Create: `src/motorcal/web.py`
- Test: `tests/test_web_livez.py`

**Interfaces:**
- Consumes: `check_integrity`, `connect` from `motorcal.store` (Phase 2); `RootConfig` from `motorcal.config` (Phase 1).
- Produces (used by every later task):
  - `def verify_token(token: str, valid_tokens: list[str]) -> bool`.
  - `def create_app(db_path: Path, root_config: RootConfig, tokens: list[str]) -> FastAPI` — stores `db_path`/`root_config`/`tokens` on `app.state`.
  - `GET /livez` route: `{"status": "ok"}` / 200 if the database passes `check_integrity`; 503 with an error detail otherwise. No freshness or data checks.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_livez.py
from fastapi.testclient import TestClient

from motorcal.config import DefaultsConfig, DurationDefaults, RetentionConfig, RootConfig, UnknownTimeConfig
from motorcal.store import connect, init_schema
from motorcal.web import create_app, verify_token


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(durations=DurationDefaults(), alerts={}, include_sessions=["race"]),
        unknown_time=UnknownTimeConfig(),
        series={},
    )


def test_verify_token_accepts_a_configured_token():
    assert verify_token("good-token", ["good-token", "other-token"]) is True


def test_verify_token_rejects_an_unconfigured_token():
    assert verify_token("bad-token", ["good-token"]) is False


def test_verify_token_rejects_against_empty_token_list():
    assert verify_token("anything", []) is False


def test_livez_returns_ok_for_a_healthy_database(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()

    app = create_app(db_path, _root_config(), tokens=["t"])
    client = TestClient(app)
    response = client.get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_livez_returns_503_for_a_corrupt_database(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    with open(db_path, "r+b") as f:
        f.seek(100)
        f.write(b"\xff" * 200)

    app = create_app(db_path, _root_config(), tokens=["t"])
    client = TestClient(app)
    response = client.get("/livez")

    assert response.status_code == 503
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_livez.py -v`
Expected: FAIL / collection error — `motorcal.web` does not exist yet.

- [ ] **Step 3: Write `src/motorcal/web.py`**

```python
"""FastAPI application: token-protected feed/status routes and health checks."""
from __future__ import annotations

import secrets
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
        conn = connect(app.state.db_path)
        try:
            if not check_integrity(conn):
                raise HTTPException(status_code=503, detail="database integrity check failed")
        finally:
            conn.close()
        return {"status": "ok"}

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_web_livez.py -v`
Expected: PASS, 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/motorcal/web.py tests/test_web_livez.py
git commit -m "Add token verification, FastAPI app factory, and /livez"
```

---

### Task 2: `/readyz` and `/healthz`

**Files:**
- Modify: `src/motorcal/web.py`
- Test: `tests/test_web_readyz_healthz.py`

**Interfaces:**
- Consumes: `list_published_events_by_series` (Phase 7), `get_snapshot_meta` (Phase 4), `connect` (Phase 2).
- Produces:
  - `GET /readyz`: 200 with `{"ready": true, "series": {"wec": true, ...}}` only if every configured series has at least one `published_events` row; otherwise 503 with the per-series breakdown showing which are not ready.
  - `GET /healthz`: 200 with `{"healthy": true, "series": {"wec": {"last_complete_at": "...", "stale": false, "event_count": N}, ...}}` only if every series' current-year `source_snapshot_meta` exists and is within `stale_after_hours` (default 12) of `now`; 503 otherwise. A series with no `source_snapshot_meta` row at all reports `last_complete_at: null`, `stale: true`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_readyz_healthz.py
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.store import (
    connect,
    init_schema,
    transaction,
    upsert_published_event,
    upsert_snapshot_meta,
)
from motorcal.web import create_app


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(durations=DurationDefaults(), alerts={}, include_sessions=["race"]),
        unknown_time=UnknownTimeConfig(),
        series={
            "wec": SeriesConfig(league_id=4413, name="WEC", max_round=20),
            "f1": SeriesConfig(league_id=4370, name="Formula 1", max_round=30),
        },
    )


def _insert_published(conn, uid, series):
    upsert_published_event(
        conn, uid=uid, series=series, session_type="race", summary="S",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=3600, location="L", description="D", status="CONFIRMED",
        sequence=1, dtstamp="t0", last_modified="t0", fingerprint="fp", alarms_json="[]",
        source_provider="thesportsdb", source_id_event="1", synthetic_uid=None,
        cancelled_at=None, retain_until=None,
    )


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_readyz_returns_503_when_a_series_has_no_published_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", "wec")  # f1 has nothing
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["series"]["wec"] is True
    assert body["series"]["f1"] is False


def test_readyz_returns_200_when_every_series_has_published_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", "wec")
        _insert_published(conn, "u2", "f1")
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/readyz")

    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_healthz_returns_503_when_a_series_has_never_been_refreshed(tmp_path):
    conn = _fresh_conn(tmp_path)
    now = datetime.now(timezone.utc)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", str(now.year), now.isoformat(), 5)
        # f1 has no snapshot_meta row at all
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["healthy"] is False
    assert body["series"]["f1"]["stale"] is True
    assert body["series"]["f1"]["last_complete_at"] is None


def test_healthz_returns_503_when_a_series_is_stale(tmp_path):
    conn = _fresh_conn(tmp_path)
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(hours=48)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", str(now.year), stale_time.isoformat(), 5)
        upsert_snapshot_meta(conn, "thesportsdb", "f1", str(now.year), now.isoformat(), 5)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["series"]["wec"]["stale"] is True
    assert body["series"]["f1"]["stale"] is False


def test_healthz_returns_200_when_every_series_is_fresh(tmp_path):
    conn = _fresh_conn(tmp_path)
    now = datetime.now(timezone.utc)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", str(now.year), now.isoformat(), 5)
        upsert_snapshot_meta(conn, "thesportsdb", "f1", str(now.year), now.isoformat(), 3)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json()["healthy"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_readyz_healthz.py -v`
Expected: FAIL / collection error — `/readyz`/`/healthz` don't exist yet.

- [ ] **Step 3: Append to `src/motorcal/web.py`**

Add these imports to the top (extend the existing `from motorcal.store import ...` line rather than duplicating it):

```python
from datetime import datetime, timezone

from fastapi.responses import JSONResponse

from motorcal.store import (
    check_integrity,
    connect,
    get_snapshot_meta,
    list_published_events_by_series,
)

DEFAULT_STALE_AFTER_HOURS = 12
```

Inside `create_app`, after the `/livez` route, add:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_web_readyz_healthz.py -v`
Expected: PASS, 5 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-7 (240) plus this phase's 5 (Task 1) plus 5 (Task 2) pass — 250 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/web.py tests/test_web_readyz_healthz.py
git commit -m "Add /readyz and /healthz routes"
```

---

### Task 3: `/c/{token}/{series}.ics` — the feed route

**Files:**
- Modify: `src/motorcal/web.py`
- Test: `tests/test_web_calendar_route.py`

**Interfaces:**
- Consumes: `verify_token` (Task 1); `render_calendar_bytes`, `sync_feed_revision` (Phase 7); `list_published_events_by_series` (Phase 7).
- Produces:
  - `GET /c/{token}/{series}.ics`: 404 for a bad token or an unconfigured series; 503 if the series has no `published_events` rows; otherwise renders the calendar, syncs the feed revision, and returns it with `Content-Type: text/calendar`, `Cache-Control: private, no-cache`, `ETag`, and `Last-Modified` headers. A matching `If-None-Match` request header returns a bodyless 304 with the same headers.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_calendar_route.py
from fastapi.testclient import TestClient

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.store import connect, init_schema, transaction, upsert_published_event
from motorcal.web import create_app


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(durations=DurationDefaults(), alerts={}, include_sessions=["race"]),
        unknown_time=UnknownTimeConfig(),
        series={"wec": SeriesConfig(league_id=4413, name="WEC", max_round=20)},
    )


def _insert_published(conn, uid="u1", series="wec"):
    upsert_published_event(
        conn, uid=uid, series=series, session_type="race", summary="6 Hours of Imola",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=6 * 3600, location="Imola, Italy", description="D", status="CONFIRMED",
        sequence=1, dtstamp="2026-01-01T00:00:00+00:00", last_modified="2026-01-01T00:00:00+00:00",
        fingerprint="fp", alarms_json="[]", source_provider="thesportsdb", source_id_event="1",
        synthetic_uid=None, cancelled_at=None, retain_until=None,
    )


def test_bad_token_returns_404(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/bad-token/wec.ics")

    assert response.status_code == 404


def test_unconfigured_series_returns_404(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/good-token/nonexistent-series.ics")

    assert response.status_code == 404


def test_series_with_no_published_events_returns_503(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/good-token/wec.ics")

    assert response.status_code == 503


def test_valid_request_returns_ics_with_expected_headers(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        _insert_published(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/good-token/wec.ics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.headers["cache-control"] == "private, no-cache"
    assert "etag" in response.headers
    assert "last-modified" in response.headers
    assert b"BEGIN:VCALENDAR" in response.content
    assert b"6 Hours of Imola" in response.content


def test_conditional_request_with_matching_etag_returns_304(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        _insert_published(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    client = TestClient(app)
    first = client.get("/c/good-token/wec.ics")

    second = client.get(
        "/c/good-token/wec.ics", headers={"If-None-Match": first.headers["etag"]}
    )

    assert second.status_code == 304
    assert len(second.content) == 0


def test_conditional_request_with_stale_etag_returns_200(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        _insert_published(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get(
        "/c/good-token/wec.ics", headers={"If-None-Match": '"stale-value"'}
    )

    assert response.status_code == 200
    assert len(response.content) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_calendar_route.py -v`
Expected: FAIL / collection error — `/c/{token}/{series}.ics` doesn't exist yet.

- [ ] **Step 3: Append to `src/motorcal/web.py`**

Add these imports (extend existing lines where applicable rather than duplicating):

```python
from email.utils import format_datetime

from fastapi import Request
from fastapi.responses import Response

from motorcal.ics import render_calendar_bytes, sync_feed_revision
```

Inside `create_app`, after the `/healthz` route, add:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_web_calendar_route.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-7 and Phase 8 Tasks 1-2 (250) plus this task's 6 pass — 256 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/web.py tests/test_web_calendar_route.py
git commit -m "Add /c/{token}/{series}.ics with ETag/304/Cache-Control support"
```

---

### Task 4: `/c/{token}/status` and token-redaction access logging

**Files:**
- Modify: `src/motorcal/web.py`
- Test: `tests/test_web_status_and_logging.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces:
  - `GET /c/{token}/status`: 404 for a bad token; otherwise a JSON body combining readiness and health information (reusing the same per-series computation as `/readyz`/`/healthz`) plus each series' current feed revision (`get_feed_revision`, Phase 7) if one exists.
  - `class RedactTokenMiddleware` (Starlette `BaseHTTPMiddleware`) — logs `"{method} {redacted_path} -> {status_code}"` via a `logging.getLogger("motorcal.access")` logger, where the path's `/c/{token}/...` segment is replaced with `/c/REDACTED/...`. Registered on the app in `create_app`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_status_and_logging.py
import logging

from fastapi.testclient import TestClient

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.store import connect, init_schema, transaction, upsert_published_event, upsert_snapshot_meta
from motorcal.web import create_app


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(durations=DurationDefaults(), alerts={}, include_sessions=["race"]),
        unknown_time=UnknownTimeConfig(),
        series={"wec": SeriesConfig(league_id=4413, name="WEC", max_round=20)},
    )


def test_status_bad_token_returns_404(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/bad-token/status")

    assert response.status_code == 404


def test_status_reports_readiness_and_health_per_series(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        upsert_published_event(
            conn, uid="u1", series="wec", session_type="race", summary="S",
            start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
            duration_seconds=3600, location="L", description="D", status="CONFIRMED",
            sequence=1, dtstamp="t0", last_modified="t0", fingerprint="fp", alarms_json="[]",
            source_provider="thesportsdb", source_id_event="1", synthetic_uid=None,
            cancelled_at=None, retain_until=None,
        )
        upsert_snapshot_meta(conn, "thesportsdb", "wec", str(now.year), now.isoformat(), 1)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    response = TestClient(app).get("/c/good-token/status")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["healthy"] is True
    assert body["series"]["wec"]["ready"] is True
    assert body["series"]["wec"]["stale"] is False


def test_access_log_redacts_the_token(tmp_path, caplog):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["super-secret-token"])
    client = TestClient(app)

    with caplog.at_level(logging.INFO, logger="motorcal.access"):
        client.get("/c/super-secret-token/status")

    access_records = [r for r in caplog.records if r.name == "motorcal.access"]
    assert len(access_records) == 1
    message = access_records[0].getMessage()
    assert "super-secret-token" not in message
    assert "REDACTED" in message


def test_access_log_redacts_the_token_even_on_a_404(tmp_path, caplog):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["good-token"])
    client = TestClient(app)

    with caplog.at_level(logging.INFO, logger="motorcal.access"):
        client.get("/c/leaked-guess-token/status")

    access_records = [r for r in caplog.records if r.name == "motorcal.access"]
    assert len(access_records) == 1
    assert "leaked-guess-token" not in access_records[0].getMessage()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_status_and_logging.py -v`
Expected: FAIL / collection error — `/c/{token}/status` and the redaction middleware don't exist yet.

- [ ] **Step 3: Append to `src/motorcal/web.py`**

Add these imports (extend existing lines where applicable):

```python
import logging
import re

from starlette.middleware.base import BaseHTTPMiddleware

from motorcal.store import get_feed_revision

_TOKEN_PATH_RE = re.compile(r"^/c/[^/]+")
_access_logger = logging.getLogger("motorcal.access")


class RedactTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        redacted_path = _TOKEN_PATH_RE.sub("/c/REDACTED", request.url.path)
        _access_logger.info("%s %s -> %s", request.method, redacted_path, response.status_code)
        return response
```

In `create_app`, register the middleware right after `app = FastAPI()`:

```python
    app.add_middleware(RedactTokenMiddleware)
```

Add the `/c/{token}/status` route after `/c/{token}/{series}.ics`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_web_status_and_logging.py -v`
Expected: PASS, 4 passed.

- [ ] **Step 5: Run the entire test suite**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-7 and Phase 8 Tasks 1-4 pass — 260 passed total (240 + 5 + 5 + 6 + 4).

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/web.py tests/test_web_status_and_logging.py
git commit -m "Add /c/{token}/status and token-redaction access logging middleware"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: all 5 routes with their specified behaviors (HTTP routes section); 404 on bad token via `secrets.compare_digest`; `Cache-Control: private, no-cache`; 503 on empty feed instead of an empty calendar; conditional-request 304 support with deterministic `ETag`/`Last-Modified` (built directly on Phase 7's `sync_feed_revision`); token redaction in access logs.
- Explicitly out of scope for this phase (later phases own them): actually calling `rebuild_publication` on a schedule and persisting `RebuildReport` (patch errors, unknown-classification UIDs) so `/status` can surface them — Phase 9; the Docker health check configuration itself and disabling `uvicorn`'s own access log — Phase 10; proper current-vs-future season determination for `/healthz`'s freshness check (this phase only checks the current calendar year) — Phase 9 owns season logic and may want to extend this phase's freshness check once it exists.
- Type consistency check: `create_app`'s `tokens: list[str]` parameter is intentionally NOT read from `os.environ` inside `web.py` itself — Phase 9 (or `cli.py`) is responsible for parsing `MOTORCAL_TOKENS` (comma-separated, per the spec) and passing the resulting list in, keeping `web.py` environment-agnostic and directly testable via `create_app(...)` without mutating process-global environment state.
