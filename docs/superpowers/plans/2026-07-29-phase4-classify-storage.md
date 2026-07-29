# Motorsports Calendar — Phase 4: Classification + Normalized Source Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/motorcal/classify.py` (pure, per-series ordered-regex session classification) and extend `src/motorcal/store.py` with `ingest_snapshot` — the function that takes a Phase 3 `SnapshotResult` and decides whether to commit it, applying the complete-snapshot, suspicious-empty, and disappearance-reconciliation rules from the spec, all inside one atomic transaction.

**Architecture:** Classification is a **pure function** of `(series, name, round)` — it is deliberately NOT persisted as a column on `source_events`. `source_events` stores only the raw normalized fields (name/date/time/venue/round/etc.); Phase 6's publication rebuild will call `classify_event` again when reading source events back out. This avoids a schema migration in this phase and keeps `classify.py` trivially unit-testable in isolation from the database.

**Tech Stack:** Reuses `src/motorcal/store.py` (Phase 2) and `src/motorcal/providers/thesportsdb.py`'s `SnapshotResult`/`ProviderEvent` (Phase 3). No new dependencies.

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous.
- Classification rules (ordered, per series, case-insensitive substring/pattern matching against the event name):
  - Round `500` is always `testing`, regardless of series or name — checked before any name-based rule.
  - WEC: match `hyperpole` before `qualifying` (a hyperpole session name also contains "Qualifying" — e.g. `"...Hyperpole Qualifying – LMP2 & LMGT3"` — and must classify as `hyperpole`, not `qualifying`).
  - F1: match `sprint qualifying` before `sprint` or bare `qualifying` (e.g. `"Chinese Grand Prix Sprint Qualifying"` must classify as `sprint_qualifying`, not `sprint` or `qualifying`).
  - A championship event with no session suffix classifies as `race` only through a tested, series-specific **positive** regex (e.g. F1: the name fully matches `^.+ Grand Prix$`; WEC: the name fully matches `^\d+ Hours? of .+$`) — never as an "else/fallback" branch. Anything that matches none of a series' rules is `unknown`, not `race`.
  - IndyCar and IMSA are configured `race_only: true` and expose only race-level events from the provider (confirmed against real captured data — no practice/qualifying entries ever appear for these series); their positive rule is simply "every event in this series is `race`" (still checked after the round-500 test, which still applies — e.g. IMSA's real round 500 is literally named `"Roar Before The Rolex 24"`, still `testing`).
  - A series with no configured rule set at all classifies every event as `unknown` (this only matters if a future series is added to config without adding matching rules here — it must fail safe to `unknown`, never crash or silently default to `race`).
- Disappearance reconciliation (source-layer half only — the spec's "becomes CANCELLED" language describes the *published* event and is Phase 6's responsibility; this phase only marks the *source* row):
  - Applies only after a **complete** snapshot, and only within the exact `{provider, series, season}` scope that was scanned.
  - A previously-observed source event absent from a new complete snapshot gets `source_events.disappeared_at` set to the ingest timestamp.
  - If a disappeared event reappears with the same `id_event`, it is reactivated — this already happens for free via `store.upsert_source_event`'s existing `ON CONFLICT ... SET disappeared_at = NULL` behavior (built in Phase 2); this phase does not need to add anything for reactivation, only for marking the disappearance in the first place.
  - An incomplete snapshot must not create, update, or clear any `disappeared_at` value.
- The complete-snapshot and suspicious-empty contract:
  - An incomplete snapshot (`SnapshotResult.complete is False`) is discarded in full — no `source_events` row is created, updated, or marked disappeared, and no `source_snapshot_meta` row is touched.
  - An empty, complete snapshot (`SnapshotResult.complete is True`, zero events) for the **current** calendar-year season is always suspicious and rejected, regardless of history.
  - An empty, complete snapshot for a **future** season (fetched because `next_season_from` has passed, per config) is accepted only if there is no previously-populated snapshot for that exact scope (`source_snapshot_meta.last_event_count` is absent or `0`); if that scope was previously populated and is now empty, it is suspicious and rejected.
  - Whether a season is "current" or "future" is **not** this phase's concern — it's determined by the caller (Phase 9's scheduler, which knows today's date and `config.source.next_season_from`) and passed in as an explicit `is_current_season: bool` argument.
  - Replacing the source snapshot (writing all of a scan's events, marking disappearances, and updating `source_snapshot_meta`) happens inside one call to `store.transaction()`, so a crash partway through cannot leave a scope half-updated.
- No pip: dependency management is `uv` only.

---

### Task 1: Classification (`classify.py`)

**Files:**
- Create: `src/motorcal/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `SessionType` from `motorcal.models` (Phase 1).
- Produces (used by Phase 6):
  - `def classify_event(series: str, name: str, round_number: int) -> SessionType` — pure function, no I/O.

- [ ] **Step 1: Write the failing tests**

Every case below is a real event name captured from the live TheSportsDB API in Phase 1's fixture corpus (`tests/fixtures/thesportsdb/`) — these are not hypothetical examples.

```python
# tests/test_classify.py
from motorcal.classify import classify_event
from motorcal.models import SessionType


def test_round_500_is_always_testing_regardless_of_series_or_name():
    assert classify_event("f1", "Bahrain Testing 1 Day 1", 500) is SessionType.TESTING
    assert classify_event("wec", "Imola Prologue Morning Session", 500) is SessionType.TESTING
    assert classify_event("imsa", "Roar Before The Rolex 24", 500) is SessionType.TESTING
    assert classify_event("indycar", "Anything At All", 500) is SessionType.TESTING


def test_f1_practice_sessions():
    assert classify_event("f1", "Australian Grand Prix Practice 1", 1) is SessionType.PRACTICE
    assert classify_event("f1", "Australian Grand Prix Practice 2", 1) is SessionType.PRACTICE
    assert classify_event("f1", "Chinese Grand Prix Practice 1", 2) is SessionType.PRACTICE


def test_f1_qualifying():
    assert classify_event("f1", "Australian Grand Prix Qualifying", 1) is SessionType.QUALIFYING


def test_f1_sprint_qualifying_before_sprint_and_qualifying():
    assert classify_event("f1", "Chinese Grand Prix Sprint Qualifying", 2) is SessionType.SPRINT_QUALIFYING


def test_f1_sprint():
    assert classify_event("f1", "Chinese Grand Prix Sprint", 2) is SessionType.SPRINT


def test_f1_bare_name_is_race_via_positive_rule():
    assert classify_event("f1", "Australian Grand Prix", 1) is SessionType.RACE
    assert classify_event("f1", "Chinese Grand Prix", 2) is SessionType.RACE


def test_f1_unrecognized_name_is_unknown_not_race():
    assert classify_event("f1", "Drivers Parade", 1) is SessionType.UNKNOWN


def test_wec_hyperpole_before_qualifying():
    assert (
        classify_event("wec", "24 Hours of Le Mans Hyperpole Qualifying – LMP2 & LMGT3", 3)
        is SessionType.HYPERPOLE
    )
    assert (
        classify_event("wec", "24 Hours of Le Mans Hyperpole Qualifying – Hypercar", 3)
        is SessionType.HYPERPOLE
    )


def test_wec_class_split_qualifying_is_plain_qualifying():
    assert (
        classify_event("wec", "6 Hours of Spa Francorchamps Qualifying - LMGT3", 2)
        is SessionType.QUALIFYING
    )
    assert (
        classify_event("wec", "6 Hours of Spa Francorchamps Qualifying - Hypercar", 2)
        is SessionType.QUALIFYING
    )
    assert classify_event("wec", "6 Hours of Imola Qualifying", 1) is SessionType.QUALIFYING


def test_wec_practice():
    assert classify_event("wec", "6 Hours of Imola Free Practice 3", 1) is SessionType.PRACTICE
    assert (
        classify_event("wec", "24 Hours of Le Mans Free Practice 1", 3) is SessionType.PRACTICE
    )


def test_wec_bare_name_is_race_via_positive_rule():
    assert classify_event("wec", "6 Hours of Imola", 1) is SessionType.RACE
    assert classify_event("wec", "24 Hours of Le Mans", 3) is SessionType.RACE


def test_wec_unrecognized_name_is_unknown_not_race():
    assert classify_event("wec", "Drivers Parade", 1) is SessionType.UNKNOWN


def test_indycar_is_race_only_series():
    assert (
        classify_event("indycar", "Firestone Grand Prix of St. Petersburg", 1)
        is SessionType.RACE
    )


def test_imsa_is_race_only_series():
    assert classify_event("imsa", "Rolex 24 At DAYTONA", 1) is SessionType.RACE
    assert classify_event("imsa", "Mobil 1 Twelve Hours of Sebring", 2) is SessionType.RACE
    assert classify_event("imsa", "Acura Grand Prix of Long Beach", 3) is SessionType.RACE


def test_unconfigured_series_is_always_unknown():
    assert classify_event("some_future_series", "Anything", 1) is SessionType.UNKNOWN
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_classify.py -v`
Expected: FAIL / collection error — `motorcal.classify` does not exist yet.

- [ ] **Step 3: Write `src/motorcal/classify.py`**

```python
"""Per-series, ordered-regex session classification. Pure function, no I/O."""
from __future__ import annotations

import re

from motorcal.models import SessionType

_TESTING_ROUND = 500

_F1_RULES: list[tuple[re.Pattern[str], SessionType]] = [
    (re.compile(r"sprint qualifying", re.IGNORECASE), SessionType.SPRINT_QUALIFYING),
    (re.compile(r"sprint", re.IGNORECASE), SessionType.SPRINT),
    (re.compile(r"practice", re.IGNORECASE), SessionType.PRACTICE),
    (re.compile(r"qualifying", re.IGNORECASE), SessionType.QUALIFYING),
    (re.compile(r"^.+ grand prix$", re.IGNORECASE), SessionType.RACE),
]

_WEC_RULES: list[tuple[re.Pattern[str], SessionType]] = [
    (re.compile(r"hyperpole", re.IGNORECASE), SessionType.HYPERPOLE),
    (re.compile(r"qualifying", re.IGNORECASE), SessionType.QUALIFYING),
    (re.compile(r"practice", re.IGNORECASE), SessionType.PRACTICE),
    (re.compile(r"^\d+ hours? of .+$", re.IGNORECASE), SessionType.RACE),
]

_SERIES_RULES: dict[str, list[tuple[re.Pattern[str], SessionType]]] = {
    "f1": _F1_RULES,
    "wec": _WEC_RULES,
}

# Series that expose only race-level events from the provider (no practice/qualifying
# breakdown exists in the source data). Every event in one of these series is a race,
# except round 500 (still testing, checked first, unconditionally).
_RACE_ONLY_SERIES = {"indycar", "imsa"}


def classify_event(series: str, name: str, round_number: int) -> SessionType:
    """Classify one event's session type from its series, name, and round number."""
    if round_number == _TESTING_ROUND:
        return SessionType.TESTING

    if series in _RACE_ONLY_SERIES:
        return SessionType.RACE

    rules = _SERIES_RULES.get(series)
    if rules is None:
        return SessionType.UNKNOWN

    for pattern, session_type in rules:
        if pattern.search(name):
            return session_type

    return SessionType.UNKNOWN
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_classify.py -v`
Expected: PASS, 15 passed.

- [ ] **Step 5: Commit**

```bash
git add src/motorcal/classify.py tests/test_classify.py
git commit -m "Add per-series ordered-regex session classification"
```

---

### Task 2: Scope queries and snapshot metadata in `store.py`

**Files:**
- Modify: `src/motorcal/store.py`
- Test: `tests/test_store_scope_queries.py`

**Interfaces:**
- Consumes: `connect`, `init_schema`, `transaction`, `upsert_source_event` from Phase 2.
- Produces (used by Task 3):
  - `def list_source_events_by_scope(conn: sqlite3.Connection, provider: str, series: str, season: str) -> list[sqlite3.Row]`.
  - `def get_snapshot_meta(conn: sqlite3.Connection, provider: str, series: str, season: str) -> sqlite3.Row | None`.
  - `def upsert_snapshot_meta(conn: sqlite3.Connection, provider: str, series: str, season: str, last_complete_at: str, last_event_count: int) -> None` — insert-or-replace by `(provider, series, season)`.
  - `def mark_source_event_disappeared(conn: sqlite3.Connection, provider: str, id_event: str, disappeared_at: str) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_scope_queries.py
from motorcal.store import (
    connect,
    get_snapshot_meta,
    init_schema,
    list_source_events_by_scope,
    mark_source_event_disappeared,
    transaction,
    upsert_snapshot_meta,
    upsert_source_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert(conn, id_event, series="wec", season="2026", seen_at="2026-07-29T00:00:00+00:00"):
    upsert_source_event(
        conn,
        provider="thesportsdb",
        id_event=id_event,
        series=series,
        season=season,
        round=1,
        name=f"Event {id_event}",
        date="2026-04-19",
        time="00:00:00",
        venue="Venue",
        country="Country",
        raw_json="{}",
        seen_at=seen_at,
    )


def test_list_source_events_by_scope_filters_correctly(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "1", series="wec", season="2026")
        _insert(conn, "2", series="wec", season="2026")
        _insert(conn, "3", series="wec", season="2027")  # different season
        _insert(conn, "4", series="f1", season="2026")  # different series

    rows = list_source_events_by_scope(conn, "thesportsdb", "wec", "2026")
    ids = {row["id_event"] for row in rows}
    assert ids == {"1", "2"}


def test_list_source_events_by_scope_empty_when_none_match(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert list_source_events_by_scope(conn, "thesportsdb", "wec", "2026") == []


def test_snapshot_meta_round_trip(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_snapshot_meta(conn, "thesportsdb", "wec", "2026") is None

    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", "2026", "2026-07-29T00:00:00+00:00", 5)

    row = get_snapshot_meta(conn, "thesportsdb", "wec", "2026")
    assert row["last_complete_at"] == "2026-07-29T00:00:00+00:00"
    assert row["last_event_count"] == 5


def test_snapshot_meta_upsert_replaces_previous_values(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", "2026", "2026-07-29T00:00:00+00:00", 5)
    with transaction(conn):
        upsert_snapshot_meta(conn, "thesportsdb", "wec", "2026", "2026-08-01T00:00:00+00:00", 7)

    row = get_snapshot_meta(conn, "thesportsdb", "wec", "2026")
    assert row["last_complete_at"] == "2026-08-01T00:00:00+00:00"
    assert row["last_event_count"] == 7


def test_mark_source_event_disappeared(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "1")

    with transaction(conn):
        mark_source_event_disappeared(conn, "thesportsdb", "1", "2026-08-01T00:00:00+00:00")

    rows = list_source_events_by_scope(conn, "thesportsdb", "wec", "2026")
    assert rows[0]["disappeared_at"] == "2026-08-01T00:00:00+00:00"


def test_upsert_source_event_reactivates_a_disappeared_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "1", seen_at="t1")
    with transaction(conn):
        mark_source_event_disappeared(conn, "thesportsdb", "1", "t2")

    rows = list_source_events_by_scope(conn, "thesportsdb", "wec", "2026")
    assert rows[0]["disappeared_at"] == "t2"

    with transaction(conn):
        _insert(conn, "1", seen_at="t3")  # reappears

    rows = list_source_events_by_scope(conn, "thesportsdb", "wec", "2026")
    assert rows[0]["disappeared_at"] is None  # reactivated (Phase 2's existing ON CONFLICT clause)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_scope_queries.py -v`
Expected: FAIL / collection error — `list_source_events_by_scope`, `get_snapshot_meta`, `upsert_snapshot_meta`, `mark_source_event_disappeared` do not exist yet.

- [ ] **Step 3: Append to `src/motorcal/store.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_scope_queries.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-3 (119) plus Task 1 (15) plus Task 2 (6) pass — 140 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/store.py tests/test_store_scope_queries.py
git commit -m "Add scope queries, snapshot metadata, and disappearance marking to store.py"
```

---

### Task 3: `ingest_snapshot` — the commit/discard decision

**Files:**
- Modify: `src/motorcal/store.py`
- Test: `tests/test_store_ingest.py`

**Interfaces:**
- Consumes: `transaction`, `upsert_source_event`, `list_source_events_by_scope`, `get_snapshot_meta`, `upsert_snapshot_meta`, `mark_source_event_disappeared` from Task 2. Consumes `SnapshotResult`/`ProviderEvent` from `motorcal.providers.thesportsdb` (Phase 3) purely as type hints — this function does not fetch anything itself.
- Produces (used by Phase 9's scheduler):
  - `@dataclass class IngestResult` fields: `committed: bool`, `reason: str | None`, `events_written: int`.
  - `def ingest_snapshot(conn: sqlite3.Connection, snapshot: SnapshotResult, *, provider: str, series: str, season: str, now: str, is_current_season: bool) -> IngestResult` — implements the complete/suspicious-empty/disappearance contract described in Global Constraints, entirely inside one `transaction()` call when the snapshot is committed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_ingest.py
import json

from motorcal.providers.thesportsdb import ProviderEvent, SnapshotResult
from motorcal.store import (
    connect,
    get_source_event,
    init_schema,
    ingest_snapshot,
    transaction,
    upsert_source_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _event(id_event, name="Event", round_number=1, season="2026", series="wec"):
    return ProviderEvent(
        id_event=id_event,
        name=name,
        date="2026-04-19",
        time="00:00:00",
        round=round_number,
        season=season,
        series=series,
        venue="Venue",
        country="Country",
        raw={"idEvent": id_event},
    )


def test_incomplete_snapshot_is_discarded_in_full(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="Original", date="2026-04-19", time="00:00:00",
            venue="V", country="C", raw_json="{}", seen_at="t0",
        )

    snapshot = SnapshotResult(complete=False, events=[_event("1", name="Changed")], diagnostics=["round 2: boom"], rounds_attempted=2, rounds_failed=1)
    result = ingest_snapshot(
        conn, snapshot, provider="thesportsdb", series="wec", season="2026",
        now="t1", is_current_season=True,
    )

    assert result.committed is False
    assert result.reason == "incomplete_snapshot"
    assert result.events_written == 0
    row = get_source_event(conn, "thesportsdb", "1")
    assert row["name"] == "Original"  # untouched


def test_complete_snapshot_with_events_is_committed(tmp_path):
    conn = _fresh_conn(tmp_path)
    snapshot = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)

    result = ingest_snapshot(
        conn, snapshot, provider="thesportsdb", series="wec", season="2026",
        now="t1", is_current_season=True,
    )

    assert result.committed is True
    assert result.reason is None
    assert result.events_written == 2
    assert get_source_event(conn, "thesportsdb", "1") is not None
    assert get_source_event(conn, "thesportsdb", "2") is not None


def test_disappearance_marks_source_event_but_does_not_delete_it(tmp_path):
    conn = _fresh_conn(tmp_path)
    first = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    ingest_snapshot(conn, first, provider="thesportsdb", series="wec", season="2026", now="t1", is_current_season=True)

    second = SnapshotResult(complete=True, events=[_event("1")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    result = ingest_snapshot(conn, second, provider="thesportsdb", series="wec", season="2026", now="t2", is_current_season=True)

    assert result.committed is True
    row1 = get_source_event(conn, "thesportsdb", "1")
    row2 = get_source_event(conn, "thesportsdb", "2")
    assert row1["disappeared_at"] is None
    assert row2["disappeared_at"] == "t2"  # marked, not deleted


def test_reappearance_reactivates_the_same_source_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    first = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    ingest_snapshot(conn, first, provider="thesportsdb", series="wec", season="2026", now="t1", is_current_season=True)
    second = SnapshotResult(complete=True, events=[_event("1")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    ingest_snapshot(conn, second, provider="thesportsdb", series="wec", season="2026", now="t2", is_current_season=True)

    third = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    result = ingest_snapshot(conn, third, provider="thesportsdb", series="wec", season="2026", now="t3", is_current_season=True)

    assert result.committed is True
    row2 = get_source_event(conn, "thesportsdb", "2")
    assert row2["disappeared_at"] is None  # reactivated


def test_incomplete_snapshot_does_not_touch_disappearance_state(tmp_path):
    conn = _fresh_conn(tmp_path)
    first = SnapshotResult(complete=True, events=[_event("1"), _event("2")], diagnostics=[], rounds_attempted=2, rounds_failed=0)
    ingest_snapshot(conn, first, provider="thesportsdb", series="wec", season="2026", now="t1", is_current_season=True)

    incomplete = SnapshotResult(complete=False, events=[_event("1")], diagnostics=["round 2: boom"], rounds_attempted=2, rounds_failed=1)
    ingest_snapshot(conn, incomplete, provider="thesportsdb", series="wec", season="2026", now="t2", is_current_season=True)

    row2 = get_source_event(conn, "thesportsdb", "2")
    assert row2["disappeared_at"] is None  # not marked — incomplete snapshots never touch disappearance


def test_empty_snapshot_for_current_season_is_always_suspicious(tmp_path):
    conn = _fresh_conn(tmp_path)
    empty = SnapshotResult(complete=True, events=[], diagnostics=[], rounds_attempted=5, rounds_failed=0)

    result = ingest_snapshot(
        conn, empty, provider="thesportsdb", series="wec", season="2026",
        now="t1", is_current_season=True,
    )

    assert result.committed is False
    assert result.reason == "suspicious_empty_current_season"


def test_empty_snapshot_for_brand_new_future_season_is_accepted(tmp_path):
    conn = _fresh_conn(tmp_path)
    empty = SnapshotResult(complete=True, events=[], diagnostics=[], rounds_attempted=5, rounds_failed=0)

    result = ingest_snapshot(
        conn, empty, provider="thesportsdb", series="wec", season="2027",
        now="t1", is_current_season=False,
    )

    assert result.committed is True
    assert result.events_written == 0


def test_empty_snapshot_for_previously_populated_future_season_is_suspicious(tmp_path):
    conn = _fresh_conn(tmp_path)
    populated = SnapshotResult(complete=True, events=[_event("1", season="2027")], diagnostics=[], rounds_attempted=5, rounds_failed=0)
    ingest_snapshot(conn, populated, provider="thesportsdb", series="wec", season="2027", now="t1", is_current_season=False)

    empty = SnapshotResult(complete=True, events=[], diagnostics=[], rounds_attempted=5, rounds_failed=0)
    result = ingest_snapshot(
        conn, empty, provider="thesportsdb", series="wec", season="2027",
        now="t2", is_current_season=False,
    )

    assert result.committed is False
    assert result.reason == "suspicious_empty_future_season"
    assert get_source_event(conn, "thesportsdb", "1") is not None  # untouched, not disappeared
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_ingest.py -v`
Expected: FAIL / collection error — `IngestResult`, `ingest_snapshot` do not exist yet.

- [ ] **Step 3: Append to `src/motorcal/store.py`**

Add `from dataclasses import dataclass` to the top imports if not already present (check first — Task 1 of Phase 2 did not need it, so it is likely not yet imported). Add `from motorcal.providers.thesportsdb import SnapshotResult` to the imports (this is a type-only dependency — `store.py` does not call any provider function). Append to the end of the file:

```python
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
```

Add `import json` to the top of `store.py` if not already present (check first).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_ingest.py -v`
Expected: PASS, 8 passed.

- [ ] **Step 5: Run the entire test suite**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-3 and Phase 4 Tasks 1-3 pass — 148 passed total (119 + 15 + 6 + 8).

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/store.py tests/test_store_ingest.py
git commit -m "Add ingest_snapshot: complete-snapshot, suspicious-empty, and disappearance contract"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: classification ordering rules including hyperpole-before-qualifying and sprint-qualifying-before-sprint/qualifying, round-500-is-always-testing, and the positive (not fallback) race rule (Classification section); disappearance reconciliation's source-layer half, complete-snapshot discard-in-full, and both suspicious-empty variants (current vs. future season) (Provider and snapshot semantics + Season and retention policy sections); one atomic transaction for the whole commit (Architecture section).
- Explicitly out of scope for this phase (later phases own them): translating `source_events.disappeared_at` into a published `CANCELLED` event with its 90-day retention window (Phase 6); the 180-day historical pruning of past events (Phase 6); reactivation with a *different* source ID being treated as a brand-new event rather than the same one (that's a Phase 6 merge-layer decision about published UIDs, not a source-layer concern — this phase's `upsert_source_event` correctly never conflates two different `id_event` values, since the primary key is `(provider, id_event)`); determining whether a season is "current" or "future" (Phase 9, which owns the scheduler and knows today's date and `next_season_from`).
- Type consistency check: `ingest_snapshot` takes a `providers.thesportsdb.SnapshotResult` by type but never calls any provider function — this keeps the dependency one-directional (store → provider types only, never provider → store), matching Phase 3's explicit "no dependency on store.py" design. `classify_event`'s `series` parameter and `ProviderEvent.series` (Phase 3) are the same config series key (e.g. `"wec"`) — Phase 6 will call `classify_event(row["series"], row["name"], row["round"])` directly against `source_events` columns without needing any translation.
