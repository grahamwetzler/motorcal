# Motorsports Calendar — Phase 7: Deterministic ICS Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/motorcal/ics.py`: render one deterministic `VCALENDAR` per series from `published_events` rows (Phase 6's output), plus revision tracking so repeated renders of unchanged data produce a stable content hash for `ETag`/`Last-Modified`/`304` support (Phase 8 will wire the actual HTTP layer).

**Architecture:** `ics.py` renders using the `icalendar` library (already a Phase 1 dependency). Rendering functions take explicit keyword arguments (not raw `sqlite3.Row` objects), mirroring the `PreviousPublishedState` pattern from Phase 6 — this keeps `ics.py` testable without a database. A single integration function converts `published_events` rows into those keyword arguments and ties everything to `store.py`.

**Tech Stack:** `icalendar` (already installed). No new dependencies.

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous. The "ICS generation" section is this phase's primary spec.
- Timed values render as UTC with a trailing `Z` and **no `VTIMEZONE` component** — confirmed empirically: a `datetime` parsed via `datetime.fromisoformat("...+00:00")` (exactly how Phase 6 stores `start`/`dtstamp`/`last_modified`) has `tzinfo == datetime.timezone.utc` and `icalendar` renders it correctly with no `VTIMEZONE`.
- All-day events render as `DTSTART;VALUE=DATE:YYYYMMDD` using a plain `date` (not `datetime`) — confirmed via prototyping.
- An event with no known duration gets no `DTEND`/`DURATION` property at all — never invent an end time. Confirmed: `icalendar` happily serializes a `VEVENT` with only `DTSTART`.
- `DTSTAMP`, `LAST-MODIFIED`, and `SEQUENCE` come straight from the stored `published_events` row (Phase 6's output) — never from "now" at render time. This is what makes rendering the same row twice byte-identical.
- Postponed events (`STATUS:TENTATIVE`) get a `[Postponed] ` prefix on the summary (and, for consistency, on any `VALARM`'s own `DESCRIPTION`, since that's derived from the same rendered summary). Cancelled events render with `STATUS:CANCELLED` and no special prefix — the `STATUS` field alone signals it.
- Alarms: one `VALARM` per configured offset string (already resolved and stored as a JSON list on the `published_events` row by Phase 6), each `ACTION:DISPLAY` with a negative `TRIGGER;RELATED=START` computed via `motorcal.config.parse_alarm_offset` (already built in Phase 1) converted to a `timedelta`. Confirmed via prototyping that `icalendar` correctly renders a negative `timedelta` as an ICS duration (e.g. `-P1D`, `-PT30M`) and preserves the insertion order of multiple `VALARM` components.
- Determinism: events within one calendar must be added in a **fixed, content-derived order** (sort by `uid`) so that re-rendering unchanged data produces byte-identical output — confirmed via prototyping that two independently-built `icalendar.Calendar` objects with the same components added in the same order produce identical `to_ical()` bytes.
- Calendar-level properties: `PRODID`, `VERSION:2.0`, `METHOD:PUBLISH`, `X-WR-CALNAME` (the series' configured `name`), `X-WR-CALDESC` (mentions "race sessions only" when `SeriesConfig.race_only` is set — this is the spec's "IndyCar and IMSA are explicitly labeled race-only in ... calendar descriptions" requirement), `REFRESH-INTERVAL;VALUE=DURATION:PT1H`, `X-PUBLISHED-TTL:PT1H`.
- The feed's content hash must change whenever EITHER an event's content changes OR calendar-level metadata changes (e.g. a config reload renames a series) — hashing the entire rendered byte output (not just the events) satisfies this automatically, since calendar-level properties are part of those same bytes.
- Revision tracking must be sticky like Phase 6's sequence/timestamp preservation: if the newly rendered content hash equals the previously stored `feed_revision.revision`, `updated_at` must NOT change — only a genuine content change advances it. This is what lets `Last-Modified` stay stable across repeated no-op rebuilds.
- No pip: dependency management is `uv` only.

---

### Task 1: `build_vevent` — single-event rendering

**Files:**
- Create: `src/motorcal/ics.py`
- Test: `tests/test_ics_vevent.py`

**Interfaces:**
- Consumes: `parse_alarm_offset` from `motorcal.config` (Phase 1).
- Produces (used by Task 2):
  - `def build_vevent(*, uid: str, summary: str, status: str, start: datetime | None, all_day_date: str | None, duration_seconds: int | None, dtstamp: datetime, last_modified: datetime, sequence: int, description: str, location: str | None, alarms: list[str]) -> icalendar.Event`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ics_vevent.py
from datetime import date, datetime, timezone

from motorcal.ics import build_vevent


def test_confirmed_timed_event_with_duration_and_alarm():
    event = build_vevent(
        uid="thesportsdb-2421035@x.example.com",
        summary="6 Hours of Imola",
        status="CONFIRMED",
        start=datetime(2026, 4, 19, 13, 0, tzinfo=timezone.utc),
        all_day_date=None,
        duration_seconds=6 * 3600,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=1,
        description="Venue: Imola\nSource: TheSportsDB",
        location="Imola, Italy",
        alarms=["-1d", "-30m"],
    )
    ics_bytes = event.to_ical()

    assert b"VTIMEZONE" not in ics_bytes
    assert b"DTSTART:20260419T130000Z" in ics_bytes
    assert b"DTEND:20260419T190000Z" in ics_bytes
    assert b"UID:thesportsdb-2421035@x.example.com" in ics_bytes
    assert b"STATUS:CONFIRMED" in ics_bytes
    assert b"SUMMARY:6 Hours of Imola" in ics_bytes
    assert ics_bytes.count(b"BEGIN:VALARM") == 2
    assert b"TRIGGER:-P1D" in ics_bytes
    assert b"TRIGGER:-PT30M" in ics_bytes


def test_all_day_event_has_no_dtend_and_no_alarms():
    event = build_vevent(
        uid="thesportsdb-9999@x.example.com",
        summary="Some Race (time TBC)",
        status="CONFIRMED",
        start=None,
        all_day_date="2026-05-01",
        duration_seconds=None,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=1,
        description="Time not yet confirmed by the source (TBC).",
        location=None,
        alarms=[],
    )
    ics_bytes = event.to_ical()

    assert b"DTSTART;VALUE=DATE:20260501" in ics_bytes
    assert b"DTEND" not in ics_bytes
    assert b"DURATION" not in ics_bytes
    assert ics_bytes.count(b"BEGIN:VALARM") == 0


def test_timed_event_with_no_known_duration_has_no_dtend():
    event = build_vevent(
        uid="u3@x.example.com",
        summary="Hyperpole Qualifying",
        status="CONFIRMED",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc),
        all_day_date=None,
        duration_seconds=None,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=1,
        description="d",
        location=None,
        alarms=[],
    )
    ics_bytes = event.to_ical()

    assert b"DTSTART:20260610T164500Z" in ics_bytes
    assert b"DTEND" not in ics_bytes
    assert b"DURATION" not in ics_bytes


