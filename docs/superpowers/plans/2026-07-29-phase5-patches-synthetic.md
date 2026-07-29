# Motorsports Calendar — Phase 5: Patch and Synthetic-Event Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the patch-matching validation logic (in a new `src/motorcal/merge.py`) and synthetic-event storage/reconciliation (extending `src/motorcal/store.py`), so that an `overrides.yaml` bundle's patches can be validated against real source data (exactly-one-match rule) and its synthetic events can be tracked against what was previously configured (to detect removal).

**Architecture:** `merge.py` is introduced in this phase as a pure-logic module operating on `motorcal.models.SourceEvent` and `motorcal.config.PatchConfig`/`SyntheticEventConfig` — it never touches the database directly. `store.py` gains CRUD for the `synthetic_events` table (already schema'd in Phase 2) plus a reconciliation function that calls both `store.py`'s new CRUD and `merge.py`'s pure comparison helpers. This phase does NOT implement the full publication rebuild (fingerprint, sequence, duration/alarm resolution, or turning a disappeared/removed item into a published `CANCELLED` event) — those are Phase 6. This phase only validates and reconciles; `merge.py` will grow substantially in Phase 6.

**Tech Stack:** No new dependencies. Reuses `motorcal.models.SourceEvent` (Phase 1), `motorcal.config.PatchConfig`/`SyntheticEventConfig`/`parse_duration` (Phase 1), and `motorcal.store` (Phases 2/4).

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous.
- Every patch must match **exactly one** source event. Zero or multiple matches are validation errors — this phase produces that error list; deciding what to do with an invalid bundle (keep the previously valid published configuration active) is a caller concern (Phase 9), not this phase's.
- Patch matching: prefer `id_event` (matched by `SourceEventKey.id_event` alone — there is currently only one provider, `thesportsdb`, so provider is not part of the match key here); otherwise use the fallback matcher `{series, date, contains}`, where `contains` is a case-insensitive substring match against the event name and `date`/`series` are exact matches against `SourceEvent.date`/`SourceEvent.series`.
- Synthetic events require an immutable, user-selected `uid` — this phase must never derive or infer a UID from mutable fields; it always uses `SyntheticEventConfig.uid` verbatim as given.
- Detecting that a previously-configured synthetic event has been removed from `overrides.yaml` is this phase's job (mirroring Phase 4's `source_events.disappeared_at` pattern): mark it so a later phase can act on it. This phase does **not** decide the published consequence (turning it into a `CANCELLED` VEVENT, retention windows, or the separate explicit purge action) — that's Phase 6's cancellation lifecycle.
- No pip: dependency management is `uv` only.

---

### Task 1: Patch matching validation (`merge.py`)

**Files:**
- Create: `src/motorcal/merge.py`
- Test: `tests/test_merge_patches.py`

**Interfaces:**
- Consumes: `SourceEvent`, `SourceEventKey` from `motorcal.models` (Phase 1); `PatchConfig`, `PatchMatcher` from `motorcal.config` (Phase 1).
- Produces (used by Phase 6 and Phase 9):
  - `@dataclass class PatchMatchError` fields: `patch: PatchConfig`, `reason: str` (one of `"no_match"`, `"multiple_matches"`), `candidate_count: int`.
  - `@dataclass class MatchedPatch` fields: `patch: PatchConfig`, `source_event: SourceEvent`.
  - `def match_all_patches(patches: list[PatchConfig], source_events: list[SourceEvent]) -> tuple[list[MatchedPatch], list[PatchMatchError]]` — for each patch, finds candidates (by `id_event` if set, else by the `{series, date, contains}` fallback matcher) and classifies the result as a match (exactly one candidate) or an error (zero or multiple candidates). Returns both lists; a caller checks whether `PatchMatchError` list is empty to decide bundle validity.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_merge_patches.py
from motorcal.config import PatchConfig, PatchMatcher
from motorcal.merge import MatchedPatch, PatchMatchError, match_all_patches
from motorcal.models import SourceEvent, SourceEventKey

