# Motorsports Calendar — Phase 2: SQLite Schema, Transactions, Lease, Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/motorcal/store.py`, the SQLite persistence layer: schema + migrations, WAL mode with startup integrity checks, an atomic transaction primitive proven to roll back cleanly across multiple tables, an atomic cross-process refresh lease, and an online backup function wired into the CLI.

**Architecture:** A single `store.py` module wrapping the stdlib `sqlite3` module. No ORM. Connections are opened in autocommit mode (`isolation_level=None`) so every write happens inside an explicit `BEGIN IMMEDIATE ... COMMIT/ROLLBACK` block from the `transaction()` context manager — this is what lets phases 3-6 later "replace the source snapshot and rebuild its affected published events in the same transaction" and what makes the lease's check-then-act sequence race-free across processes. This phase does not implement the provider, classifier, merge, or ICS logic — it only proves the storage primitives those phases will build on, using minimal generic CRUD against the real schema.

**Tech Stack:** Python 3.13 stdlib `sqlite3` (no new dependency). `pytest` with `tmp_path` for isolated per-test database files.

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous.
- Phase 1 (already complete, commits `b338146..ac5bba5`) produced `src/motorcal/models.py` (canonical models) and `src/motorcal/config.py` (config/overrides schema + `parse_duration`/`parse_alarm_offset`). This phase does not modify either file.
- SQLite must run in WAL mode. Run `PRAGMA integrity_check` and treat anything other than the single row `"ok"` as corruption.
- Corruption must never trigger automatic deletion or replacement of the database — readiness must fail instead (the actual `/readyz` wiring is Phase 8; this phase only needs to expose a function that detects corruption so Phase 8 can call it).
- The database contains (per spec): normalized source snapshots/events, synthetic event state, published canonical events, event fingerprint/sequence/last-modified, cancellation/tombstone state, refresh history and completeness, and the refresh lease. This phase creates all of these tables now (so later phases only add columns/queries, never new tables) but only implements CRUD for `source_events` and `published_events` — enough to prove the transaction and lease primitives end-to-end. `synthetic_events`, `refresh_history`, `source_snapshot_meta`, and `feed_revision` get their read/write helpers in later phases (5, 3, 3, 7 respectively) — this phase only creates their table definitions.
- Refreshing is protected by an atomic SQLite lease with an expiry, preventing overlapping refreshes across scheduler ticks, multiple web workers, or duplicated containers. Losing the lease must prevent commit — a caller that calls `transaction()` while not holding a live lease should be rejected by that caller's own logic in a later phase; this phase's job is only to make `acquire_lease`/`release_lease`/`current_lease_holder` correct and race-free, not to wire lease-checking into refresh logic (that's Phase 9).
- Timestamps stored in the database are ISO 8601 strings in UTC (e.g. `"2026-07-29T12:00:00+00:00"`), always timezone-aware when parsed back.
- Periodic online backups must go to a separate file/volume without disrupting a live WAL writer — use SQLite's `Connection.backup()` API, not a file copy.
- No pip: dependency management is `uv` only.

---

### Task 1: Schema, migrations, and connection helpers

**Files:**
- Create: `src/motorcal/store.py`
- Test: `tests/test_store_schema.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (used by every later task and phase):
  - `SCHEMA_VERSION: int = 1`
  - `def connect(db_path: Path) -> sqlite3.Connection` — opens the DB in autocommit mode (`isolation_level=None`), sets `row_factory = sqlite3.Row`, and enables `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON`.
  - `def init_schema(conn: sqlite3.Connection) -> None` — idempotent; creates all tables listed below if `PRAGMA user_version` is below `SCHEMA_VERSION`, then sets `PRAGMA user_version` to `SCHEMA_VERSION`.
  - `def check_integrity(conn: sqlite3.Connection) -> bool` — runs `PRAGMA integrity_check`, returns `True` only if the single result row is exactly `"ok"`.
  - Tables created by `init_schema`: `source_events`, `source_snapshot_meta`, `refresh_history`, `synthetic_events`, `published_events`, `refresh_lease`, `feed_revision` (exact columns below).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_schema.py
import sqlite3
from pathlib import Path

import pytest

from motorcal.store import SCHEMA_VERSION, check_integrity, connect, init_schema


def _fresh_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_connect_enables_wal_mode(tmp_path):
    conn = _fresh_conn(tmp_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_init_schema_sets_user_version(tmp_path):
    conn = _fresh_conn(tmp_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_init_schema_is_idempotent(tmp_path):
    conn = _fresh_conn(tmp_path)
    init_schema(conn)  # calling twice must not raise or duplicate tables
    init_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    assert tables == {
        "source_events",
        "source_snapshot_meta",
        "refresh_history",
        "synthetic_events",
        "published_events",
        "refresh_lease",
        "feed_revision",
    }


def test_source_events_table_columns(tmp_path):
    conn = _fresh_conn(tmp_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(source_events)").fetchall()}
    assert columns == {
        "provider",
        "id_event",
        "series",
        "season",
        "round",
        "name",
        "date",
        "time",
        "venue",
        "country",
        "raw_json",
        "first_seen_at",
        "last_seen_at",
        "disappeared_at",
    }


def test_source_events_primary_key_is_provider_and_id_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    conn.execute(
        "INSERT INTO source_events "
        "(provider, id_event, series, season, round, name, date, time, venue, country, "
        " raw_json, first_seen_at, last_seen_at) "
        "VALUES ('thesportsdb', '2421035', 'wec', '2026', 1, '6 Hours of Imola', "
        " '2026-04-19', '00:00:00', 'Imola', 'Italy', '{}', "
        " '2026-07-29T00:00:00+00:00', '2026-07-29T00:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_events "
            "(provider, id_event, series, season, round, name, date, time, venue, country, "
            " raw_json, first_seen_at, last_seen_at) "
            "VALUES ('thesportsdb', '2421035', 'wec', '2026', 1, 'Duplicate', "
            " '2026-04-19', '00:00:00', 'Imola', 'Italy', '{}', "
            " '2026-07-29T00:00:00+00:00', '2026-07-29T00:00:00+00:00')"
        )


def test_published_events_table_columns(tmp_path):
    conn = _fresh_conn(tmp_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(published_events)").fetchall()}
    assert columns == {
        "uid",
        "series",
        "session_type",
        "summary",
        "start",
        "all_day_date",
        "time_confirmed",
        "duration_seconds",
        "location",
        "description",
        "status",
        "sequence",
        "dtstamp",
        "last_modified",
        "fingerprint",
        "alarms_json",
        "source_provider",
        "source_id_event",
        "synthetic_uid",
        "cancelled_at",
        "retain_until",
    }


def test_refresh_lease_table_columns(tmp_path):
    conn = _fresh_conn(tmp_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(refresh_lease)").fetchall()}
    assert columns == {"id", "holder", "acquired_at", "expires_at"}


def test_check_integrity_ok_on_fresh_db(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert check_integrity(conn) is True


def test_check_integrity_detects_corruption(tmp_path):
    db_path = tmp_path / "corrupt.db"
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    # Corrupt the file on disk directly (simulate real corruption).
    with open(db_path, "r+b") as f:
        f.seek(100)
        f.write(b"\xff" * 200)
    conn2 = sqlite3.connect(db_path)
    assert check_integrity(conn2) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_schema.py -v`
Expected: FAIL / collection error — `motorcal.store` does not exist yet.

- [ ] **Step 3: Write `src/motorcal/store.py`**

```python
"""SQLite persistence: schema, migrations, transactions, lease, and backup."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS source_events (
        provider TEXT NOT NULL,
        id_event TEXT NOT NULL,
        series TEXT NOT NULL,
        season TEXT NOT NULL,
        round INTEGER NOT NULL,
        name TEXT NOT NULL,
        date TEXT NOT NULL,
        time TEXT,
        venue TEXT,
        country TEXT,
        raw_json TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        disappeared_at TEXT,
        PRIMARY KEY (provider, id_event)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_snapshot_meta (
        provider TEXT NOT NULL,
        series TEXT NOT NULL,
        season TEXT NOT NULL,
        last_complete_at TEXT NOT NULL,
        last_event_count INTEGER NOT NULL,
        PRIMARY KEY (provider, series, season)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS refresh_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        series TEXT NOT NULL,
        season TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT NOT NULL,
        detail TEXT,
        event_count INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS synthetic_events (
        uid TEXT PRIMARY KEY,
        series TEXT NOT NULL,
        summary TEXT NOT NULL,
        start TEXT,
        date TEXT,
        duration_seconds INTEGER,
        location TEXT,
        status TEXT NOT NULL DEFAULT 'CONFIRMED',
        note TEXT,
        alarms_json TEXT NOT NULL DEFAULT '[]',
        present_in_config INTEGER NOT NULL DEFAULT 1,
        cancelled_at TEXT,
        purged INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS published_events (
        uid TEXT PRIMARY KEY,
        series TEXT NOT NULL,
        session_type TEXT NOT NULL,
        summary TEXT NOT NULL,
        start TEXT,
        all_day_date TEXT,
        time_confirmed INTEGER NOT NULL,
        duration_seconds INTEGER,
        location TEXT,
        description TEXT NOT NULL,
        status TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        dtstamp TEXT NOT NULL,
        last_modified TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        alarms_json TEXT NOT NULL DEFAULT '[]',
        source_provider TEXT,
        source_id_event TEXT,
        synthetic_uid TEXT,
        cancelled_at TEXT,
        retain_until TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS refresh_lease (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        holder TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feed_revision (
        series TEXT PRIMARY KEY,
        revision TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a database connection in autocommit mode with WAL and foreign keys enabled."""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables if missing and stamp the schema version. Idempotent."""
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version >= SCHEMA_VERSION:
        return
    with transaction(conn):
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def check_integrity(conn: sqlite3.Connection) -> bool:
    """Run PRAGMA integrity_check; True only if SQLite reports a clean single 'ok' row.

    Severe corruption can make the PRAGMA itself raise sqlite3.DatabaseError
    (e.g. "database disk image is malformed" or "file is not a database")
    instead of returning a non-'ok' row — both cases mean "not intact".
    """
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError:
        return False
    return len(rows) == 1 and rows[0][0] == "ok"


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Wrap a block of writes in an immediate, all-or-nothing SQLite transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_schema.py -v`
Expected: PASS, 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/motorcal/store.py tests/test_store_schema.py
git commit -m "Add SQLite schema, migrations, connection, and integrity check helpers"
```

---

### Task 2: Atomic transaction primitive proven across tables

**Files:**
- Modify: `src/motorcal/store.py` (add source_events and published_events CRUD)
- Test: `tests/test_store_transactions.py`

**Interfaces:**
- Consumes: `connect`, `init_schema`, `transaction` from Task 1.
- Produces (used by phases 3, 4, 6):
  - `def upsert_source_event(conn, *, provider: str, id_event: str, series: str, season: str, round: int, name: str, date: str, time: str | None, venue: str | None, country: str | None, raw_json: str, seen_at: str) -> None` — inserts a new row or updates `name/date/time/venue/country/raw_json/last_seen_at` (and clears `disappeared_at`) on conflict of `(provider, id_event)`. Sets `first_seen_at = seen_at` only on first insert.
  - `def get_source_event(conn, provider: str, id_event: str) -> sqlite3.Row | None`.
  - `def upsert_published_event(conn, *, uid: str, series: str, session_type: str, summary: str, start: str | None, all_day_date: str | None, time_confirmed: bool, duration_seconds: int | None, location: str | None, description: str, status: str, sequence: int, dtstamp: str, last_modified: str, fingerprint: str, alarms_json: str, source_provider: str | None, source_id_event: str | None, synthetic_uid: str | None, cancelled_at: str | None, retain_until: str | None) -> None` — insert-or-replace by `uid`.
  - `def get_published_event(conn, uid: str) -> sqlite3.Row | None`.
  - `class IntentionalRollback(Exception)` — a test-only marker exception used to prove transactions roll back (defined in `store.py` so tests can import it; production code never raises it).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_transactions.py
import pytest

from motorcal.store import (
    IntentionalRollback,
    connect,
    get_published_event,
    get_source_event,
    init_schema,
    transaction,
    upsert_published_event,
    upsert_source_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert_sample_source_event(conn):
    upsert_source_event(
        conn,
        provider="thesportsdb",
        id_event="2421035",
        series="wec",
        season="2026",
        round=1,
        name="6 Hours of Imola",
        date="2026-04-19",
        time="00:00:00",
        venue="Imola",
        country="Italy",
        raw_json="{}",
        seen_at="2026-07-29T00:00:00+00:00",
    )


def _insert_sample_published_event(conn):
    upsert_published_event(
        conn,
        uid="thesportsdb-2421035@racing.example.com",
        series="wec",
        session_type="race",
        summary="6 Hours of Imola",
        start=None,
        all_day_date="2026-04-19",
        time_confirmed=False,
        duration_seconds=None,
        location="Imola, Italy",
        description="Round 1 of WEC",
        status="CONFIRMED",
        sequence=1,
        dtstamp="2026-07-29T00:00:00+00:00",
        last_modified="2026-07-29T00:00:00+00:00",
        fingerprint="abc123",
        alarms_json="[]",
        source_provider="thesportsdb",
        source_id_event="2421035",
        synthetic_uid=None,
        cancelled_at=None,
        retain_until=None,
    )


def test_upsert_and_get_source_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_sample_source_event(conn)
    row = get_source_event(conn, "thesportsdb", "2421035")
    assert row["name"] == "6 Hours of Imola"
    assert row["first_seen_at"] == "2026-07-29T00:00:00+00:00"
    assert row["disappeared_at"] is None


def test_upsert_source_event_updates_without_resetting_first_seen(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_sample_source_event(conn)
    with transaction(conn):
        upsert_source_event(
            conn,
            provider="thesportsdb",
            id_event="2421035",
            series="wec",
            season="2026",
            round=1,
            name="6 Hours of Imola (Updated)",
            date="2026-04-19",
            time="13:00:00",
            venue="Imola",
            country="Italy",
            raw_json="{}",
            seen_at="2026-08-01T00:00:00+00:00",
        )
    row = get_source_event(conn, "thesportsdb", "2421035")
    assert row["name"] == "6 Hours of Imola (Updated)"
    assert row["time"] == "13:00:00"
    assert row["first_seen_at"] == "2026-07-29T00:00:00+00:00"  # unchanged
    assert row["last_seen_at"] == "2026-08-01T00:00:00+00:00"  # updated


def test_upsert_and_get_published_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_sample_published_event(conn)
    row = get_published_event(conn, "thesportsdb-2421035@racing.example.com")
    assert row["summary"] == "6 Hours of Imola"
    assert row["status"] == "CONFIRMED"
    assert row["sequence"] == 1


def test_get_source_event_returns_none_when_missing(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_source_event(conn, "thesportsdb", "does-not-exist") is None


def test_get_published_event_returns_none_when_missing(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_published_event(conn, "does-not-exist@racing.example.com") is None


def test_transaction_rolls_back_both_tables_on_exception(tmp_path):
    conn = _fresh_conn(tmp_path)
    with pytest.raises(IntentionalRollback):
        with transaction(conn):
            _insert_sample_source_event(conn)
            _insert_sample_published_event(conn)
            raise IntentionalRollback("simulated failure mid-transaction")
    # Neither write should have survived — this is the "one atomic transaction"
    # guarantee the spec requires for snapshot replace + publication rebuild.
    assert get_source_event(conn, "thesportsdb", "2421035") is None
    assert get_published_event(conn, "thesportsdb-2421035@racing.example.com") is None


def test_transaction_commits_both_tables_together_on_success(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_sample_source_event(conn)
        _insert_sample_published_event(conn)
    assert get_source_event(conn, "thesportsdb", "2421035") is not None
    assert get_published_event(conn, "thesportsdb-2421035@racing.example.com") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_transactions.py -v`
Expected: FAIL / collection error — `upsert_source_event`, `get_source_event`, `upsert_published_event`, `get_published_event`, `IntentionalRollback` do not exist yet.

- [ ] **Step 3: Append the CRUD helpers and exception to `src/motorcal/store.py`**

Append to the end of `src/motorcal/store.py`:

```python
class IntentionalRollback(Exception):
    """Test-only exception used to prove a transaction rolls back cleanly."""


def upsert_source_event(
    conn: sqlite3.Connection,
    *,
    provider: str,
    id_event: str,
    series: str,
    season: str,
    round: int,
    name: str,
    date: str,
    time: str | None,
    venue: str | None,
    country: str | None,
    raw_json: str,
    seen_at: str,
) -> None:
    """Insert a new source event or update its mutable fields, preserving first_seen_at."""
    conn.execute(
        """
        INSERT INTO source_events
            (provider, id_event, series, season, round, name, date, time, venue, country,
             raw_json, first_seen_at, last_seen_at, disappeared_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT (provider, id_event) DO UPDATE SET
            series = excluded.series,
            season = excluded.season,
            round = excluded.round,
            name = excluded.name,
            date = excluded.date,
            time = excluded.time,
            venue = excluded.venue,
            country = excluded.country,
            raw_json = excluded.raw_json,
            last_seen_at = excluded.last_seen_at,
            disappeared_at = NULL
        """,
        (
            provider,
            id_event,
            series,
            season,
            round,
            name,
            date,
            time,
            venue,
            country,
            raw_json,
            seen_at,
            seen_at,
        ),
    )


def get_source_event(conn: sqlite3.Connection, provider: str, id_event: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM source_events WHERE provider = ? AND id_event = ?",
        (provider, id_event),
    ).fetchone()


def upsert_published_event(
    conn: sqlite3.Connection,
    *,
    uid: str,
    series: str,
    session_type: str,
    summary: str,
    start: str | None,
    all_day_date: str | None,
    time_confirmed: bool,
    duration_seconds: int | None,
    location: str | None,
    description: str,
    status: str,
    sequence: int,
    dtstamp: str,
    last_modified: str,
    fingerprint: str,
    alarms_json: str,
    source_provider: str | None,
    source_id_event: str | None,
    synthetic_uid: str | None,
    cancelled_at: str | None,
    retain_until: str | None,
) -> None:
    """Insert or fully replace a published event by its stable UID."""
    conn.execute(
        """
        INSERT INTO published_events
            (uid, series, session_type, summary, start, all_day_date, time_confirmed,
             duration_seconds, location, description, status, sequence, dtstamp,
             last_modified, fingerprint, alarms_json, source_provider, source_id_event,
             synthetic_uid, cancelled_at, retain_until)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (uid) DO UPDATE SET
            series = excluded.series,
            session_type = excluded.session_type,
            summary = excluded.summary,
            start = excluded.start,
            all_day_date = excluded.all_day_date,
            time_confirmed = excluded.time_confirmed,
            duration_seconds = excluded.duration_seconds,
            location = excluded.location,
            description = excluded.description,
            status = excluded.status,
            sequence = excluded.sequence,
            dtstamp = excluded.dtstamp,
            last_modified = excluded.last_modified,
            fingerprint = excluded.fingerprint,
            alarms_json = excluded.alarms_json,
            source_provider = excluded.source_provider,
            source_id_event = excluded.source_id_event,
            synthetic_uid = excluded.synthetic_uid,
            cancelled_at = excluded.cancelled_at,
            retain_until = excluded.retain_until
        """,
        (
            uid,
            series,
            session_type,
            summary,
            start,
            all_day_date,
            int(time_confirmed),
            duration_seconds,
            location,
            description,
            status,
            sequence,
            dtstamp,
            last_modified,
            fingerprint,
            alarms_json,
            source_provider,
            source_id_event,
            synthetic_uid,
            cancelled_at,
            retain_until,
        ),
    )


def get_published_event(conn: sqlite3.Connection, uid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM published_events WHERE uid = ?", (uid,)
    ).fetchone()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_transactions.py -v`
Expected: PASS, 7 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all Phase 1 tests (49) plus Task 1 (9) plus Task 2 (7) pass — 65 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/store.py tests/test_store_transactions.py
git commit -m "Add source_event/published_event CRUD and prove atomic transaction rollback"
```

---

### Task 3: Atomic refresh lease

**Files:**
- Modify: `src/motorcal/store.py`
- Test: `tests/test_store_lease.py`

**Interfaces:**
- Consumes: `connect`, `init_schema`, `transaction` from Task 1.
- Produces (used by Phase 9's scheduler):
  - `def acquire_lease(conn: sqlite3.Connection, holder: str, ttl_seconds: float, *, now: float | None = None) -> bool` — returns `True` and (re)claims the lease if no live lease exists or the existing one has expired; returns `False` without changing the row if a different, still-live lease exists. Uses `time.time()` when `now` is omitted.
  - `def release_lease(conn: sqlite3.Connection, holder: str) -> None` — deletes the lease row only if it is currently held by `holder`; a no-op otherwise (never releases someone else's lease).
  - `def current_lease_holder(conn: sqlite3.Connection, *, now: float | None = None) -> str | None` — returns the current live holder, or `None` if no row exists or the row has expired.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_lease.py
import time

from motorcal.store import (
    acquire_lease,
    connect,
    current_lease_holder,
    init_schema,
    release_lease,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_acquire_lease_succeeds_when_no_lease_exists(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0) is True
    assert current_lease_holder(conn, now=1000.0) == "worker-a"


def test_second_acquire_fails_while_first_lease_is_live(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0) is True
    assert acquire_lease(conn, "worker-b", ttl_seconds=60, now=1010.0) is False
    assert current_lease_holder(conn, now=1010.0) == "worker-a"


def test_acquire_succeeds_after_expiry(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0) is True
    # worker-a's lease expires at 1060.0; worker-b tries at 1100.0.
    assert acquire_lease(conn, "worker-b", ttl_seconds=60, now=1100.0) is True
    assert current_lease_holder(conn, now=1100.0) == "worker-b"


def test_release_lease_removes_own_lease(tmp_path):
    conn = _fresh_conn(tmp_path)
    acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0)
    release_lease(conn, "worker-a")
    assert current_lease_holder(conn, now=1000.0) is None


def test_release_lease_does_not_remove_someone_elses_lease(tmp_path):
    conn = _fresh_conn(tmp_path)
    acquire_lease(conn, "worker-a", ttl_seconds=60, now=1000.0)
    release_lease(conn, "worker-b")  # not the holder — must be a no-op
    assert current_lease_holder(conn, now=1000.0) == "worker-a"


def test_current_lease_holder_is_none_when_never_acquired(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert current_lease_holder(conn, now=1000.0) is None


def test_two_connections_racing_for_the_lease_only_one_wins(tmp_path):
    # Simulates two separate processes/workers each holding their own connection
    # to the same database file, both trying to acquire the lease at the same moment.
    db_path = tmp_path / "shared.db"
    conn_a = connect(db_path)
    init_schema(conn_a)
    conn_b = connect(db_path)

    now = time.time()
    result_a = acquire_lease(conn_a, "worker-a", ttl_seconds=60, now=now)
    result_b = acquire_lease(conn_b, "worker-b", ttl_seconds=60, now=now)

    assert result_a is True
    assert result_b is False
    assert current_lease_holder(conn_a, now=now) == "worker-a"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_lease.py -v`
Expected: FAIL / collection error — `acquire_lease`, `release_lease`, `current_lease_holder` do not exist yet.

- [ ] **Step 3: Append the lease functions to `src/motorcal/store.py`**

Add `import time` and `from datetime import datetime, timezone` to the top imports of `src/motorcal/store.py` (alongside the existing `sqlite3`/`contextmanager`/`Path` imports). Append to the end of the file:

```python
def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def acquire_lease(
    conn: sqlite3.Connection, holder: str, ttl_seconds: float, *, now: float | None = None
) -> bool:
    """Atomically claim the single refresh lease if it is free or expired."""
    now = time.time() if now is None else now
    with transaction(conn):
        row = conn.execute(
            "SELECT holder, expires_at FROM refresh_lease WHERE id = 1"
        ).fetchone()
        if row is not None and _parse_iso(row["expires_at"]) > now:
            return False
        conn.execute(
            """
            INSERT INTO refresh_lease (id, holder, acquired_at, expires_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                holder = excluded.holder,
                acquired_at = excluded.acquired_at,
                expires_at = excluded.expires_at
            """,
            (holder, _iso(now), _iso(now + ttl_seconds)),
        )
        return True


def release_lease(conn: sqlite3.Connection, holder: str) -> None:
    """Release the lease, but only if it is currently held by `holder`."""
    with transaction(conn):
        conn.execute(
            "DELETE FROM refresh_lease WHERE id = 1 AND holder = ?", (holder,)
        )


def current_lease_holder(conn: sqlite3.Connection, *, now: float | None = None) -> str | None:
    """Return the current live lease holder, or None if unheld or expired."""
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT holder, expires_at FROM refresh_lease WHERE id = 1"
    ).fetchone()
    if row is None or _parse_iso(row["expires_at"]) <= now:
        return None
    return row["holder"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_lease.py -v`
Expected: PASS, 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/motorcal/store.py tests/test_store_lease.py
git commit -m "Add atomic cross-process refresh lease (acquire/release/expiry)"
```

---

### Task 4: Online backup function and CLI wiring

**Files:**
- Modify: `src/motorcal/store.py`
- Modify: `src/motorcal/cli.py`
- Test: `tests/test_store_backup.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `connect`, `init_schema`, `upsert_source_event`, `transaction`, `check_integrity` from Tasks 1-3.
- Produces:
  - `def backup_database(source_path: Path, dest_path: Path) -> None` in `store.py` — uses SQLite's online backup API (`sqlite3.Connection.backup`) so it is safe to run against a live WAL-mode database; creates `dest_path` (or overwrites it) with a fully consistent copy.
  - `src/motorcal/cli.py` gains an argparse-based `main(argv: list[str] | None = None) -> int` replacing the Task 1 stub, with two subcommands:
    - `motorcal init-db --db PATH` — calls `connect` + `init_schema` on `PATH`, prints `"Initialized database at {PATH}"`, returns `0`.
    - `motorcal backup --db PATH --dest PATH` — calls `backup_database(db, dest)`, prints `"Backed up {db} to {dest}"`, returns `0`. If `check_integrity` on the source connection returns `False` first, it must print an error to stderr and return `1` **without** attempting the backup (never back up a database already known to be corrupt).
    - Any other/missing subcommand prints usage to stderr and returns `1` (this replaces, rather than removes, the Task 1 "not yet implemented" stub's spirit — argparse's own `--help` covers discoverability).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_backup.py
from pathlib import Path

from motorcal.store import (
    backup_database,
    check_integrity,
    connect,
    get_source_event,
    init_schema,
    transaction,
    upsert_source_event,
)


def test_backup_database_creates_a_working_copy(tmp_path):
    source_path = tmp_path / "source.db"
    dest_path = tmp_path / "backup.db"

    conn = connect(source_path)
    init_schema(conn)
    with transaction(conn):
        upsert_source_event(
            conn,
            provider="thesportsdb",
            id_event="2421035",
            series="wec",
            season="2026",
            round=1,
            name="6 Hours of Imola",
            date="2026-04-19",
            time="00:00:00",
            venue="Imola",
            country="Italy",
            raw_json="{}",
            seen_at="2026-07-29T00:00:00+00:00",
        )
    conn.close()

    backup_database(source_path, dest_path)

    assert dest_path.exists()
    backup_conn = connect(dest_path)
    row = get_source_event(backup_conn, "thesportsdb", "2421035")
    assert row is not None
    assert row["name"] == "6 Hours of Imola"
    assert check_integrity(backup_conn) is True


def test_backup_database_overwrites_existing_destination(tmp_path):
    source_path = tmp_path / "source.db"
    dest_path = tmp_path / "backup.db"
    dest_path.write_bytes(b"not a real database")

    conn = connect(source_path)
    init_schema(conn)
    conn.close()

    backup_database(source_path, dest_path)

    backup_conn = connect(dest_path)
    assert check_integrity(backup_conn) is True
```

```python
# tests/test_cli.py
from pathlib import Path

from motorcal.cli import main
from motorcal.store import check_integrity, connect, init_schema, transaction, upsert_source_event


def test_init_db_creates_and_initializes_database(tmp_path, capsys):
    db_path = tmp_path / "new.db"
    exit_code = main(["init-db", "--db", str(db_path)])
    assert exit_code == 0
    assert db_path.exists()
    captured = capsys.readouterr()
    assert str(db_path) in captured.out

    conn = connect(db_path)
    assert check_integrity(conn) is True


def test_backup_command_copies_database(tmp_path, capsys):
    db_path = tmp_path / "source.db"
    dest_path = tmp_path / "backup.db"
    main(["init-db", "--db", str(db_path)])

    exit_code = main(["backup", "--db", str(db_path), "--dest", str(dest_path)])
    assert exit_code == 0
    assert dest_path.exists()
    captured = capsys.readouterr()
    assert "Backed up" in captured.out


def test_backup_command_refuses_to_back_up_corrupt_database(tmp_path, capsys):
    db_path = tmp_path / "corrupt.db"
    dest_path = tmp_path / "backup.db"
    main(["init-db", "--db", str(db_path)])

    with open(db_path, "r+b") as f:
        f.seek(100)
        f.write(b"\xff" * 200)

    exit_code = main(["backup", "--db", str(db_path), "--dest", str(dest_path)])
    assert exit_code == 1
    assert not dest_path.exists()
    captured = capsys.readouterr()
    assert captured.err != ""


def test_main_with_no_subcommand_returns_1(capsys):
    exit_code = main([])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err != ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_backup.py tests/test_cli.py -v`
Expected: FAIL / collection error — `backup_database` does not exist; `cli.main` doesn't accept an `argv` list or subcommands yet.

- [ ] **Step 3: Append `backup_database` to `src/motorcal/store.py`**

```python
def backup_database(source_path: Path, dest_path: Path) -> None:
    """Create a fully consistent copy of a live (possibly WAL-mode) database.

    Any existing file at dest_path is removed first: SQLite's backup API
    requires the destination to be empty or a valid SQLite file — writing
    over pre-existing non-SQLite bytes raises "file is not a database".
    """
    for path in (dest_path, Path(f"{dest_path}-wal"), Path(f"{dest_path}-shm")):
        if path.exists():
            path.unlink()

    source_conn = sqlite3.connect(source_path)
    dest_conn = sqlite3.connect(dest_path)
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()
```

- [ ] **Step 4: Rewrite `src/motorcal/cli.py`**

```python
"""motorcal command-line interface."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from motorcal.store import backup_database, check_integrity, connect, init_schema


def _cmd_init_db(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    conn = connect(db_path)
    init_schema(conn)
    print(f"Initialized database at {db_path}")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    dest_path = Path(args.dest)

    conn = connect(db_path)
    if not check_integrity(conn):
        print(f"Refusing to back up {db_path}: integrity check failed", file=sys.stderr)
        return 1

    backup_database(db_path, dest_path)
    print(f"Backed up {db_path} to {dest_path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motorcal")
    subparsers = parser.add_subparsers(dest="command")

    init_db_parser = subparsers.add_parser("init-db", help="Create/upgrade the database schema")
    init_db_parser.add_argument("--db", required=True, help="Path to the SQLite database file")
    init_db_parser.set_defaults(func=_cmd_init_db)

    backup_parser = subparsers.add_parser("backup", help="Back up the database to another file")
    backup_parser.add_argument("--db", required=True, help="Path to the source database file")
    backup_parser.add_argument("--dest", required=True, help="Path to write the backup to")
    backup_parser.set_defaults(func=_cmd_backup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_usage(sys.stderr)
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_backup.py tests/test_cli.py -v`
Expected: PASS, 6 passed (2 backup + 4 CLI).

- [ ] **Step 6: Run the entire test suite**

Run: `uv run pytest -v`
Expected: all tests from Phase 1 and Phase 2 Tasks 1-4 pass — 78 passed total (49 + 9 + 7 + 7 + 6).

- [ ] **Step 7: Commit**

```bash
git add src/motorcal/store.py src/motorcal/cli.py tests/test_store_backup.py tests/test_cli.py
git commit -m "Add online backup function and wire init-db/backup into the CLI"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: schema for all 7 tables the spec's "database contains" list requires (Provider/snapshot semantics + Canonical and published event model sections); migrations via `PRAGMA user_version` gating (Deployment and recovery section, "SQLite uses WAL mode, integrity checks at startup"); atomic transactions proven across two tables (Architecture section, "atomic source/publication transaction"); atomic cross-process lease (Rate limiting and concurrency section); online backup (Deployment and recovery section, "periodic online backups to a separate file or volume").
- Explicitly out of scope for this phase (later phases own them): bounded round scanning and provider HTTP calls (Phase 3), classification (Phase 4), patch/synthetic-event CRUD beyond the `synthetic_events` table shape (Phase 5), fingerprint/sequence computation and retention/cancellation logic (Phase 6), ICS rendering and `feed_revision` read/write (Phase 7), FastAPI routes including `/readyz`'s actual use of `check_integrity` (Phase 8), the scheduler's actual use of `acquire_lease`/`release_lease` around a real refresh (Phase 9), and the `republish --force-version` recovery operation, which needs real sequence-computation logic from Phase 6 and belongs with Phase 10's recovery documentation.
- Type consistency check: `published_events.sequence` is `INTEGER NOT NULL` with no default — Phase 6 is responsible for actually computing `max(previous_sequence + 1, current_utc_unix_minute)` before calling `upsert_published_event`; this phase only stores whatever integer it's given. `time_confirmed` is stored as SQLite `INTEGER` (0/1) via `int(time_confirmed)` in `upsert_published_event` and read back as `sqlite3.Row`'s native int — callers in later phases must convert back to `bool` themselves (`sqlite3.Row` does not do this automatically); note this for whoever writes Phase 6's merge code.