def test_tentative_status_prefixes_postponed_on_summary_and_alarm():
    event = build_vevent(
        uid="u4@x.example.com",
        summary="Some Race",
        status="TENTATIVE",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc),
        all_day_date=None,
        duration_seconds=None,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=2,
        description="d",
        location=None,
        alarms=["-1d"],
    )
    ics_bytes = event.to_ical()

    assert b"STATUS:TENTATIVE" in ics_bytes
    assert b"SUMMARY:[Postponed] Some Race" in ics_bytes
    assert b"DESCRIPTION:[Postponed] Some Race" in ics_bytes  # the VALARM's own description


def test_cancelled_status_has_no_special_prefix():
    event = build_vevent(
        uid="u5@x.example.com",
        summary="Cancelled Race",
        status="CANCELLED",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc),
        all_day_date=None,
        duration_seconds=None,
        dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sequence=2,
        description="d",
        location=None,
        alarms=[],
    )
    ics_bytes = event.to_ical()

    assert b"STATUS:CANCELLED" in ics_bytes
    assert b"SUMMARY:Cancelled Race" in ics_bytes  # no prefix


def test_location_omitted_when_none():
    event = build_vevent(
        uid="u6@x.example.com", summary="S", status="CONFIRMED",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc), all_day_date=None,
        duration_seconds=None, dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1,
        description="d", location=None, alarms=[],
    )
    ics_bytes = event.to_ical()
    assert b"LOCATION" not in ics_bytes