WEC_RACE = SourceEvent(
    key=SourceEventKey(provider="thesportsdb", id_event="2421035"),
    series="wec",
    season="2026",
    round=1,
    name="6 Hours of Imola",
    date="2026-04-19",
    time="00:00:00",
    venue="Imola",
    country="Italy",
    raw={},
)
WEC_QUALIFYING = SourceEvent(
    key=SourceEventKey(provider="thesportsdb", id_event="2421036"),
    series="wec",
    season="2026",
    round=1,
    name="6 Hours of Imola Qualifying",
    date="2026-04-18",
    time="12:30:00",
    venue="Imola",
    country="Italy",
    raw={},
)
F1_RACE = SourceEvent(
    key=SourceEventKey(provider="thesportsdb", id_event="9999"),
    series="f1",
    season="2026",
    round=1,
    name="Australian Grand Prix",
    date="2026-03-08",
    time="04:00:00",
    venue="Melbourne",
    country="Australia",
    raw={},
)

ALL_EVENTS = [WEC_RACE, WEC_QUALIFYING, F1_RACE]


def test_id_event_patch_matches_exactly_one_event():
    patch = PatchConfig(id_event="2421035", start="2026-04-19T13:00:00Z", duration="6h", note="official WEC timetable")
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert errors == []
    assert len(matches) == 1
    assert matches[0].patch is patch
    assert matches[0].source_event is WEC_RACE


def test_fallback_matcher_matches_exactly_one_event():
    patch = PatchConfig(match=PatchMatcher(series="wec", date="2026-04-19", contains="Imola"))
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert errors == []
    assert len(matches) == 1
    assert matches[0].source_event is WEC_RACE


def test_fallback_matcher_contains_is_case_insensitive():
    patch = PatchConfig(match=PatchMatcher(series="wec", date="2026-04-19", contains="imola"))
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert errors == []
    assert len(matches) == 1


def test_id_event_patch_with_no_match_is_a_validation_error():
    patch = PatchConfig(id_event="does-not-exist")
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert matches == []
    assert len(errors) == 1
    assert errors[0].reason == "no_match"
    assert errors[0].candidate_count == 0


def test_fallback_matcher_with_no_match_is_a_validation_error():
    patch = PatchConfig(match=PatchMatcher(series="wec", date="2099-01-01", contains="Imola"))
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert matches == []
    assert len(errors) == 1
    assert errors[0].reason == "no_match"


def test_fallback_matcher_with_multiple_matches_is_a_validation_error():
    duplicate = SourceEvent(
        key=SourceEventKey(provider="thesportsdb", id_event="2421035-dup"),
        series="wec",
        season="2026",
        round=1,
        name="6 Hours of Imola (Rescheduled)",
        date="2026-04-19",
        time="00:00:00",
        venue="Imola",
        country="Italy",
        raw={},
    )
    patch = PatchConfig(match=PatchMatcher(series="wec", date="2026-04-19", contains="Imola"))
    matches, errors = match_all_patches([patch], [WEC_RACE, duplicate])

    assert matches == []
    assert len(errors) == 1
    assert errors[0].reason == "multiple_matches"
    assert errors[0].candidate_count == 2


def test_multiple_valid_patches_all_match_independently():
    patch_a = PatchConfig(id_event="2421035", note="a")
    patch_b = PatchConfig(match=PatchMatcher(series="f1", date="2026-03-08", contains="Grand Prix"))
    matches, errors = match_all_patches([patch_a, patch_b], ALL_EVENTS)

    assert errors == []
    assert len(matches) == 2
    matched_ids = {m.source_event.key.id_event for m in matches}
    assert matched_ids == {"2421035", "9999"}


def test_one_bad_patch_does_not_prevent_others_from_matching():
    good_patch = PatchConfig(id_event="2421035")
    bad_patch = PatchConfig(id_event="does-not-exist")
    matches, errors = match_all_patches([good_patch, bad_patch], ALL_EVENTS)

    assert len(matches) == 1
    assert matches[0].patch is good_patch
    assert len(errors) == 1
    assert errors[0].patch is bad_patch


