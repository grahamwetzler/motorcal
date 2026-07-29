"""SQLite persistence: schema, migrations, transactions, lease, and backup."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from motorcal.providers.thesportsdb import SnapshotResult

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, list[str]] = {
    1: [
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
    ],
}


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a database connection in autocommit mode with WAL and foreign keys enabled."""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if mode.lower() != "wal":
        raise RuntimeError(f"WAL mode unavailable for {db_path} (got {mode!r})")
    # No FOREIGN KEY constraints are declared: published_events must be able to
    # outlive its source_events/synthetic_events row (retention/tombstones).
    # This pragma is enabled for forward-compatibility if a later phase adds one.
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables if missing and stamp the schema version. Idempotent."""
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current_version} is newer than this build "
            f"supports (expected <= {SCHEMA_VERSION}); refusing to run against a "
            "newer schema."
        )
    if current_version == SCHEMA_VERSION:
        return
    with transaction(conn):
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={version}")


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
    """Wrap a block of writes in an immediate, all-or-nothing SQLite transaction.

    Reentrant: if a transaction is already open on this connection, this call
    is a no-op passthrough and the outermost transaction() owns commit/rollback.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass  # SQLite already auto-rolled back (e.g. disk full / I/O error)
        raise
    else:
        conn.execute("COMMIT")


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
        if row is not None and row["holder"] != holder and _parse_iso(row["expires_at"]) > now:
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


def backup_database(source_path: Path, dest_path: Path) -> None:
    """Create a fully consistent copy of a live (possibly WAL-mode) database.

    Any existing file at dest_path is removed first: SQLite's backup API
    requires the destination to be empty or a valid SQLite file — writing
    over pre-existing non-SQLite bytes raises "file is not a database".
    """
    source_path = Path(source_path)
    if not source_path.exists():
        raise FileNotFoundError(f"Source database not found: {source_path}")

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


def list_source_events_by_scope(
    conn: sqlite3.Connection, provider: str, series: str, season: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_events WHERE provider = ? AND series = ? AND season = ?",
        (provider, series, season),
    ).fetchall()


def get_snapshot_meta(
    conn: sqlite3.Connection, provider: str, series: str, season: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM source_snapshot_meta WHERE provider = ? AND series = ? AND season = ?",
        (provider, series, season),
    ).fetchone()


def upsert_snapshot_meta(
    conn: sqlite3.Connection,
    provider: str,
    series: str,
    season: str,
    last_complete_at: str,
    last_event_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO source_snapshot_meta (provider, series, season, last_complete_at, last_event_count)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (provider, series, season) DO UPDATE SET
            last_complete_at = excluded.last_complete_at,
            last_event_count = excluded.last_event_count
        """,
        (provider, series, season, last_complete_at, last_event_count),
    )


def mark_source_event_disappeared(
    conn: sqlite3.Connection, provider: str, id_event: str, disappeared_at: str
) -> None:
    conn.execute(
        "UPDATE source_events SET disappeared_at = ? WHERE provider = ? AND id_event = ?",
        (disappeared_at, provider, id_event),
    )


@dataclass
class IngestResult:
    """The outcome of deciding whether to commit one provider scan."""

    committed: bool
    reason: str | None
    events_written: int


def ingest_snapshot(
    conn: sqlite3.Connection,
    snapshot: SnapshotResult,
    *,
    provider: str,
    series: str,
    season: str,
    now: str,
    is_current_season: bool,
) -> IngestResult:
    """Decide whether to commit a provider scan, and if so, write it atomically.

    Implements: incomplete snapshots are discarded in full; an empty snapshot is
    suspicious (and rejected) for the current season always, and for a future
    season only if that scope was previously populated; disappearance marking
    (not published cancellation — that is Phase 6) happens only for a committed,
    complete snapshot.
    """
    if not snapshot.complete:
        return IngestResult(committed=False, reason="incomplete_snapshot", events_written=0)

    if len(snapshot.events) == 0:
        if is_current_season:
            return IngestResult(
                committed=False, reason="suspicious_empty_current_season", events_written=0
            )
        existing_meta = get_snapshot_meta(conn, provider, series, season)
        previously_populated = existing_meta is not None and existing_meta["last_event_count"] > 0
        if previously_populated:
            return IngestResult(
                committed=False, reason="suspicious_empty_future_season", events_written=0
            )

    with transaction(conn):
        seen_ids = set()
        for event in snapshot.events:
            upsert_source_event(
                conn,
                provider=provider,
                id_event=event.id_event,
                series=event.series,
                season=event.season,
                round=event.round,
                name=event.name,
                date=event.date,
                time=event.time,
                venue=event.venue,
                country=event.country,
                raw_json=json.dumps(event.raw),
                seen_at=now,
            )
            seen_ids.add(event.id_event)

        for row in list_source_events_by_scope(conn, provider, series, season):
            if row["id_event"] not in seen_ids and row["disappeared_at"] is None:
                mark_source_event_disappeared(conn, provider, row["id_event"], now)

        upsert_snapshot_meta(conn, provider, series, season, now, len(snapshot.events))

    return IngestResult(committed=True, reason=None, events_written=len(snapshot.events))
