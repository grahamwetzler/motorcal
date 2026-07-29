# Motorsports Calendar — Phase 9: Scheduler and Runtime Config Reload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/motorcal/refresh.py`: the orchestration layer tying together the provider (Phase 3), classification/source-storage (Phase 4), patch/synthetic-event validation (Phase 5), publication rebuild (Phase 6), and ICS/feed-revision rendering (Phase 7) into one scheduled refresh cycle, plus a config-file-change poller for hot reload — then wire both into `cli.py`'s `serve` command alongside Phase 8's FastAPI app.

**Architecture:** This is the final integration point of the whole pipeline. `refresh.py` contains no new business logic of its own beyond season determination and orchestration sequencing — every actual decision (classification, patch matching, cancellation, fingerprinting, rendering) is delegated to the module that already owns it. The refresh cycle and the config-reload poller both end by calling the exact same `rebuild_publication` (Phase 6), so "a scheduled refresh happened" and "the config file changed" produce identical downstream behavior.

**Tech Stack:** `apscheduler` (already a Phase 1 dependency, unused until now) for cron scheduling. `uvicorn` (already a Phase 1 dependency) to run Phase 8's FastAPI app. No new dependencies.

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous. The "Season and retention policy," "Rate limiting and concurrency," and "Configuration and reload behavior" sections are this phase's primary spec.
- Season fetching: always fetch the current calendar-year season (`str(now.year)`); once `now` has passed `config.source.next_season_from` (an `"MM-DD"` string) for the current year, ALSO fetch next year's season, marked as NOT the current season (`is_current_season=False`) for Phase 4's `ingest_snapshot` suspicious-empty rule. Confirmed via prototyping: comparing `now` against `now.replace(month=M, day=D, ...)` correctly handles the before/on/after-cutoff cases including year boundaries.
- The refresh lease (Phase 2's `acquire_lease`/`release_lease`) wraps the **entire** refresh cycle (every series/season scan, ingest, and rebuild) — not per-series — so overlapping scheduler ticks, multiple workers, or a duplicated container can never run two refresh cycles concurrently. If the lease can't be acquired, the cycle is skipped entirely (not an error — another tick or worker already holds it).
- Every refresh cycle and every successful config reload must: (1) reconcile synthetic events against the current overrides (Phase 5's `reconcile_synthetic_events`) — this must happen even on a plain scheduled refresh, not just on a config-file change, since `overrides.yaml`'s synthetic events must always reflect the currently-loaded config regardless of what triggered the rebuild; (2) call `rebuild_publication` (Phase 6); (3) render and sync the feed revision for every series (Phase 7's `render_calendar_bytes` + `sync_feed_revision`); (4) persist a summary of the rebuild (patch errors, unknown-classification UIDs, counts) so Phase 8's `/status` route — which currently has nothing to show for these, per Phase 8's explicit deferral — can finally surface them.
- Config reload: on file change, validate the **entire** new bundle (`config.yaml` + `overrides.yaml` together) before touching anything; if either fails to load/validate, the previous bundle and all previously published state remain completely untouched — this is naturally guaranteed by wrapping reconciliation + rebuild in one `store.transaction()`, since any exception during that block leaves the database exactly as it was.
- No pip: dependency management is `uv` only.

---

### Task 1: Season determination and diagnostics persistence

**Files:**
- Create: `src/motorcal/refresh.py`
- Modify: `src/motorcal/store.py`
- Test: `tests/test_refresh_seasons.py`
- Test: `tests/test_store_diagnostics.py`

**Interfaces:**
- Consumes: nothing new for `seasons_to_fetch`. `transaction` from `motorcal.store` (Phase 2) for the diagnostics CRUD.
- Produces (used by Task 2):
  - `def seasons_to_fetch(now: datetime, next_season_from: str) -> list[tuple[str, bool]]` — pure function; returns `[(season, is_current_season), ...]`.
  - `SCHEMA_VERSION` bumped to `2` in `store.py`, with a new migration adding a `refresh_diagnostics` table (singleton row like `feed_revision`/`refresh_lease`).
  - `def get_refresh_diagnostics(conn: sqlite3.Connection) -> sqlite3.Row | None`.
  - `def upsert_refresh_diagnostics(conn: sqlite3.Connection, updated_at: str, patch_errors_json: str, unknown_events_json: str, events_published: int, events_cancelled: int, events_pruned: int) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_refresh_seasons.py
from datetime import datetime, timezone

from motorcal.refresh import seasons_to_fetch


def test_only_current_season_before_the_cutoff():
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    assert seasons_to_fetch(now, "10-01") == [("2026", True)]


def test_next_season_included_on_the_cutoff_date():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert seasons_to_fetch(now, "10-01") == [("2026", True), ("2027", False)]


def test_next_season_included_after_the_cutoff():
    now = datetime(2026, 12, 31, tzinfo=timezone.utc)
    assert seasons_to_fetch(now, "10-01") == [("2026", True), ("2027", False)]


def test_only_current_season_early_in_the_year():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert seasons_to_fetch(now, "10-01") == [("2026", True)]


def test_current_season_is_always_first_in_the_list():
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    result = seasons_to_fetch(now, "10-01")
    assert result[0] == ("2026", True)
```

```python
# tests/test_store_diagnostics.py
from motorcal.store import (
    connect,
    get_refresh_diagnostics,
    init_schema,
    transaction,
    upsert_refresh_diagnostics,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_get_refresh_diagnostics_returns_none_when_never_set(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_refresh_diagnostics(conn) is None


def test_refresh_diagnostics_round_trip(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_refresh_diagnostics(
            conn, "t0", '[{"reason": "no_match"}]', '["uid-1"]', 10, 2, 1
        )

    row = get_refresh_diagnostics(conn)
    assert row["updated_at"] == "t0"
    assert row["patch_errors_json"] == '[{"reason": "no_match"}]'
    assert row["unknown_events_json"] == '["uid-1"]'
    assert row["events_published"] == 10
    assert row["events_cancelled"] == 2
    assert row["events_pruned"] == 1


def test_refresh_diagnostics_upsert_replaces_previous_values(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_refresh_diagnostics(conn, "t0", "[]", "[]", 1, 0, 0)
    with transaction(conn):
        upsert_refresh_diagnostics(conn, "t1", '[{"reason": "no_match"}]', '["u1"]', 5, 1, 0)

    row = get_refresh_diagnostics(conn)
    assert row["updated_at"] == "t1"
    assert row["events_published"] == 5


def test_init_schema_from_version_1_migrates_to_version_2(tmp_path):
    # Simulate an existing Phase-1-through-8 database (schema version 1) being
    # opened by this phase's code, proving the migration mechanism (built in
    # Phase 2 specifically for this) upgrades it without data loss.
    import sqlite3

    from motorcal.store import SCHEMA_VERSION

    db_path = tmp_path / "old.db"
    conn = connect(db_path)
    # Manually create only the version-1 tables and stamp version 1, bypassing
    # the current (already version-2-aware) init_schema.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS source_events (provider TEXT, id_event TEXT, "
        "PRIMARY KEY (provider, id_event))"
    )
    conn.execute("PRAGMA user_version=1")

    init_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert "refresh_diagnostics" in tables
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_refresh_seasons.py tests/test_store_diagnostics.py -v`
Expected: FAIL / collection error — `motorcal.refresh` does not exist yet; `get_refresh_diagnostics`/`upsert_refresh_diagnostics` don't exist yet.

- [ ] **Step 3: Bump the schema version and add the migration in `src/motorcal/store.py`**

Find `SCHEMA_VERSION = 1` near the top of the file and change it to:

```python
SCHEMA_VERSION = 2
```

Find the `_MIGRATIONS` dict (currently only has a `1: [...]` key) and add a `2: [...]` key right after the `1` entry closes (i.e. add this as a new top-level key in the same dict, not nested inside key `1`):

```python
    2: [
        """
        CREATE TABLE IF NOT EXISTS refresh_diagnostics (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            updated_at TEXT NOT NULL,
            patch_errors_json TEXT NOT NULL,
            unknown_events_json TEXT NOT NULL,
            events_published INTEGER NOT NULL,
            events_cancelled INTEGER NOT NULL,
            events_pruned INTEGER NOT NULL
        )
        """,
    ],
```

- [ ] **Step 4: Append the diagnostics CRUD to `src/motorcal/store.py`**

```python
def get_refresh_diagnostics(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM refresh_diagnostics WHERE id = 1").fetchone()


def upsert_refresh_diagnostics(
    conn: sqlite3.Connection,
    updated_at: str,
    patch_errors_json: str,
    unknown_events_json: str,
    events_published: int,
    events_cancelled: int,
    events_pruned: int,
) -> None:
    conn.execute(
        """
        INSERT INTO refresh_diagnostics
            (id, updated_at, patch_errors_json, unknown_events_json,
             events_published, events_cancelled, events_pruned)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            updated_at = excluded.updated_at,
            patch_errors_json = excluded.patch_errors_json,
            unknown_events_json = excluded.unknown_events_json,
            events_published = excluded.events_published,
            events_cancelled = excluded.events_cancelled,
            events_pruned = excluded.events_pruned
        """,
        (updated_at, patch_errors_json, unknown_events_json,
         events_published, events_cancelled, events_pruned),
    )
```

- [ ] **Step 5: Run store tests to verify they pass**

Run: `uv run pytest tests/test_store_diagnostics.py -v`
Expected: PASS, 4 passed.

- [ ] **Step 6: Write `src/motorcal/refresh.py`**

```python
"""Scheduled refresh orchestration and runtime config reload."""
from __future__ import annotations

from datetime import datetime


def seasons_to_fetch(now: datetime, next_season_from: str) -> list[tuple[str, bool]]:
    """Which {season, is_current_season} pairs to fetch on this refresh cycle.

    The current calendar-year season is always included. Once `now` has passed
    `next_season_from` (an "MM-DD" string) for this year, next year's season is
    also included, marked as NOT current -- Phase 4's ingest_snapshot uses this
    flag to decide whether an empty snapshot is suspicious.
    """
    month, day = (int(part) for part in next_season_from.split("-"))
    current_year = now.year
    seasons = [(str(current_year), True)]
    cutoff = now.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
    if now >= cutoff:
        seasons.append((str(current_year + 1), False))
    return seasons
```

- [ ] **Step 7: Run all Task 1 tests to verify they pass**

Run: `uv run pytest tests/test_refresh_seasons.py tests/test_store_diagnostics.py -v`
Expected: PASS, 9 passed (5 + 4).

- [ ] **Step 8: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-8 (260) plus this task's 9 pass — 269 passed.

- [ ] **Step 9: Commit**

```bash
git add src/motorcal/refresh.py src/motorcal/store.py tests/test_refresh_seasons.py tests/test_store_diagnostics.py
git commit -m "Add season determination, refresh_diagnostics table (schema v2), and its CRUD"
```

---

### Task 2: `run_refresh_cycle` — full refresh orchestration

**Files:**
- Modify: `src/motorcal/refresh.py`
- Test: `tests/test_refresh_cycle.py`

**Interfaces:**
- Consumes: `acquire_lease`, `release_lease`, `transaction` (Phase 2); `build_client`, `RateLimiter`, `scan_series_season` (Phase 3); `ingest_snapshot` (Phase 4); `reconcile_synthetic_events`, `rebuild_publication`, `PatchMatchError` (Phase 5/6 via `motorcal.merge`); `render_calendar_bytes`, `sync_feed_revision` (Phase 7); `upsert_refresh_diagnostics` (Task 1).
- Produces (used by Task 4's scheduler wiring):
  - `@dataclass class RefreshCycleResult` fields: `lease_acquired: bool`, `series_season_outcomes: dict[str, dict[str, str]]` (per series, per season: `"committed"` or the `IngestResult.reason` string), `rebuild_report: RebuildReport | None`.
  - `def run_refresh_cycle(conn: sqlite3.Connection, *, root_config: RootConfig, overrides: OverridesConfig, api_key: str, uid_domain: str, lease_holder: str, lease_ttl_seconds: float, now: datetime) -> RefreshCycleResult`.

- [ ] **Step 1: Write the failing tests**

These tests use `httpx.MockTransport` (the same pattern Phase 3 established) so no real network calls happen.

```python
# tests/test_refresh_cycle.py
import json
from datetime import datetime, timezone

import httpx

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    OverridesConfig,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.refresh import run_refresh_cycle
from motorcal.store import (
    connect,
    get_feed_revision,
    get_published_event,
    get_refresh_diagnostics,
    get_source_event,
    init_schema,
)
from motorcal.models import source_uid

UID_DOMAIN = "x.example.com"


def _root_config(series=None, next_season_from="10-01"):
    return RootConfig(
        server={"base_url": f"https://{UID_DOMAIN}", "uid_domain": UID_DOMAIN},
        source={"refresh_cron": "0 * * * *", "next_season_from": next_season_from,
                "rate_limit_per_min": 6000},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(
            durations=DurationDefaults(), alerts={"race": ["-1d"]}, include_sessions=["race"],
        ),
        unknown_time=UnknownTimeConfig(),
        series=series or {"wec": SeriesConfig(league_id=4413, name="WEC", max_round=1)},
    )


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _single_race_handler(request: httpx.Request) -> httpx.Response:
    round_number = int(request.url.params["r"])
    if round_number == 1:
        body = {
            "events": [
                {
                    "idEvent": "2421035", "idLeague": "4413", "strSeason": request.url.params["s"],
                    "dateEvent": "2026-04-19", "strTime": "13:00:00", "strEvent": "6 Hours of Imola",
                    "strVenue": "Imola", "strCountry": "Italy",
                }
            ]
        }
    else:
        body = {"events": None}
    return httpx.Response(200, text=json.dumps(body))


def _patched_client(monkeypatch):
    def fake_build_client():
        return httpx.Client(transport=httpx.MockTransport(_single_race_handler))

    monkeypatch.setattr("motorcal.refresh.build_client", fake_build_client)


def test_refresh_cycle_ingests_and_publishes(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = run_refresh_cycle(
        conn, root_config=_root_config(), overrides=OverridesConfig(), api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    assert result.lease_acquired is True
    assert result.series_season_outcomes["wec"]["2026"] == "committed"
    assert get_source_event(conn, "thesportsdb", "2421035") is not None
    assert get_published_event(conn, source_uid("2421035", UID_DOMAIN)) is not None
    assert result.rebuild_report.events_published == 1


def test_refresh_cycle_syncs_feed_revision_for_every_series(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    run_refresh_cycle(
        conn, root_config=_root_config(), overrides=OverridesConfig(), api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    revision = get_feed_revision(conn, "wec")
    assert revision is not None
    assert revision["revision"] != ""


def test_refresh_cycle_persists_diagnostics(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    run_refresh_cycle(
        conn, root_config=_root_config(), overrides=OverridesConfig(), api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    diagnostics = get_refresh_diagnostics(conn)
    assert diagnostics is not None
    assert diagnostics["events_published"] == 1
    assert json.loads(diagnostics["unknown_events_json"]) == []


def test_refresh_cycle_skips_entirely_when_lease_already_held(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    from motorcal.store import acquire_lease
    acquire_lease(conn, "other-worker", ttl_seconds=300, now=now.timestamp())

    result = run_refresh_cycle(
        conn, root_config=_root_config(), overrides=OverridesConfig(), api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    assert result.lease_acquired is False
    assert get_source_event(conn, "thesportsdb", "2421035") is None  # nothing was fetched


def test_refresh_cycle_reconciles_synthetic_events(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    from motorcal.config import SyntheticEventConfig
    from motorcal.models import synthetic_event_uid

    synthetic_cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24", series="imsa", summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z", duration="24h",
    )
    overrides = OverridesConfig(events=[synthetic_cfg])

    run_refresh_cycle(
        conn, root_config=_root_config(), overrides=overrides, api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    row = get_published_event(conn, synthetic_event_uid("imsa-2026-rolex-24", UID_DOMAIN))
    assert row is not None


def test_refresh_cycle_fetches_next_season_after_cutoff(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 12, 15, tzinfo=timezone.utc)  # after the "10-01" cutoff

    result = run_refresh_cycle(
        conn, root_config=_root_config(next_season_from="10-01"), overrides=OverridesConfig(),
        api_key="3", uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    assert set(result.series_season_outcomes["wec"].keys()) == {"2026", "2027"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_refresh_cycle.py -v`
Expected: FAIL / collection error — `run_refresh_cycle` does not exist yet.

- [ ] **Step 3: Append to `src/motorcal/refresh.py`**

Add these imports to the top of the file (alongside the existing `from datetime import datetime`):

```python
import json
import sqlite3
from dataclasses import dataclass

from motorcal.config import OverridesConfig, RootConfig
from motorcal.ics import render_calendar_bytes, sync_feed_revision
from motorcal.merge import PatchMatchError, RebuildReport, rebuild_publication, reconcile_synthetic_events
from motorcal.providers.thesportsdb import RateLimiter, build_client, scan_series_season
from motorcal.store import (
    acquire_lease,
    ingest_snapshot,
    release_lease,
    transaction,
    upsert_refresh_diagnostics,
)
```

Append to the end of the file:

```python
@dataclass
class RefreshCycleResult:
    lease_acquired: bool
    series_season_outcomes: dict[str, dict[str, str]]
    rebuild_report: RebuildReport | None


def _serialize_patch_error(error: PatchMatchError) -> dict:
    return {
        "reason": error.reason,
        "candidate_count": error.candidate_count,
        "id_event": error.patch.id_event,
        "match": (
            {
                "series": error.patch.match.series,
                "date": error.patch.match.date,
                "contains": error.patch.match.contains,
            }
            if error.patch.match
            else None
        ),
    }


def run_refresh_cycle(
    conn: sqlite3.Connection,
    *,
    root_config: RootConfig,
    overrides: OverridesConfig,
    api_key: str,
    uid_domain: str,
    lease_holder: str,
    lease_ttl_seconds: float,
    now: datetime,
) -> RefreshCycleResult:
    """Run one complete refresh: scan every series/season, ingest, rebuild, render.

    The lease wraps the whole cycle. If it can't be acquired, the cycle is
    skipped entirely (another tick/worker already holds it) -- this is not an
    error condition.
    """
    if not acquire_lease(conn, lease_holder, lease_ttl_seconds, now=now.timestamp()):
        return RefreshCycleResult(
            lease_acquired=False, series_season_outcomes={}, rebuild_report=None
        )

    try:
        client = build_client()
        rate_limiter = RateLimiter(rate_per_minute=root_config.source.rate_limit_per_min)
        series_season_outcomes: dict[str, dict[str, str]] = {}

        try:
            for series_key, series_config in root_config.series.items():
                series_season_outcomes[series_key] = {}
                for season, is_current in seasons_to_fetch(now, root_config.source.next_season_from):
                    snapshot = scan_series_season(
                        client, api_key, series_config.league_id, season, series_config.max_round,
                        series=series_key, include_non_championship=root_config.include_non_championship,
                        rate_limiter=rate_limiter,
                    )
                    ingest_result = ingest_snapshot(
                        conn, snapshot, provider="thesportsdb", series=series_key, season=season,
                        now=now.isoformat(), is_current_season=is_current,
                    )
                    series_season_outcomes[series_key][season] = ingest_result.reason or "committed"
        finally:
            client.close()

        with transaction(conn):
            reconcile_synthetic_events(conn, overrides.events, now.isoformat())
            report = rebuild_publication(
                conn, root_config=root_config, overrides=overrides, uid_domain=uid_domain, now=now
            )
            upsert_refresh_diagnostics(
                conn,
                now.isoformat(),
                json.dumps([_serialize_patch_error(e) for e in report.patch_errors]),
                json.dumps(report.unknown_events),
                report.events_published,
                report.events_cancelled,
                report.events_pruned,
            )

        for series_key, series_config in root_config.series.items():
            ics_bytes = render_calendar_bytes(conn, series_key, series_config)
            sync_feed_revision(conn, series_key, ics_bytes, now.isoformat())

        return RefreshCycleResult(
            lease_acquired=True,
            series_season_outcomes=series_season_outcomes,
            rebuild_report=report,
        )
    finally:
        release_lease(conn, lease_holder)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_refresh_cycle.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-8 and Phase 9 Task 1 (269) plus this task's 6 pass — 275 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/refresh.py tests/test_refresh_cycle.py
git commit -m "Add run_refresh_cycle: full scan-ingest-rebuild-render orchestration"
```

---

### Task 3: Config reload poller

**Files:**
- Modify: `src/motorcal/refresh.py`
- Test: `tests/test_refresh_reload.py`

**Interfaces:**
- Consumes: `load_config`, `load_overrides`, `ConfigError` (Phase 1); `transaction` (Phase 2); `reconcile_synthetic_events`, `rebuild_publication` (Phase 5/6).
- Produces (used by Task 4):
  - `def config_bundle_hash(config_path: Path, overrides_path: Path) -> str` — a hash over both files' actual contents (not just mtime, which can be unreliable across some filesystems/copy operations).
  - `@dataclass class ReloadResult` fields: `reloaded: bool`, `root_config: RootConfig`, `overrides: OverridesConfig`, `bundle_hash: str`, `error: str | None`.
  - `def check_and_reload_config(conn: sqlite3.Connection, config_path: Path, overrides_path: Path, previous_hash: str | None, previous_root_config: RootConfig, previous_overrides: OverridesConfig, uid_domain: str, now: datetime) -> ReloadResult` — if the bundle hash is unchanged, returns immediately with `reloaded=False` and the previous state. If changed, tries to load+validate both files; on any failure (bad YAML, schema validation, or an unexpected error during reconciliation/rebuild), returns `reloaded=False` with the previous state UNTOUCHED and `error` set to a human-readable message. Only on full success does it return `reloaded=True` with the new state — and only after `reconcile_synthetic_events` + `rebuild_publication` have both completed inside one `transaction()`, so a mid-rebuild failure can never leave a half-applied config active.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_refresh_reload.py
from datetime import datetime, timezone
from pathlib import Path

from motorcal.config import OverridesConfig, load_config
from motorcal.refresh import check_and_reload_config, config_bundle_hash
from motorcal.store import connect, get_published_event, init_schema

EXAMPLE_CONFIG = Path("config/config.example.yaml")
EXAMPLE_OVERRIDES = Path("config/overrides.example.yaml")
UID_DOMAIN = "racing.example.com"  # matches config.example.yaml's uid_domain


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_config_bundle_hash_changes_when_content_changes(tmp_path):
    config_a = tmp_path / "a.yaml"
    config_a.write_text("hello")
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("patches: []\nevents: []\n")

    hash1 = config_bundle_hash(config_a, overrides)
    config_a.write_text("goodbye")
    hash2 = config_bundle_hash(config_a, overrides)

    assert hash1 != hash2


def test_config_bundle_hash_is_stable_for_unchanged_content(tmp_path):
    config_a = tmp_path / "a.yaml"
    config_a.write_text("hello")
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("patches: []\nevents: []\n")

    assert config_bundle_hash(config_a, overrides) == config_bundle_hash(config_a, overrides)


def test_reload_skips_when_bundle_unchanged(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    overrides = OverridesConfig()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    previous_hash = config_bundle_hash(EXAMPLE_CONFIG, EXAMPLE_OVERRIDES)

    result = check_and_reload_config(
        conn, EXAMPLE_CONFIG, EXAMPLE_OVERRIDES, previous_hash, root_config, overrides,
        UID_DOMAIN, now,
    )

    assert result.reloaded is False
    assert result.error is None
    assert result.root_config is root_config  # untouched, same object


def test_reload_succeeds_on_first_load_and_rebuilds(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    overrides = OverridesConfig()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = check_and_reload_config(
        conn, EXAMPLE_CONFIG, EXAMPLE_OVERRIDES, None, root_config, overrides, UID_DOMAIN, now,
    )

    assert result.reloaded is True
    assert result.error is None
    assert result.bundle_hash == config_bundle_hash(EXAMPLE_CONFIG, EXAMPLE_OVERRIDES)


def test_reload_applies_a_new_synthetic_event_from_overrides(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = check_and_reload_config(
        conn, EXAMPLE_CONFIG, EXAMPLE_OVERRIDES, None, root_config, OverridesConfig(), UID_DOMAIN, now,
    )

    from motorcal.models import synthetic_event_uid
    row = get_published_event(conn, synthetic_event_uid("imsa-2026-rolex-24", UID_DOMAIN))
    assert row is not None
    assert row["summary"] == "Rolex 24 at Daytona"


def test_reload_leaves_previous_state_untouched_on_invalid_yaml(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    overrides = OverridesConfig()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    bad_config = tmp_path / "bad_config.yaml"
    bad_config.write_text("not: valid: yaml: [[[")

    result = check_and_reload_config(
        conn, bad_config, EXAMPLE_OVERRIDES, None, root_config, overrides, UID_DOMAIN, now,
    )

    assert result.reloaded is False
    assert result.error is not None
    assert result.root_config is root_config  # previous config still active


def test_reload_leaves_previous_state_untouched_on_schema_validation_failure(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    overrides = OverridesConfig()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    bad_config = tmp_path / "bad_config.yaml"
    bad_config.write_text(
        EXAMPLE_CONFIG.read_text().replace("league_id: 4370", "league_id: not_a_number")
    )

    result = check_and_reload_config(
        conn, bad_config, EXAMPLE_OVERRIDES, None, root_config, overrides, UID_DOMAIN, now,
    )

    assert result.reloaded is False
    assert result.error is not None
    assert result.root_config is root_config
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_refresh_reload.py -v`
Expected: FAIL / collection error — `config_bundle_hash`, `check_and_reload_config` don't exist yet.

- [ ] **Step 3: Append to `src/motorcal/refresh.py`**

Add these imports (extend the existing `from motorcal.config import ...` line rather than duplicating it; add `hashlib` and `Path`):

```python
import hashlib
from pathlib import Path

from motorcal.config import ConfigError, load_config, load_overrides
```

Append to the end of the file:

```python
def config_bundle_hash(config_path: Path, overrides_path: Path) -> str:
    """A content-based hash of both config files, used to detect a real change."""
    hasher = hashlib.sha256()
    for path in (config_path, overrides_path):
        hasher.update(Path(path).read_bytes())
    return hasher.hexdigest()


@dataclass
class ReloadResult:
    reloaded: bool
    root_config: RootConfig
    overrides: OverridesConfig
    bundle_hash: str | None
    error: str | None


def check_and_reload_config(
    conn: sqlite3.Connection,
    config_path: Path,
    overrides_path: Path,
    previous_hash: str | None,
    previous_root_config: RootConfig,
    previous_overrides: OverridesConfig,
    uid_domain: str,
    now: datetime,
) -> ReloadResult:
    """Detect a config-file change, validate the whole bundle, and rebuild atomically.

    On any failure the previous config/overrides/published state remain
    completely untouched -- validation happens before any database write, and
    reconciliation + rebuild happen inside one transaction so a mid-way
    failure can never leave a half-applied config active.
    """
    new_hash = config_bundle_hash(config_path, overrides_path)
    if new_hash == previous_hash:
        return ReloadResult(
            reloaded=False, root_config=previous_root_config, overrides=previous_overrides,
            bundle_hash=previous_hash, error=None,
        )

    try:
        new_root_config = load_config(config_path)
        new_overrides = load_overrides(overrides_path)
    except ConfigError as exc:
        return ReloadResult(
            reloaded=False, root_config=previous_root_config, overrides=previous_overrides,
            bundle_hash=previous_hash, error=str(exc),
        )

    try:
        with transaction(conn):
            reconcile_synthetic_events(conn, new_overrides.events, now.isoformat())
            rebuild_publication(
                conn, root_config=new_root_config, overrides=new_overrides,
                uid_domain=uid_domain, now=now,
            )
    except Exception as exc:  # noqa: BLE001 -- any rebuild failure must roll back and be reported
        return ReloadResult(
            reloaded=False, root_config=previous_root_config, overrides=previous_overrides,
            bundle_hash=previous_hash, error=str(exc),
        )

    return ReloadResult(
        reloaded=True, root_config=new_root_config, overrides=new_overrides,
        bundle_hash=new_hash, error=None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_refresh_reload.py -v`
Expected: PASS, 7 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-8 and Phase 9 Tasks 1-2 (275) plus this task's 7 pass — 282 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/refresh.py tests/test_refresh_reload.py
git commit -m "Add config-file-change detection and atomic hot reload"
```

---

### Task 4: Scheduler wiring, `/status` diagnostics, and `motorcal serve`

**Files:**
- Modify: `src/motorcal/refresh.py`
- Modify: `src/motorcal/web.py`
- Modify: `src/motorcal/cli.py`
- Test: `tests/test_refresh_scheduler.py`
- Test: `tests/test_web_status_diagnostics.py`

**Interfaces:**
- Consumes: `run_refresh_cycle` (Task 2), `check_and_reload_config` (Task 3); `CronTrigger` from `apscheduler.triggers.cron`; `BackgroundScheduler` from `apscheduler.schedulers.background`.
- Produces:
  - `def build_scheduler(refresh_job, refresh_cron: str, reload_job, reload_interval_seconds: float = 30) -> BackgroundScheduler` — a configured (but not yet started) scheduler with the refresh cron job and a periodic config-reload check job attached. Kept as plain callables (`refresh_job`, `reload_job` take no arguments) so this function stays testable without needing a real database/config — the caller (`cli.py`) is responsible for building the actual closures around `run_refresh_cycle`/`check_and_reload_config` with real state.
  - `src/motorcal/web.py`'s `/c/{token}/status` route gains `patch_errors` and `unknown_events` fields in its JSON body, populated from `get_refresh_diagnostics` (Task 1) when a row exists (empty lists otherwise — e.g. before the first refresh cycle has ever run).
  - `src/motorcal/cli.py` gains a `serve` subcommand: `motorcal serve --db PATH --config PATH --overrides PATH` — loads the config bundle once at startup (exits nonzero on failure, per the spec's "On startup, invalid configuration exits nonzero"), builds the FastAPI app (Phase 8's `create_app`) and the scheduler (this task's `build_scheduler`) wired to real refresh/reload closures over that state, starts the scheduler, then runs the app with `uvicorn.run(...)`. `MOTORCAL_TOKENS` and `THESPORTSDB_API_KEY` are read from `os.environ` here — this is the one place in the whole codebase allowed to do that, per Phase 8's explicit design note that `web.py` itself stays environment-agnostic.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_refresh_scheduler.py
from motorcal.refresh import build_scheduler


def test_build_scheduler_registers_the_refresh_cron_job():
    calls = {"refresh": 0, "reload": 0}

    def refresh_job():
        calls["refresh"] += 1

    def reload_job():
        calls["reload"] += 1

    scheduler = build_scheduler(refresh_job, "17 */6 * * *", reload_job, reload_interval_seconds=30)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 2
    job_funcs = {job.func for job in jobs}
    assert refresh_job in job_funcs
    assert reload_job in job_funcs


def test_build_scheduler_does_not_start_automatically():
    scheduler = build_scheduler(lambda: None, "0 * * * *", lambda: None)
    assert scheduler.running is False
```

```python
# tests/test_web_status_diagnostics.py
import json

from fastapi.testclient import TestClient

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.store import connect, init_schema, transaction, upsert_refresh_diagnostics
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


def test_status_includes_empty_diagnostics_before_any_refresh(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/c/t/status")

    body = response.json()
    assert body["patch_errors"] == []
    assert body["unknown_events"] == []


def test_status_surfaces_persisted_diagnostics(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    with transaction(conn):
        upsert_refresh_diagnostics(
            conn, "t0", json.dumps([{"reason": "no_match", "id_event": "999"}]),
            json.dumps(["thesportsdb-1@x.example.com"]), 5, 1, 0,
        )
    conn.close()

    app = create_app(tmp_path / "test.db", _root_config(), tokens=["t"])
    response = TestClient(app).get("/c/t/status")

    body = response.json()
    assert body["patch_errors"] == [{"reason": "no_match", "id_event": "999"}]
    assert body["unknown_events"] == ["thesportsdb-1@x.example.com"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_refresh_scheduler.py tests/test_web_status_diagnostics.py -v`
Expected: FAIL / collection error — `build_scheduler` doesn't exist yet; `/status` doesn't include `patch_errors`/`unknown_events` yet.

- [ ] **Step 3: Append to `src/motorcal/refresh.py`**

Add this import at the top:

```python
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
```

Append to the end of the file:

```python
def build_scheduler(
    refresh_job, refresh_cron: str, reload_job, reload_interval_seconds: float = 30
) -> BackgroundScheduler:
    """Build (but do not start) a scheduler running refresh_job on refresh_cron
    and reload_job on a fixed interval."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(refresh_job, CronTrigger.from_crontab(refresh_cron))
    scheduler.add_job(reload_job, IntervalTrigger(seconds=reload_interval_seconds))
    return scheduler
```

- [ ] **Step 4: Run scheduler tests to verify they pass**

Run: `uv run pytest tests/test_refresh_scheduler.py -v`
Expected: PASS, 2 passed.

- [ ] **Step 5: Modify `src/motorcal/web.py`'s `/c/{token}/status` route**

Read the existing route first (it currently returns a dict built from `series_status` plus top-level `ready`/`healthy`). Add `get_refresh_diagnostics` to the existing `from motorcal.store import (...)` block (do not duplicate the import line), then extend the returned body:

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

            diagnostics_row = get_refresh_diagnostics(conn)
            if diagnostics_row is None:
                patch_errors, unknown_events = [], []
            else:
                patch_errors = json.loads(diagnostics_row["patch_errors_json"])
                unknown_events = json.loads(diagnostics_row["unknown_events_json"])
        finally:
            conn.close()

        body = {
            "ready": all(v["ready"] for v in series_status.values()),
            "healthy": all(not v["stale"] for v in series_status.values()),
            "series": series_status,
            "patch_errors": patch_errors,
            "unknown_events": unknown_events,
        }
        return body
```

Add `import json` to the top of `web.py` if it is not already present (check first).

- [ ] **Step 6: Run web diagnostics tests to verify they pass**

Run: `uv run pytest tests/test_web_status_diagnostics.py -v`
Expected: PASS, 2 passed.

- [ ] **Step 7: Add the `serve` subcommand to `src/motorcal/cli.py`**

Read the existing `cli.py` in full first — it has `_cmd_init_db`, `_cmd_backup`, a `_build_parser` with a shared `db_parent`, and `main`. Add these imports to the top (alongside the existing ones):

```python
import os
from datetime import datetime, timezone

import uvicorn

from motorcal.config import ConfigError, load_config, load_overrides
from motorcal.refresh import build_scheduler, check_and_reload_config, run_refresh_cycle
from motorcal.web import create_app
```

Add this function:

```python
def _cmd_serve(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    config_path = Path(args.config)
    overrides_path = Path(args.overrides)

    try:
        root_config = load_config(config_path)
        overrides = load_overrides(overrides_path)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1

    api_key = os.environ.get("THESPORTSDB_API_KEY")
    tokens_env = os.environ.get("MOTORCAL_TOKENS", "")
    tokens = [t for t in tokens_env.split(",") if t]
    if not api_key or not tokens:
        print(
            "THESPORTSDB_API_KEY and MOTORCAL_TOKENS must both be set", file=sys.stderr
        )
        return 1

    conn = connect(db_path)
    init_schema(conn)
    conn.close()

    state = {
        "root_config": root_config,
        "overrides": overrides,
        "bundle_hash": None,
    }

    def refresh_job():
        conn = connect(db_path)
        try:
            run_refresh_cycle(
                conn, root_config=state["root_config"], overrides=state["overrides"],
                api_key=api_key, uid_domain=state["root_config"].server.uid_domain,
                lease_holder=f"scheduler-{os.getpid()}", lease_ttl_seconds=1800,
                now=datetime.now(timezone.utc),
            )
        finally:
            conn.close()

    def reload_job():
        conn = connect(db_path)
        try:
            result = check_and_reload_config(
                conn, config_path, overrides_path, state["bundle_hash"],
                state["root_config"], state["overrides"],
                state["root_config"].server.uid_domain, datetime.now(timezone.utc),
            )
            if result.reloaded:
                state["root_config"] = result.root_config
                state["overrides"] = result.overrides
            state["bundle_hash"] = result.bundle_hash
        finally:
            conn.close()

    scheduler = build_scheduler(refresh_job, root_config.source.refresh_cron, reload_job)
    scheduler.start()

    app = create_app(db_path, root_config, tokens)
    uvicorn.run(app, host="0.0.0.0", port=8000)
    return 0
```

Add this to `_build_parser`, after the `backup_parser` block:

```python
    serve_parser = subparsers.add_parser(
        "serve", parents=[db_parent], help="Run the scheduler and HTTP server"
    )
    serve_parser.add_argument("--config", required=True, help="Path to config.yaml")
    serve_parser.add_argument("--overrides", required=True, help="Path to overrides.yaml")
    serve_parser.set_defaults(func=_cmd_serve)
```

- [ ] **Step 8: Run the entire test suite**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-8 and Phase 9 Tasks 1-4 pass — 286 passed total (282 + 2 + 2 + 0 new for `_cmd_serve` itself, which is not unit-tested at this level — see Self-Review Notes).

- [ ] **Step 9: Commit**

```bash
git add src/motorcal/refresh.py src/motorcal/web.py src/motorcal/cli.py tests/test_refresh_scheduler.py tests/test_web_status_diagnostics.py
git commit -m "Add scheduler wiring, /status diagnostics, and motorcal serve"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: current+future season fetching per `next_season_from` (Season and retention policy section); the refresh lease wrapping the entire cycle (Rate limiting and concurrency section); config reload validating the whole bundle atomically with the previous state surviving any failure (Configuration and reload behavior section); `/status` finally surfacing patch errors and unknown-classification events, closing the gap Phase 8 explicitly deferred.
- Explicitly out of scope for this phase (later phases own them): `_cmd_serve` itself is not unit-tested end-to-end (starting a real scheduler + real uvicorn server isn't practical in a fast unit suite) — Phase 10's Compose-based live verification (the spec's verification step 16: "bring up Compose, subscribe through the tunnel, and verify updates, cancellations, and alarms in the intended calendar client") is what actually exercises this path; Docker/Compose/tunnel wiring and the `republish --force-version` recovery operation are Phase 10's.
- Type consistency check: `run_refresh_cycle` and `check_and_reload_config` both take a raw `sqlite3.Connection` and a `datetime` `now`, matching every other phase's top-level orchestration function (`ingest_snapshot`, `rebuild_publication`). `build_scheduler` takes plain zero-argument callables rather than the real orchestration functions directly, specifically so it stays testable without a database — `cli.py`'s `_cmd_serve` is the one place that closes over real state to build those callables, mirroring the "keep `web.py` environment-agnostic, push env/IO to `cli.py`" pattern Phase 8 established for `MOTORCAL_TOKENS`.