def test_no_patches_returns_empty_results():
    matches, errors = match_all_patches([], ALL_EVENTS)
    assert matches == []
    assert errors == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_merge_patches.py -v`
Expected: FAIL / collection error — `motorcal.merge` does not exist yet.

- [ ] **Step 3: Write `src/motorcal/merge.py`**

```python
"""Publication rebuild logic: patch matching (this phase) and, in a later phase,
fingerprinting, sequencing, duration/alarm resolution, and cancellation lifecycle."""
from __future__ import annotations

from dataclasses import dataclass

from motorcal.config import PatchConfig
from motorcal.models import SourceEvent


@dataclass
class PatchMatchError:
    """A patch that did not match exactly one source event."""

    patch: PatchConfig
    reason: str  # "no_match" or "multiple_matches"
    candidate_count: int


@dataclass
class MatchedPatch:
    """A patch successfully paired with the single source event it modifies."""

    patch: PatchConfig
    source_event: SourceEvent


def _find_candidates(patch: PatchConfig, source_events: list[SourceEvent]) -> list[SourceEvent]:
    if patch.id_event is not None:
        return [e for e in source_events if e.key.id_event == patch.id_event]

    matcher = patch.match
    assert matcher is not None  # config-schema validation (Phase 1) guarantees exactly one is set
    needle = matcher.contains.lower()
    return [
        e
        for e in source_events
        if e.series == matcher.series and e.date == matcher.date and needle in e.name.lower()
    ]


def match_all_patches(
    patches: list[PatchConfig], source_events: list[SourceEvent]
) -> tuple[list[MatchedPatch], list[PatchMatchError]]:
    """Match every patch against source_events, requiring exactly one candidate each."""
    matches: list[MatchedPatch] = []
    errors: list[PatchMatchError] = []

    for patch in patches:
        candidates = _find_candidates(patch, source_events)
        if len(candidates) == 1:
            matches.append(MatchedPatch(patch=patch, source_event=candidates[0]))
        elif len(candidates) == 0:
            errors.append(PatchMatchError(patch=patch, reason="no_match", candidate_count=0))
        else:
            errors.append(
                PatchMatchError(
                    patch=patch, reason="multiple_matches", candidate_count=len(candidates)
                )
            )

    return matches, errors
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_merge_patches.py -v`
Expected: PASS, 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/motorcal/merge.py tests/test_merge_patches.py
git commit -m "Add patch matching validation (exactly-one-match rule)"
```

---

### Task 2: Synthetic-event storage (`store.py`)

**Files:**
- Modify: `src/motorcal/store.py`
- Test: `tests/test_store_synthetic_events.py`

