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