def test_rendering_the_same_input_twice_is_byte_identical():
    kwargs = dict(
        uid="u7@x.example.com", summary="S", status="CONFIRMED",
        start=datetime(2026, 6, 10, 16, 45, tzinfo=timezone.utc), all_day_date=None,
        duration_seconds=3600, dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1,
        description="d", location="L", alarms=["-1d", "-30m"],
    )
    b1 = build_vevent(**kwargs).to_ical()
    b2 = build_vevent(**kwargs).to_ical()
    assert b1 == b2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ics_vevent.py -v`
Expected: FAIL / collection error — `motorcal.ics` does not exist yet.

- [ ] **Step 3: Write `src/motorcal/ics.py`**

```python
"""Deterministic ICS generation from published_events state."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from icalendar import Alarm, Event

from motorcal.config import parse_alarm_offset

PRODID = "-//motorcal//motorsports-calendar//EN"


def build_vevent(
    *,
    uid: str,
    summary: str,
    status: str,
    start: datetime | None,
    all_day_date: str | None,
    duration_seconds: int | None,
    dtstamp: datetime,
    last_modified: datetime,
    sequence: int,
    description: str,
    location: str | None,
    alarms: list[str],
) -> Event:
    """Render one published event into an icalendar VEVENT component."""
    event = Event()
    event.add("uid", uid)

    rendered_summary = f"[Postponed] {summary}" if status == "TENTATIVE" else summary
    event.add("summary", rendered_summary)

    if start is not None:
        event.add("dtstart", start)
        if duration_seconds:
            event.add("dtend", start + timedelta(seconds=duration_seconds))
    else:
        event.add("dtstart", date.fromisoformat(all_day_date))

    event.add("dtstamp", dtstamp)
    event.add("last-modified", last_modified)
    event.add("sequence", sequence)
    event.add("status", status)
    event.add("description", description)
    if location:
        event.add("location", location)

    for offset in alarms:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", rendered_summary)
        alarm.add("trigger", timedelta(seconds=parse_alarm_offset(offset)))
        event.add_component(alarm)

    return event
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ics_vevent.py -v`
Expected: PASS, 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/motorcal/ics.py tests/test_ics_vevent.py
git commit -m "Add build_vevent: deterministic single-event ICS rendering"
```

---

### Task 2: `build_calendar` — full calendar assembly

**Files:**
- Modify: `src/motorcal/ics.py`
- Test: `tests/test_ics_calendar.py`

**Interfaces:**
- Consumes: `build_vevent` (Task 1); `SeriesConfig` from `motorcal.config` (Phase 1).
- Produces (used by Task 3):
  - `def build_calendar(series_config: SeriesConfig, vevents: list[icalendar.Event]) -> icalendar.Calendar` — assembles calendar-level properties and adds every VEVENT sorted by `UID` (for deterministic output regardless of input order).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ics_calendar.py
from datetime import datetime, timezone

from motorcal.config import SeriesConfig
from motorcal.ics import build_calendar, build_vevent


def _event(uid, start_hour=13):
    return build_vevent(
        uid=uid, summary=f"Event {uid}", status="CONFIRMED",
        start=datetime(2026, 4, 19, start_hour, 0, tzinfo=timezone.utc), all_day_date=None,
        duration_seconds=3600, dtstamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1,
        description="d", location=None, alarms=[],
    )


def test_calendar_has_required_calendar_level_properties():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    cal = build_calendar(series_cfg, [_event("u1")])
    ics_bytes = cal.to_ical()

    assert b"VERSION:2.0" in ics_bytes
    assert b"METHOD:PUBLISH" in ics_bytes
    assert b"X-WR-CALNAME:WEC" in ics_bytes
    assert b"REFRESH-INTERVAL;VALUE=DURATION:PT1H" in ics_bytes
    assert b"X-PUBLISHED-TTL:PT1H" in ics_bytes
    assert b"PRODID" in ics_bytes


def test_race_only_series_mentions_it_in_caldesc():
    series_cfg = SeriesConfig(league_id=4373, name="IndyCar", max_round=30, race_only=True)
    cal = build_calendar(series_cfg, [_event("u1")])
    ics_bytes = cal.to_ical()

    assert b"CALDESC" in ics_bytes
    assert b"race" in ics_bytes.lower()


def test_non_race_only_series_caldesc_has_no_race_only_note():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20, race_only=False)
    cal = build_calendar(series_cfg, [_event("u1")])
    ics_bytes = cal.to_ical()

    assert b"race sessions only" not in ics_bytes.lower()


def test_events_are_rendered_in_uid_sorted_order_regardless_of_input_order():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    events_in_reverse = [_event("z-event"), _event("a-event"), _event("m-event")]
    cal = build_calendar(series_cfg, events_in_reverse)
    ics_bytes = cal.to_ical()

    a_pos = ics_bytes.index(b"UID:a-event")
    m_pos = ics_bytes.index(b"UID:m-event")
    z_pos = ics_bytes.index(b"UID:z-event")
    assert a_pos < m_pos < z_pos


def test_rendering_the_same_calendar_twice_is_byte_identical():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    events = [_event("u1"), _event("u2")]
    b1 = build_calendar(series_cfg, events).to_ical()
    b2 = build_calendar(series_cfg, [_event("u1"), _event("u2")]).to_ical()
    assert b1 == b2


def test_rendering_is_stable_regardless_of_input_list_order():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    forward = [_event("u1"), _event("u2")]
    backward = [_event("u2"), _event("u1")]
    assert build_calendar(series_cfg, forward).to_ical() == build_calendar(series_cfg, backward).to_ical()


def test_empty_calendar_still_has_valid_header():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    cal = build_calendar(series_cfg, [])
    ics_bytes = cal.to_ical()
    assert b"BEGIN:VCALENDAR" in ics_bytes
    assert b"END:VCALENDAR" in ics_bytes
    assert b"BEGIN:VEVENT" not in ics_bytes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ics_calendar.py -v`
Expected: FAIL / collection error — `build_calendar` does not exist yet.

- [ ] **Step 3: Append to `src/motorcal/ics.py`**

Add `from motorcal.config import SeriesConfig, parse_alarm_offset` to the top imports (extend the existing `from motorcal.config import parse_alarm_offset` line rather than duplicating it). Add `from icalendar import Alarm, Calendar, Event` (extend the existing `from icalendar import Alarm, Event` line to also import `Calendar`, rather than a separate import line).

Append to the end of the file:

```python
def build_calendar(series_config: SeriesConfig, vevents: list[Event]) -> Calendar:
    """Assemble one deterministic VCALENDAR for a series from its rendered VEVENTs."""
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("method", "PUBLISH")
    calendar.add("x-wr-calname", series_config.name)

    caldesc = f"{series_config.name} calendar"
    if series_config.race_only:
        caldesc += " (race sessions only)"
    calendar.add("x-wr-caldesc", caldesc)

    calendar.add("refresh-interval;value=duration", "PT1H")
    calendar.add("x-published-ttl", "PT1H")

    for vevent in sorted(vevents, key=lambda e: str(e.get("uid"))):
        calendar.add_component(vevent)

    return calendar
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ics_calendar.py -v`
Expected: PASS, 7 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-6 (217) plus this phase's 7 (Task 1) plus 7 (Task 2) pass — 231 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/ics.py tests/test_ics_calendar.py
git commit -m "Add build_calendar: deterministic per-series VCALENDAR assembly"
```

---

### Task 3: Full-series rendering, content hashing, and revision tracking

**Files:**
- Modify: `src/motorcal/store.py`
- Modify: `src/motorcal/ics.py`
- Test: `tests/test_store_feed_revision.py`
- Test: `tests/test_ics_render.py`

**Interfaces:**
- Consumes: `connect`, `init_schema`, `transaction`, `upsert_published_event` from Phase 2; `build_vevent`, `build_calendar` from Tasks 1-2.
- Produces (used by Phase 8):
  - `def list_published_events_by_series(conn: sqlite3.Connection, series: str) -> list[sqlite3.Row]` (new in `store.py`).
  - `def get_feed_revision(conn: sqlite3.Connection, series: str) -> sqlite3.Row | None` (new in `store.py`).
  - `def upsert_feed_revision(conn: sqlite3.Connection, series: str, revision: str, updated_at: str) -> None` (new in `store.py`, insert-or-replace by `series`).
  - `def render_calendar_bytes(conn: sqlite3.Connection, series: str, series_config: SeriesConfig) -> bytes` (new in `ics.py`) — queries `published_events` for the series, converts each row into `build_vevent` keyword arguments, and returns `build_calendar(...).to_ical()`.
  - `def compute_content_hash(ics_bytes: bytes) -> str` (new in `ics.py`) — SHA-256 hex digest.
  - `@dataclass class FeedRevisionState` fields: `revision: str`, `updated_at: str` (new in `ics.py`).
  - `def sync_feed_revision(conn: sqlite3.Connection, series: str, ics_bytes: bytes, now: str) -> FeedRevisionState` (new in `ics.py`) — computes the new hash; if it matches the stored revision, returns the **stored** state unchanged (does not touch `updated_at`); otherwise writes the new revision with `updated_at = now` inside `store.transaction()` and returns the new state.

- [ ] **Step 1: Write the failing store tests**

```python
# tests/test_store_feed_revision.py
from motorcal.store import (
    connect,
    get_feed_revision,
    init_schema,
    list_published_events_by_series,
    transaction,
    upsert_feed_revision,
    upsert_published_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert_published(conn, uid, series):
    upsert_published_event(
        conn, uid=uid, series=series, session_type="race", summary="S",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=3600, location="L", description="D", status="CONFIRMED",
        sequence=1, dtstamp="t0", last_modified="t0", fingerprint="fp", alarms_json="[]",
        source_provider="thesportsdb", source_id_event="1", synthetic_uid=None,
        cancelled_at=None, retain_until=None,
    )


def test_list_published_events_by_series_filters_correctly(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", series="wec")
        _insert_published(conn, "u2", series="wec")
        _insert_published(conn, "u3", series="f1")

    rows = list_published_events_by_series(conn, "wec")
    assert {row["uid"] for row in rows} == {"u1", "u2"}


def test_feed_revision_round_trip(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_feed_revision(conn, "wec") is None

    with transaction(conn):
        upsert_feed_revision(conn, "wec", "abc123", "t0")

    row = get_feed_revision(conn, "wec")
    assert row["revision"] == "abc123"
    assert row["updated_at"] == "t0"


def test_feed_revision_upsert_replaces_previous_values(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_feed_revision(conn, "wec", "abc123", "t0")
    with transaction(conn):
        upsert_feed_revision(conn, "wec", "def456", "t1")

    row = get_feed_revision(conn, "wec")
    assert row["revision"] == "def456"
    assert row["updated_at"] == "t1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_feed_revision.py -v`
Expected: FAIL / collection error — `list_published_events_by_series`, `get_feed_revision`, `upsert_feed_revision` do not exist yet.

- [ ] **Step 3: Append to `src/motorcal/store.py`**

```python
def list_published_events_by_series(conn: sqlite3.Connection, series: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM published_events WHERE series = ?", (series,)
    ).fetchall()


def get_feed_revision(conn: sqlite3.Connection, series: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM feed_revision WHERE series = ?", (series,)
    ).fetchone()


def upsert_feed_revision(conn: sqlite3.Connection, series: str, revision: str, updated_at: str) -> None:
    conn.execute(
        """
        INSERT INTO feed_revision (series, revision, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (series) DO UPDATE SET
            revision = excluded.revision,
            updated_at = excluded.updated_at
        """,
        (series, revision, updated_at),
    )
```

- [ ] **Step 4: Run store tests to verify they pass**

Run: `uv run pytest tests/test_store_feed_revision.py -v`
Expected: PASS, 3 passed.

- [ ] **Step 5: Write the failing ics rendering/revision tests**

```python
# tests/test_ics_render.py
import json

from motorcal.config import SeriesConfig
from motorcal.ics import compute_content_hash, render_calendar_bytes, sync_feed_revision
from motorcal.store import connect, get_feed_revision, init_schema, transaction, upsert_published_event


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert_published(conn, uid, series="wec", summary="S"):
    upsert_published_event(
        conn, uid=uid, series=series, session_type="race", summary=summary,
        start="2026-04-19T13:00:00+00:00", all_day_date=None, time_confirmed=True,
        duration_seconds=3600, location="L", description="D", status="CONFIRMED",
        sequence=1, dtstamp="2026-01-01T00:00:00+00:00", last_modified="2026-01-01T00:00:00+00:00",
        fingerprint="fp", alarms_json=json.dumps(["-1d"]),
        source_provider="thesportsdb", source_id_event="1", synthetic_uid=None,
        cancelled_at=None, retain_until=None,
    )


def test_render_calendar_bytes_produces_valid_ics_for_series(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", series="wec")
        _insert_published(conn, "u2", series="f1")  # different series, must be excluded

    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    ics_bytes = render_calendar_bytes(conn, "wec", series_cfg)

    assert b"BEGIN:VCALENDAR" in ics_bytes
    assert b"UID:u1" in ics_bytes
    assert b"UID:u2" not in ics_bytes
    assert b"X-WR-CALNAME:WEC" in ics_bytes


def test_render_calendar_bytes_is_deterministic_across_calls(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "u1", series="wec")

    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    b1 = render_calendar_bytes(conn, "wec", series_cfg)
    b2 = render_calendar_bytes(conn, "wec", series_cfg)
    assert b1 == b2


def test_compute_content_hash_is_stable_for_identical_bytes():
    assert compute_content_hash(b"hello") == compute_content_hash(b"hello")
    assert compute_content_hash(b"hello") != compute_content_hash(b"world")


def test_sync_feed_revision_creates_a_new_revision_on_first_sync(tmp_path):
    conn = _fresh_conn(tmp_path)
    state = sync_feed_revision(conn, "wec", b"content-v1", now="t1")

    assert state.revision == compute_content_hash(b"content-v1")
    assert state.updated_at == "t1"
    row = get_feed_revision(conn, "wec")
    assert row["revision"] == state.revision
    assert row["updated_at"] == "t1"


def test_sync_feed_revision_does_not_advance_updated_at_when_content_is_unchanged(tmp_path):
    conn = _fresh_conn(tmp_path)
    sync_feed_revision(conn, "wec", b"content-v1", now="t1")

    state = sync_feed_revision(conn, "wec", b"content-v1", now="t2")  # same bytes, later "now"

    assert state.updated_at == "t1"  # unchanged -- this is the determinism guarantee
    row = get_feed_revision(conn, "wec")
    assert row["updated_at"] == "t1"


def test_sync_feed_revision_advances_when_content_changes(tmp_path):
    conn = _fresh_conn(tmp_path)
    sync_feed_revision(conn, "wec", b"content-v1", now="t1")

    state = sync_feed_revision(conn, "wec", b"content-v2", now="t2")

    assert state.revision == compute_content_hash(b"content-v2")
    assert state.updated_at == "t2"
    row = get_feed_revision(conn, "wec")
    assert row["revision"] == state.revision
    assert row["updated_at"] == "t2"
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/test_ics_render.py -v`
Expected: FAIL / collection error — `render_calendar_bytes`, `compute_content_hash`, `sync_feed_revision` do not exist yet.

- [ ] **Step 7: Append to `src/motorcal/ics.py`**

Add these imports (check first for duplicates against what Tasks 1-2 already added):

```python
import hashlib
import json
import sqlite3
from dataclasses import dataclass

from motorcal.store import (
    get_feed_revision,
    list_published_events_by_series,
    transaction,
    upsert_feed_revision,
)
```

Append to the end of the file:

```python
def _row_to_vevent(row: sqlite3.Row) -> Event:
    return build_vevent(
        uid=row["uid"],
        summary=row["summary"],
        status=row["status"],
        start=datetime.fromisoformat(row["start"]) if row["start"] else None,
        all_day_date=row["all_day_date"],
        duration_seconds=row["duration_seconds"],
        dtstamp=datetime.fromisoformat(row["dtstamp"]),
        last_modified=datetime.fromisoformat(row["last_modified"]),
        sequence=row["sequence"],
        description=row["description"],
        location=row["location"],
        alarms=json.loads(row["alarms_json"]),
    )


def render_calendar_bytes(
    conn: sqlite3.Connection, series: str, series_config: SeriesConfig
) -> bytes:
    """Render the current, deterministic ICS bytes for one series from stored state."""
    rows = list_published_events_by_series(conn, series)
    vevents = [_row_to_vevent(row) for row in rows]
    return build_calendar(series_config, vevents).to_ical()


def compute_content_hash(ics_bytes: bytes) -> str:
    return hashlib.sha256(ics_bytes).hexdigest()


@dataclass
class FeedRevisionState:
    revision: str
    updated_at: str


def sync_feed_revision(
    conn: sqlite3.Connection, series: str, ics_bytes: bytes, now: str
) -> FeedRevisionState:
    """Advance the stored feed revision only if the content actually changed."""
    new_revision = compute_content_hash(ics_bytes)
    existing = get_feed_revision(conn, series)
    if existing is not None and existing["revision"] == new_revision:
        return FeedRevisionState(revision=existing["revision"], updated_at=existing["updated_at"])

    with transaction(conn):
        upsert_feed_revision(conn, series, new_revision, now)
    return FeedRevisionState(revision=new_revision, updated_at=now)
```

`datetime` must already be imported at the top of `ics.py` from Task 1 (`from datetime import date, datetime, timedelta`) — do not duplicate it.

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_ics_render.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 9: Run the entire test suite**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-6 and Phase 7 Tasks 1-3 pass — 240 passed total (217 + 7 + 7 + 3 + 6). Note there are two test files in this task (`test_store_feed_revision.py` with 3 tests, `test_ics_render.py` with 6 tests) — confirm the actual count with `uv run pytest -v` and trust its output over this number if they differ.

- [ ] **Step 10: Commit**

```bash
git add src/motorcal/store.py src/motorcal/ics.py tests/test_store_feed_revision.py tests/test_ics_render.py
git commit -m "Add series rendering, content hashing, and sticky feed-revision tracking"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: all required calendar-level properties (ICS generation section); UTC-only timed values with no VTIMEZONE and all-day VALUE=DATE rendering (same section); no invented end times (Merge and time handling section, already enforced upstream by Phase 6 — this phase just must not add a DTEND when `duration_seconds` is `None`); postponed `[Postponed]` prefix and cancelled `STATUS:CANCELLED` rendering; multiple VALARM support with negative `TRIGGER;RELATED=START`; deterministic byte output via fixed UID-sorted event order; ETag/Last-Modified determinism via the sticky `sync_feed_revision` (content hash unchanged → `updated_at` unchanged).
- Explicitly out of scope for this phase (Phase 8 owns them): the actual HTTP `ETag`/`If-None-Match`/`304` protocol handling, `Cache-Control` headers, and wiring `render_calendar_bytes`/`sync_feed_revision` into a route; `/status` diagnostics; token-protected access.
- Type consistency check: `render_calendar_bytes` and `sync_feed_revision` both take a raw `sqlite3.Connection`, matching the pattern every other phase's top-level orchestration function uses (`ingest_snapshot`, `rebuild_publication`). `render_calendar_bytes` returns plain `bytes` (not a `Calendar` object) since that's what Phase 8's HTTP response body needs directly. `SeriesConfig` (not the whole `RootConfig`) is threaded through to `build_calendar`/`render_calendar_bytes` because calendar-level rendering only ever needs one series' own config, not the full bundle — Phase 8 is responsible for looking up `root_config.series[series]` before calling in, exactly as Phase 6's `rebuild_publication` already does for the same reason.