**Interfaces:**
- Consumes: `connect`, `init_schema`, `transaction` from Phase 2. Uses the existing `synthetic_events` table schema (created in Phase 2: `uid, series, summary, start, date, duration_seconds, location, status, note, alarms_json, present_in_config, cancelled_at, purged`).
- Produces (used by Task 3):
  - `def upsert_synthetic_event(conn: sqlite3.Connection, *, uid: str, series: str, summary: str, start: str | None, date: str | None, duration_seconds: int | None, location: str | None, status: str, note: str | None, alarms_json: str) -> None` — insert-or-replace by `uid`; always sets `present_in_config = 1` and clears `cancelled_at` back to `NULL` (a synthetic event reappearing in config after being removed is reactivated, mirroring `upsert_source_event`'s behavior from Phase 2).
  - `def get_synthetic_event(conn: sqlite3.Connection, uid: str) -> sqlite3.Row | None`.
  - `def list_synthetic_events(conn: sqlite3.Connection) -> list[sqlite3.Row]`.
  - `def mark_synthetic_event_removed(conn: sqlite3.Connection, uid: str, removed_at: str) -> None` — sets `present_in_config = 0` and `cancelled_at = removed_at`, but only if `cancelled_at` is currently `NULL` (never overwrites an existing cancellation timestamp).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_synthetic_events.py
from motorcal.store import (
    connect,
    get_synthetic_event,
    init_schema,
    list_synthetic_events,
    mark_synthetic_event_removed,
    transaction,
    upsert_synthetic_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert(conn, uid, **overrides):
    defaults = dict(
        uid=uid,
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00+00:00",
        date=None,
        duration_seconds=24 * 3600,
        location=None,
        status="CONFIRMED",
        note="official IMSA timetable",
        alarms_json="[]",
    )
    defaults.update(overrides)
    upsert_synthetic_event(conn, **defaults)


def test_upsert_and_get_synthetic_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "imsa-2026-rolex-24")

    row = get_synthetic_event(conn, "imsa-2026-rolex-24")
    assert row["summary"] == "Rolex 24 at Daytona"
    assert row["duration_seconds"] == 24 * 3600
    assert row["present_in_config"] == 1
    assert row["cancelled_at"] is None


def test_get_synthetic_event_returns_none_when_missing(tmp_path):
    conn = _fresh_conn(tmp_path)
    assert get_synthetic_event(conn, "does-not-exist") is None


def test_list_synthetic_events_returns_all_rows(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "uid-1")
        _insert(conn, "uid-2")

    rows = list_synthetic_events(conn)
    assert {row["uid"] for row in rows} == {"uid-1", "uid-2"}


def test_mark_synthetic_event_removed_sets_flags(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "uid-1")

    with transaction(conn):
        mark_synthetic_event_removed(conn, "uid-1", "2026-08-01T00:00:00+00:00")

    row = get_synthetic_event(conn, "uid-1")
    assert row["present_in_config"] == 0
    assert row["cancelled_at"] == "2026-08-01T00:00:00+00:00"


def test_mark_synthetic_event_removed_does_not_overwrite_existing_cancellation(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "uid-1")
    with transaction(conn):
        mark_synthetic_event_removed(conn, "uid-1", "2026-08-01T00:00:00+00:00")
    with transaction(conn):
        mark_synthetic_event_removed(conn, "uid-1", "2026-09-01T00:00:00+00:00")  # should be a no-op

    row = get_synthetic_event(conn, "uid-1")
    assert row["cancelled_at"] == "2026-08-01T00:00:00+00:00"  # unchanged


def test_upsert_reactivates_a_removed_synthetic_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "uid-1")
    with transaction(conn):
        mark_synthetic_event_removed(conn, "uid-1", "2026-08-01T00:00:00+00:00")

    row = get_synthetic_event(conn, "uid-1")
    assert row["present_in_config"] == 0
    assert row["cancelled_at"] == "2026-08-01T00:00:00+00:00"

    with transaction(conn):
        _insert(conn, "uid-1")  # reappears in config

    row = get_synthetic_event(conn, "uid-1")
    assert row["present_in_config"] == 1
    assert row["cancelled_at"] is None  # reactivated
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_synthetic_events.py -v`
Expected: FAIL / collection error — `upsert_synthetic_event`, `get_synthetic_event`, `list_synthetic_events`, `mark_synthetic_event_removed` do not exist yet.

- [ ] **Step 3: Append to `src/motorcal/store.py`**

```python
def upsert_synthetic_event(
    conn: sqlite3.Connection,
    *,
    uid: str,
    series: str,
    summary: str,
    start: str | None,
    date: str | None,
    duration_seconds: int | None,
    location: str | None,
    status: str,
    note: str | None,
    alarms_json: str,
) -> None:
    """Insert or fully replace a synthetic event by uid. Reactivates if previously removed."""
    conn.execute(
        """
        INSERT INTO synthetic_events
            (uid, series, summary, start, date, duration_seconds, location, status, note,
             alarms_json, present_in_config, cancelled_at, purged)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, 0)
        ON CONFLICT (uid) DO UPDATE SET
            series = excluded.series,
            summary = excluded.summary,
            start = excluded.start,
            date = excluded.date,
            duration_seconds = excluded.duration_seconds,
            location = excluded.location,
            status = excluded.status,
            note = excluded.note,
            alarms_json = excluded.alarms_json,
            present_in_config = 1,
            cancelled_at = NULL
        """,
        (uid, series, summary, start, date, duration_seconds, location, status, note, alarms_json),
    )


def get_synthetic_event(conn: sqlite3.Connection, uid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM synthetic_events WHERE uid = ?", (uid,)).fetchone()


def list_synthetic_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM synthetic_events").fetchall()


def mark_synthetic_event_removed(conn: sqlite3.Connection, uid: str, removed_at: str) -> None:
    conn.execute(
        """
        UPDATE synthetic_events
        SET present_in_config = 0, cancelled_at = ?
        WHERE uid = ? AND cancelled_at IS NULL
        """,
        (removed_at, uid),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_synthetic_events.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-4 (148) plus Task 1 (9) plus Task 2 (6) pass — 163 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/store.py tests/test_store_synthetic_events.py
git commit -m "Add synthetic_events CRUD with removal marking and reactivation"
```

---

### Task 3: Synthetic-event reconciliation orchestration

**Files:**
- Modify: `src/motorcal/merge.py`
- Test: `tests/test_merge_synthetic_reconcile.py`

**Interfaces:**
- Consumes: `SyntheticEventConfig` from `motorcal.config` (Phase 1), `parse_duration` from `motorcal.config` (Phase 1); `upsert_synthetic_event`, `list_synthetic_events`, `mark_synthetic_event_removed`, `transaction` from `motorcal.store` (Task 2).
- Produces (used by Phase 9):
  - `def reconcile_synthetic_events(conn: sqlite3.Connection, synthetic_configs: list[SyntheticEventConfig], now: str) -> None` — upserts every currently-configured synthetic event (reactivating any that were previously removed), then marks as removed any stored synthetic event whose `uid` is no longer present in `synthetic_configs` and is not already cancelled. Runs entirely inside one `store.transaction()` call.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_merge_synthetic_reconcile.py
from motorcal.config import SyntheticEventConfig
from motorcal.merge import reconcile_synthetic_events
from motorcal.store import connect, get_synthetic_event, init_schema, list_synthetic_events


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_reconcile_creates_new_synthetic_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24",
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z",
        duration="24h",
        note="official IMSA timetable",
    )

    reconcile_synthetic_events(conn, [cfg], now="t1")

    row = get_synthetic_event(conn, "imsa-2026-rolex-24")
    assert row is not None
    assert row["summary"] == "Rolex 24 at Daytona"
    assert row["duration_seconds"] == 24 * 3600
    assert row["present_in_config"] == 1


def test_reconcile_marks_removed_events_as_no_longer_configured(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24", series="imsa", summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z", duration="24h",
    )
    reconcile_synthetic_events(conn, [cfg], now="t1")

    reconcile_synthetic_events(conn, [], now="t2")  # config no longer declares it

    row = get_synthetic_event(conn, "imsa-2026-rolex-24")
    assert row["present_in_config"] == 0
    assert row["cancelled_at"] == "t2"


def test_reconcile_reactivates_a_previously_removed_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24", series="imsa", summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z", duration="24h",
    )
    reconcile_synthetic_events(conn, [cfg], now="t1")
    reconcile_synthetic_events(conn, [], now="t2")  # removed

    reconcile_synthetic_events(conn, [cfg], now="t3")  # re-added

    row = get_synthetic_event(conn, "imsa-2026-rolex-24")
    assert row["present_in_config"] == 1
    assert row["cancelled_at"] is None


def test_reconcile_does_not_touch_unrelated_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg_a = SyntheticEventConfig(
        uid="event-a", series="imsa", summary="A", start="2026-01-01T00:00:00Z",
    )
    cfg_b = SyntheticEventConfig(
        uid="event-b", series="wec", summary="B", start="2026-02-01T00:00:00Z",
    )
    reconcile_synthetic_events(conn, [cfg_a, cfg_b], now="t1")

    reconcile_synthetic_events(conn, [cfg_a], now="t2")  # only B removed

    row_a = get_synthetic_event(conn, "event-a")
    row_b = get_synthetic_event(conn, "event-b")
    assert row_a["present_in_config"] == 1
    assert row_a["cancelled_at"] is None
    assert row_b["present_in_config"] == 0
    assert row_b["cancelled_at"] == "t2"


def test_reconcile_with_date_only_synthetic_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="event-date-only", series="wec", summary="Test Event", date="2026-06-01",
    )
    reconcile_synthetic_events(conn, [cfg], now="t1")

    row = get_synthetic_event(conn, "event-date-only")
    assert row["date"] == "2026-06-01"
    assert row["start"] is None


def test_reconcile_with_alarms_and_no_duration(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="event-with-alarms", series="wec", summary="Test Event",
        start="2026-06-01T10:00:00Z", alarms=["-1d", "-30m"],
    )
    reconcile_synthetic_events(conn, [cfg], now="t1")

    row = get_synthetic_event(conn, "event-with-alarms")
    assert row["duration_seconds"] is None
    assert row["alarms_json"] == '["-1d", "-30m"]'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_merge_synthetic_reconcile.py -v`
Expected: FAIL / collection error — `reconcile_synthetic_events` does not exist yet.

- [ ] **Step 3: Append to `src/motorcal/merge.py`**

Add these imports to the top of `src/motorcal/merge.py` (alongside the existing ones):

```python
import json
import sqlite3

from motorcal.config import SyntheticEventConfig, parse_duration
from motorcal.store import (
    list_synthetic_events,
    mark_synthetic_event_removed,
    transaction,
    upsert_synthetic_event,
)
```

Append to the end of the file:

```python
def reconcile_synthetic_events(
    conn: sqlite3.Connection, synthetic_configs: list[SyntheticEventConfig], now: str
) -> None:
    """Sync configured synthetic events into storage, marking removed ones.

    Every event currently in synthetic_configs is upserted (reactivating it if it
    was previously removed). Every stored synthetic event whose uid is NOT in
    synthetic_configs, and that is not already cancelled, is marked removed at `now`.
    """
    configured_uids = {cfg.uid for cfg in synthetic_configs}

    with transaction(conn):
        for cfg in synthetic_configs:
            upsert_synthetic_event(
                conn,
                uid=cfg.uid,
                series=cfg.series,
                summary=cfg.summary,
                start=cfg.start,
                date=cfg.date,
                duration_seconds=parse_duration(cfg.duration) if cfg.duration else None,
                location=cfg.location,
                status=cfg.status or "CONFIRMED",
                note=cfg.note,
                alarms_json=json.dumps(cfg.alarms),
            )

        for row in list_synthetic_events(conn):
            if row["uid"] not in configured_uids and row["cancelled_at"] is None:
                mark_synthetic_event_removed(conn, row["uid"], now)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_merge_synthetic_reconcile.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 5: Run the entire test suite**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-4 and Phase 5 Tasks 1-3 pass — 169 passed total (148 + 9 + 6 + 6). If the actual count differs, trust the test runner's real output over this number and note the discrepancy in your report.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/merge.py tests/test_merge_synthetic_reconcile.py
git commit -m "Add synthetic-event reconciliation (create, reactivate, mark removed)"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: exactly-one-match patch validation with `id_event` preferred over the `{series, date, contains}` fallback (Overrides and synthetic events → Patches section); immutable synthetic UIDs never derived from mutable fields (Synthetic events section, satisfied by construction — this phase always takes `uid` verbatim from config, never computes one); removal-detection mirroring the source-event disappearance pattern from Phase 4 (Synthetic events section, "Removing a future synthetic event first publishes it as cancelled...").
- Explicitly out of scope for this phase (Phase 6 owns them): applying a matched patch's field values (`start`, `time_confirmed`, `duration`, `summary`, `location`, `status`, `note`) onto a published event; turning `synthetic_events.cancelled_at` into an actual published `CANCELLED` VEVENT with a 90-day retention window; the separate explicit purge action that permanently removes a synthetic event after that retention window; fingerprint/sequence computation for any event, synthetic or source-backed.
- Type consistency check: `merge.py`'s `PatchMatchError`/`MatchedPatch` operate on `motorcal.models.SourceEvent` (not raw `sqlite3.Row`) — whatever calls `match_all_patches` in Phase 6 is responsible for converting `store.py`'s `source_events` rows into `SourceEvent` instances first (a small, obvious mapping: `SourceEvent(key=SourceEventKey(row["provider"], row["id_event"]), series=row["series"], season=row["season"], round=row["round"], name=row["name"], date=row["date"], time=row["time"], venue=row["venue"], country=row["country"], raw=json.loads(row["raw_json"]))`). `reconcile_synthetic_events`, by contrast, operates directly on `sqlite3.Row` via `store.py`'s own functions, since it never needs the `SourceEvent`/`models.py` shape at all — it works entirely in terms of the `synthetic_events` table's own columns.
