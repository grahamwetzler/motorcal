# Motorsports Calendar — Phase 6: Publication Rebuild, Fingerprint, Sequence, Retention, Cancellation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the full publication rebuild pipeline in `src/motorcal/merge.py`: fingerprint computation, sequence advancement, duration/alarm resolution, per-event `PublishedEvent` construction (for both source-backed and synthetic events), disappearance-to-cancellation translation, and retention pruning — all feeding `store.py`'s `published_events` table.

**Architecture:** This is the largest phase so far; it is split into 4 tasks so each has an independently testable deliverable. Tasks 1-2 are pure functions (no I/O) operating on `motorcal.models`/`motorcal.config` types — exactly like Phase 5's `merge.py` additions. Task 3 adds the handful of new `store.py` query/prune functions this phase needs. Task 4 is the orchestration that reads from the database, calls the pure functions, and writes back inside one transaction — this is where Phase 3's provider output, Phase 4's classification/source-storage, and Phase 5's patch/synthetic-event validation all finally converge into `published_events`.

**Tech Stack:** No new dependencies. Uses `hashlib` (stdlib) for fingerprinting.

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous. The "Merge and time handling" and "Canonical and published event model" sections are this phase's primary spec.
- Publication rebuild order: (1) complete committed source snapshot [Phase 4], (2) classification [Phase 4's `classify_event`], (3) validated patch [Phase 5's `match_all_patches`], (4) explicit cancellation/postponement state, (5) duration resolution, (6) alarm resolution, (7) gap/unknown-classification reporting. This phase implements steps 3-6 as part of building each `PublishedEvent`; step 7 (surfacing unknown-classification events prominently) is Phase 8's `/status` route concern, not this phase's — this phase just needs to not lose that information (an `UNKNOWN`-classified event is still built and published, per spec, just without an alarm/duration).
- Duration resolution priority: (1) the matched patch's or synthetic event's own `duration`, (2) an explicit per-series, per-session display duration (`SeriesConfig.durations`, added in Task 1 — this field does not exist yet), (3) an explicit global per-session display duration (`RootConfig.defaults.durations`, from Phase 1), (4) omitted (`None`). There is no default race duration at any tier.
- Time confirmation: source `00:00:00` (or a `None` time) is unconfirmed and renders as an all-day event (`all_day_date` set, `start` is `None`), the summary gains the configured `unknown_time.summary_suffix` (default `" (time TBC)"`), and it receives no alarms and no duration. An explicit patched `start` — including exact midnight — is confirmed unless the patch also sets `time_confirmed: false`.
- Alarms: only confirmed (timed), non-`unknown`/non-`testing` events get alarms. A synthetic event's own configured `alarms` list wins outright; a source-backed event uses `RootConfig.defaults.alerts[session_type]` (an empty list is a valid, deliberate "no alarms for this session type" configuration — e.g. the example config's `practice: []`).
- Fingerprinting: the fingerprint must cover every client-visible field — `summary`, `description`, `location`, `status`, `start`/`all_day_date`, `duration_seconds`, and the alarm set (order-independent — two events with the same alarms in a different order must fingerprint identically). Anything not listed here (e.g. internal bookkeeping like `first_seen_at`) must never affect the fingerprint.
- Sequence: `sequence = max(previous_sequence + 1, current_utc_unix_minute)` when the fingerprint changes; a brand-new event may start at the current UTC Unix minute. **Critically: if the newly computed fingerprint is identical to the previously stored one, `sequence`, `dtstamp`, and `last_modified` must all be left completely unchanged** — this is what makes repeated rebuilds of unchanged data produce byte-identical ICS output (Phase 7's determinism requirement depends on this).
- Cancellation vs. historical retention (source-backed events): a source event that disappeared from a complete snapshot (`source_events.disappeared_at` is set, from Phase 4) becomes a published `CANCELLED` event **only if its own scheduled time was still in the future or currently active at the moment the disappearance was first noticed** — a past event that disappears is left exactly as last published (spec: "remains last-known-good until normal historical pruning"), it does not get retroactively cancelled. Cancellation is **sticky**: once `CANCELLED`, an event must stay `CANCELLED` on every subsequent rebuild, even long after its own scheduled time has passed — the future-vs-past check only governs the *initial* cancel-or-leave-alone decision, never a re-evaluation on a later rebuild (see `PreviousPublishedState.status` above). A `CANCELLED` event stays in the feed for `retention.cancelled_after_event_days` (default 90) after its own scheduled end; a non-cancelled past event is retained for `retention.historical_days` (default 180) after its own scheduled end, then pruned (deleted, not cancelled).
- Synthetic-event cancellation: a synthetic event whose `synthetic_events.cancelled_at` is set (Phase 5's reconciliation, meaning it was removed from `overrides.yaml`) is published as `CANCELLED` the same way, using the same retention window. The separate "explicit purge action" that permanently deletes a long-cancelled synthetic event's row is a distinct, manually-invoked operation (this phase provides the primitives; CLI wiring is Phase 10).
- Patch field precedence for status: a matched patch may set `status` (e.g. `TENTATIVE` for a postponement) — this is applied first; automatic disappearance-driven `CANCELLED` is applied **after** and overrides a patch's `status` if both would otherwise apply to the same event (spec's step ordering: patch is step 3, cancellation state is step 4 — the later step wins for this one field).
- No pip: dependency management is `uv` only.

---

### Task 1: Per-series duration override + pure resolution primitives

**Files:**
- Modify: `src/motorcal/config.py`
- Create: `src/motorcal/merge.py` (already exists from Phase 5 — this task appends to it)
- Test: `tests/test_config_series_durations.py`
- Test: `tests/test_merge_resolution.py`

**Interfaces:**
- Consumes: `DurationDefaults`, `_StrictModel` from `motorcal.config` (Phase 1); `SessionType`, `EventStatus` from `motorcal.models` (Phase 1).
- Produces (used by Tasks 2 and 4):
  - `SeriesConfig` gains a new field: `durations: DurationDefaults | None = None`.
  - `def compute_fingerprint(*, summary: str, description: str, location: str | None, status: str, start: str | None, all_day_date: str | None, duration_seconds: int | None, alarms: list[str]) -> str` — a stable SHA-256 hex digest over exactly these fields, order-independent for `alarms`.
  - `def next_sequence(previous_sequence: int | None, now_unix_minute: int) -> int`.
  - `def resolve_duration(session_type: SessionType, *, own_duration_seconds: int | None, series_config: object, root_config: object) -> int | None` — implements the 4-tier priority described in Global Constraints. `series_config`/`root_config` are typed loosely here (`object`) in the signature description only because the concrete types (`SeriesConfig`/`RootConfig`) live in `motorcal.config`, which this function already imports directly — use the real types in the implementation, this bullet is just documenting the parameter's purpose.
  - `def resolve_alarms(session_type: SessionType, *, is_synthetic: bool, own_alarms: list[str] | None, time_confirmed: bool, root_config: object) -> list[str]` — returns `[]` for unconfirmed events or `SessionType.UNKNOWN`/`SessionType.TESTING`; otherwise a synthetic event's own list if `is_synthetic`, else `root_config.defaults.alerts.get(session_type.value, [])`.

- [ ] **Step 1: Write the failing config test**

```python
# tests/test_config_series_durations.py
from pathlib import Path

from motorcal.config import load_config

EXAMPLE_CONFIG = Path("config/config.example.yaml")


def test_series_config_durations_defaults_to_none():
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.series["f1"].durations is None


def test_series_config_accepts_per_series_duration_overrides(tmp_path):
    overridden = tmp_path / "config.yaml"
    overridden.write_text(
        EXAMPLE_CONFIG.read_text().replace(
            '  f1:\n    league_id: 4370\n    name: "Formula 1"\n    max_round: 30\n',
            '  f1:\n    league_id: 4370\n    name: "Formula 1"\n    max_round: 30\n'
            '    durations:\n      race: "2h"\n',
        )
    )
    cfg = load_config(overridden)
    assert cfg.series["f1"].durations is not None
    assert cfg.series["f1"].durations.race == "2h"
    assert cfg.series["f1"].durations.practice is None
```

- [ ] **Step 2: Run the config test to verify it fails**

Run: `uv run pytest tests/test_config_series_durations.py -v`
Expected: FAIL — `SeriesConfig` has no `durations` field yet (pydantic raises a validation error on the second test, and the first test fails because the attribute doesn't exist).

- [ ] **Step 3: Add the `durations` field to `SeriesConfig` in `src/motorcal/config.py`**

Find the existing `SeriesConfig` class (it currently has `league_id`, `name`, `max_round`, `race_only`) and add one field:

```python
class SeriesConfig(_StrictModel):
    league_id: int
    name: str
    max_round: int
    race_only: bool = False
    durations: DurationDefaults | None = None
```

- [ ] **Step 4: Run the config test to verify it passes**

Run: `uv run pytest tests/test_config_series_durations.py -v`
Expected: PASS, 2 passed.

- [ ] **Step 5: Write the failing merge-resolution tests**

```python
# tests/test_merge_resolution.py
from motorcal.config import DefaultsConfig, DurationDefaults, RootConfig, SeriesConfig, UnknownTimeConfig
from motorcal.merge import compute_fingerprint, next_sequence, resolve_alarms, resolve_duration
from motorcal.models import SessionType


def test_compute_fingerprint_is_deterministic_for_identical_inputs():
    fp1 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600,
        alarms=["-1d", "-30m"],
    )
    fp2 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600,
        alarms=["-1d", "-30m"],
    )
    assert fp1 == fp2


def test_compute_fingerprint_alarm_order_does_not_matter():
    fp1 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600,
        alarms=["-1d", "-30m"],
    )
    fp2 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600,
        alarms=["-30m", "-1d"],
    )
    assert fp1 == fp2


def test_compute_fingerprint_changes_when_status_changes():
    fp1 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600, alarms=[],
    )
    fp2 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CANCELLED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600, alarms=[],
    )
    assert fp1 != fp2


def test_compute_fingerprint_changes_when_alarm_set_changes():
    fp1 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600, alarms=["-1d"],
    )
    fp2 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600, alarms=["-30m"],
    )
    assert fp1 != fp2


def test_next_sequence_for_a_brand_new_event():
    assert next_sequence(None, now_unix_minute=12345678) == 12345678


def test_next_sequence_increments_when_greater_than_current_minute():
    assert next_sequence(previous_sequence=100, now_unix_minute=50) == 101


def test_next_sequence_jumps_to_current_minute_when_ahead_of_previous():
    assert next_sequence(previous_sequence=100, now_unix_minute=999999) == 999999


def test_next_sequence_restored_backup_never_goes_backwards():
    # A restored-from-backup previous_sequence that's already far in the future of
    # "now" must still advance forward, never reset down to now_unix_minute.
    assert next_sequence(previous_sequence=999999, now_unix_minute=100) == 1000000


def _root_config(global_race_duration="1h", alerts=None):
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention={},
        defaults=DefaultsConfig(
            durations=DurationDefaults(race=global_race_duration),
            alerts=alerts or {"race": ["-1d"]},
            include_sessions=["race"],
        ),
        unknown_time=UnknownTimeConfig(),
        series={},
    )


def test_resolve_duration_prefers_own_duration_over_everything():
    root = _root_config()
    series = SeriesConfig(league_id=1, name="X", max_round=1, durations=DurationDefaults(race="3h"))
    result = resolve_duration(
        SessionType.RACE, own_duration_seconds=21600, series_config=series, root_config=root
    )
    assert result == 21600  # own duration (6h), not the 3h series override or 1h global default


def test_resolve_duration_falls_back_to_series_override():
    root = _root_config(global_race_duration="1h")
    series = SeriesConfig(league_id=1, name="X", max_round=1, durations=DurationDefaults(race="3h"))
    result = resolve_duration(
        SessionType.RACE, own_duration_seconds=None, series_config=series, root_config=root
    )
    assert result == 3 * 3600


def test_resolve_duration_falls_back_to_global_default():
    root = _root_config(global_race_duration="1h")
    series = SeriesConfig(league_id=1, name="X", max_round=1)  # no series-level override
    result = resolve_duration(
        SessionType.RACE, own_duration_seconds=None, series_config=series, root_config=root
    )
    assert result == 3600


def test_resolve_duration_returns_none_when_nothing_configured():
    root = _root_config(global_race_duration=None)
    series = SeriesConfig(league_id=1, name="X", max_round=1)
    result = resolve_duration(
        SessionType.HYPERPOLE, own_duration_seconds=None, series_config=series, root_config=root
    )
    assert result is None


def test_resolve_alarms_returns_empty_for_unconfirmed_time():
    root = _root_config(alerts={"race": ["-1d"]})
    result = resolve_alarms(
        SessionType.RACE, is_synthetic=False, own_alarms=None, time_confirmed=False, root_config=root
    )
    assert result == []


def test_resolve_alarms_returns_empty_for_unknown_session_type():
    root = _root_config(alerts={"race": ["-1d"]})
    result = resolve_alarms(
        SessionType.UNKNOWN, is_synthetic=False, own_alarms=None, time_confirmed=True, root_config=root
    )
    assert result == []


def test_resolve_alarms_returns_empty_for_testing_session_type():
    root = _root_config(alerts={"race": ["-1d"]})
    result = resolve_alarms(
        SessionType.TESTING, is_synthetic=False, own_alarms=None, time_confirmed=True, root_config=root
    )
    assert result == []


def test_resolve_alarms_uses_synthetic_own_alarms_when_synthetic():
    root = _root_config(alerts={"race": ["-1d"]})
    result = resolve_alarms(
        SessionType.RACE, is_synthetic=True, own_alarms=["-2d", "-1h"],
        time_confirmed=True, root_config=root,
    )
    assert result == ["-2d", "-1h"]


def test_resolve_alarms_uses_global_defaults_for_source_backed_event():
    root = _root_config(alerts={"race": ["-1d", "-30m"]})
    result = resolve_alarms(
        SessionType.RACE, is_synthetic=False, own_alarms=None, time_confirmed=True, root_config=root
    )
    assert result == ["-1d", "-30m"]


def test_resolve_alarms_empty_list_is_a_valid_deliberate_configuration():
    root = _root_config(alerts={"race": ["-1d"], "practice": []})
    result = resolve_alarms(
        SessionType.PRACTICE, is_synthetic=False, own_alarms=None, time_confirmed=True, root_config=root
    )
    assert result == []
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/test_merge_resolution.py -v`
Expected: FAIL / collection error — `compute_fingerprint`, `next_sequence`, `resolve_duration`, `resolve_alarms` do not exist yet.

- [ ] **Step 7: Append to `src/motorcal/merge.py`**

Read the existing file first — it already has `PatchMatchError`, `MatchedPatch`, `match_all_patches`, `reconcile_synthetic_events` from Phase 5, plus their imports. Add these imports at the top alongside the existing ones (do not duplicate any existing import line):

```python
import hashlib
from motorcal.config import RootConfig, SeriesConfig
from motorcal.models import SessionType
```

Append to the end of the file:

```python
def compute_fingerprint(
    *,
    summary: str,
    description: str,
    location: str | None,
    status: str,
    start: str | None,
    all_day_date: str | None,
    duration_seconds: int | None,
    alarms: list[str],
) -> str:
    """A stable digest over every client-visible VEVENT field. Alarm order never matters."""
    payload = {
        "summary": summary,
        "description": description,
        "location": location,
        "status": status,
        "start": start,
        "all_day_date": all_day_date,
        "duration_seconds": duration_seconds,
        "alarms": sorted(alarms),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def next_sequence(previous_sequence: int | None, now_unix_minute: int) -> int:
    """A new event may start at the current minute; an existing one must never regress."""
    if previous_sequence is None:
        return now_unix_minute
    return max(previous_sequence + 1, now_unix_minute)


def resolve_duration(
    session_type: SessionType,
    *,
    own_duration_seconds: int | None,
    series_config: SeriesConfig,
    root_config: RootConfig,
) -> int | None:
    """4-tier duration priority: own > per-series default > global default > None."""
    if own_duration_seconds is not None:
        return own_duration_seconds

    if series_config.durations is not None:
        series_value = getattr(series_config.durations, session_type.value, None)
        if series_value is not None:
            return parse_duration(series_value)

    global_value = getattr(root_config.defaults.durations, session_type.value, None)
    if global_value is not None:
        return parse_duration(global_value)

    return None


def resolve_alarms(
    session_type: SessionType,
    *,
    is_synthetic: bool,
    own_alarms: list[str] | None,
    time_confirmed: bool,
    root_config: RootConfig,
) -> list[str]:
    """Alarms only apply to confirmed, non-testing, non-unknown sessions."""
    if not time_confirmed or session_type in (SessionType.UNKNOWN, SessionType.TESTING):
        return []
    if is_synthetic:
        return list(own_alarms) if own_alarms is not None else []
    return list(root_config.defaults.alerts.get(session_type.value, []))
```

`json` and `parse_duration` must both already be importable — `json` was added to `merge.py`'s imports in Phase 5 (check the top of the file; add `import json` only if it is not already there), and `parse_duration` needs `from motorcal.config import parse_duration` added to the import block (Phase 5 imported `SyntheticEventConfig` and `parse_duration` from `motorcal.config` already for `reconcile_synthetic_events` — check whether `parse_duration` is already imported before adding it, to avoid a duplicate import line).

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_merge_resolution.py -v`
Expected: PASS, 18 passed.

- [ ] **Step 9: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-5 (169) plus this task's 2 config tests plus 18 merge tests pass — 189 passed.

- [ ] **Step 10: Commit**

```bash
git add src/motorcal/config.py src/motorcal/merge.py tests/test_config_series_durations.py tests/test_merge_resolution.py
git commit -m "Add per-series duration override and fingerprint/sequence/duration/alarm resolution primitives"
```

---

### Task 2: `build_published_event` — the per-event builder

**Files:**
- Modify: `src/motorcal/merge.py`
- Test: `tests/test_merge_build_event.py`

**Interfaces:**
- Consumes: `compute_fingerprint`, `next_sequence`, `resolve_duration`, `resolve_alarms` (Task 1); `SourceEvent`, `PublishedEvent`, `SessionType`, `EventStatus`, `source_uid`, `synthetic_event_uid` from `motorcal.models` (Phase 1); `PatchConfig` from `motorcal.config` (Phase 1).
- Produces (used by Task 4):
  - `@dataclass class PreviousPublishedState` fields: `fingerprint: str`, `sequence: int`, `dtstamp: str`, `last_modified: str`, `status: str` — a lightweight, sqlite-independent snapshot of what was previously published for one UID, so `build_published_event*` stays pure and testable without a database. `status` is needed because cancellation must be **sticky**: once an event is published `CANCELLED`, it must stay `CANCELLED` on every later rebuild — even after its own scheduled time has since passed — until retention pruning removes it. Without remembering the previous status, a naive "is this event still future/active" re-check on a later rebuild would incorrectly flip an already-cancelled past event back to `CONFIRMED`.
  - `def build_description(*, venue: str | None, country: str | None, round_number: int | None, race_only: bool, time_confirmed: bool, time_source: str, note: str | None) -> str` — `time_source` is one of `"provider"`, `"patch"`, `"synthetic"`. Produces a multi-line human-readable description covering venue, country, round (when not `None`), source attribution, a race-only-feed note when `race_only`, a time-confirmation note, and the patch/synthetic `note` field when present.
  - `def build_published_event_from_source(*, source_event: SourceEvent, session_type: SessionType, is_disappeared: bool, matched_patch: PatchConfig | None, uid_domain: str, race_only: bool, series_config: SeriesConfig, root_config: RootConfig, previous: PreviousPublishedState | None, now: datetime) -> PublishedEvent`.
  - `def build_published_event_from_synthetic(*, uid: str, series: str, summary: str, start: str | None, date: str | None, duration_seconds: int | None, location: str | None, note: str | None, alarms: list[str], is_cancelled: bool, uid_domain: str, root_config: RootConfig, previous: PreviousPublishedState | None, now: datetime) -> PublishedEvent`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_merge_build_event.py
from datetime import datetime, timezone

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    PatchConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.merge import (
    PreviousPublishedState,
    build_published_event_from_source,
    build_published_event_from_synthetic,
)
from motorcal.models import EventStatus, SessionType, SourceEvent, SourceEventKey

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _root_config():
    return RootConfig(
        server={"base_url": "https://x.example.com", "uid_domain": "x.example.com"},
        source={"refresh_cron": "0 * * * *"},
        retention={},
        defaults=DefaultsConfig(
            durations=DurationDefaults(),
            alerts={"race": ["-1d", "-30m"], "qualifying": ["-15m"]},
            include_sessions=["race", "qualifying"],
        ),
        unknown_time=UnknownTimeConfig(),
        series={},
    )


def _wec_race_event(time="00:00:00"):
    return SourceEvent(
        key=SourceEventKey(provider="thesportsdb", id_event="2421035"),
        series="wec",
        season="2026",
        round=1,
        name="6 Hours of Imola",
        date="2026-04-19",
        time=time,
        venue="Imola",
        country="Italy",
        raw={},
    )


def test_unconfirmed_time_produces_all_day_tbc_event_with_no_alarms():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.summary == "6 Hours of Imola (time TBC)"
    assert event.all_day_date == "2026-04-19"
    assert event.start is None
    assert event.time_confirmed is False
    assert event.alarms == []
    assert event.duration_seconds is None
    assert event.status is EventStatus.CONFIRMED
    assert event.uid == "thesportsdb-2421035@x.example.com"


def test_patch_confirms_start_and_sets_duration():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(
        id_event="2421035", start="2026-04-19T13:00:00Z", duration="6h", note="official WEC timetable"
    )
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.time_confirmed is True
    assert event.start.isoformat() == "2026-04-19T13:00:00+00:00"
    assert event.all_day_date is None
    assert event.duration_seconds == 6 * 3600
    assert "(time TBC)" not in event.summary
    assert event.alarms == ["-1d", "-30m"]  # global race defaults, since the patch has no alarms field


def test_patch_can_explicitly_reject_confirmation_of_a_non_midnight_start():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(id_event="2421035", start="2026-04-19T13:00:00Z", time_confirmed=False)
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.time_confirmed is False
    assert event.all_day_date == "2026-04-19"
    assert event.alarms == []


def test_confirmed_source_time_needs_no_patch():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.time_confirmed is True
    assert event.start.isoformat() == "2026-04-19T13:00:00+00:00"


def test_disappeared_future_event_becomes_cancelled():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),  # 2026-04-19, well after NOW (2026-07-29)... 
        session_type=SessionType.RACE,
        is_disappeared=True,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),  # NOW is before the event's date
    )
    assert event.status is EventStatus.CANCELLED


def test_disappeared_past_event_is_not_retroactively_cancelled():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    previous = PreviousPublishedState(
        fingerprint="irrelevant-for-this-test", sequence=5, dtstamp="t0", last_modified="t0",
        status="CONFIRMED",
    )
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),  # 2026-04-19
        session_type=SessionType.RACE,
        is_disappeared=True,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=previous,
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),  # well after the event's own scheduled time
    )
    assert event.status is EventStatus.CONFIRMED  # NOT cancelled — it's in the past


def test_cancellation_is_sticky_across_a_later_rebuild_after_the_event_has_passed():
    # An event cancelled while it was still in the future must STAY cancelled on a later
    # rebuild, even after its own scheduled time has since passed -- it must never flip
    # back to CONFIRMED just because "is this still future/active" would now say no.
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    previously_cancelled = PreviousPublishedState(
        fingerprint="irrelevant-for-this-test", sequence=5, dtstamp="t0", last_modified="t0",
        status="CANCELLED",
    )
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),  # 2026-04-19
        session_type=SessionType.RACE,
        is_disappeared=True,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=previously_cancelled,
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),  # long after the event's own scheduled time
    )
    assert event.status is EventStatus.CANCELLED


def test_disappearance_cancellation_overrides_a_patch_status():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(id_event="2421035", status="TENTATIVE", note="postponed")
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=True,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert event.status is EventStatus.CANCELLED  # disappearance wins over the patch's TENTATIVE


def test_patch_status_applies_when_not_disappeared():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(id_event="2421035", status="TENTATIVE", note="postponed")
    event = build_published_event_from_source(
        source_event=_wec_race_event(time="13:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.status is EventStatus.TENTATIVE


def test_unchanged_rebuild_preserves_sequence_and_timestamps():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch = PatchConfig(id_event="2421035", start="2026-04-19T13:00:00Z", duration="6h")
    first = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    previous = PreviousPublishedState(
        fingerprint=first.fingerprint,
        sequence=first.sequence,
        dtstamp=first.dtstamp.isoformat(),
        last_modified=first.last_modified.isoformat(),
        status=first.status.value,
    )
    later = datetime(2026, 8, 1, tzinfo=timezone.utc)
    second = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=previous,
        now=later,
    )
    assert second.sequence == first.sequence
    assert second.dtstamp == first.dtstamp
    assert second.last_modified == first.last_modified
    assert second.fingerprint == first.fingerprint


def test_changed_rebuild_bumps_sequence_and_updates_timestamps():
    series_cfg = SeriesConfig(league_id=4413, name="WEC", max_round=20)
    patch1 = PatchConfig(id_event="2421035", start="2026-04-19T13:00:00Z", duration="6h")
    first = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch1,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    previous = PreviousPublishedState(
        fingerprint=first.fingerprint,
        sequence=first.sequence,
        dtstamp=first.dtstamp.isoformat(),
        last_modified=first.last_modified.isoformat(),
        status=first.status.value,
    )
    patch2 = PatchConfig(id_event="2421035", start="2026-04-19T14:00:00Z", duration="6h")  # time changed
    later = datetime(2026, 8, 1, tzinfo=timezone.utc)
    second = build_published_event_from_source(
        source_event=_wec_race_event(time="00:00:00"),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=patch2,
        uid_domain="x.example.com",
        race_only=False,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=previous,
        now=later,
    )
    assert second.sequence > first.sequence
    assert second.dtstamp != first.dtstamp
    assert second.fingerprint != first.fingerprint


def test_race_only_series_note_appears_in_description():
    series_cfg = SeriesConfig(league_id=4373, name="IndyCar", max_round=30, race_only=True)
    event = build_published_event_from_source(
        source_event=SourceEvent(
            key=SourceEventKey(provider="thesportsdb", id_event="1"),
            series="indycar", season="2026", round=1,
            name="Firestone Grand Prix of St. Petersburg", date="2026-03-01",
            time="17:00:00", venue="St. Petersburg", country="USA", raw={},
        ),
        session_type=SessionType.RACE,
        is_disappeared=False,
        matched_patch=None,
        uid_domain="x.example.com",
        race_only=True,
        series_config=series_cfg,
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert "race" in event.description.lower()


def test_synthetic_event_uses_local_uid_format_and_own_alarms():
    event = build_published_event_from_synthetic(
        uid="imsa-2026-rolex-24",
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z",
        date=None,
        duration_seconds=24 * 3600,
        location=None,
        note="official IMSA timetable",
        alarms=["-1d", "-30m"],
        is_cancelled=False,
        uid_domain="x.example.com",
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.uid == "local-imsa-2026-rolex-24@x.example.com"
    assert event.status is EventStatus.CONFIRMED
    assert event.duration_seconds == 24 * 3600
    assert event.alarms == ["-1d", "-30m"]
    assert event.synthetic_uid == "imsa-2026-rolex-24"
    assert event.source_id_event is None


def test_synthetic_event_cancelled_flag_produces_cancelled_status():
    event = build_published_event_from_synthetic(
        uid="imsa-2026-rolex-24",
        series="imsa",
        summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z",
        date=None,
        duration_seconds=24 * 3600,
        location=None,
        note=None,
        alarms=[],
        is_cancelled=True,
        uid_domain="x.example.com",
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.status is EventStatus.CANCELLED


def test_synthetic_event_with_date_only_is_all_day():
    event = build_published_event_from_synthetic(
        uid="event-date-only",
        series="wec",
        summary="Test Event",
        start=None,
        date="2026-06-01",
        duration_seconds=None,
        location=None,
        note=None,
        alarms=[],
        is_cancelled=False,
        uid_domain="x.example.com",
        root_config=_root_config(),
        previous=None,
        now=NOW,
    )
    assert event.all_day_date == "2026-06-01"
    assert event.start is None
    assert event.time_confirmed is True  # a synthetic date-only event is deliberately configured, not TBC
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_merge_build_event.py -v`
Expected: FAIL / collection error — `PreviousPublishedState`, `build_description`, `build_published_event_from_source`, `build_published_event_from_synthetic` do not exist yet.

- [ ] **Step 3: Append to `src/motorcal/merge.py`**

Add these imports to the top of `merge.py` (check first for duplicates with what Phase 5/Task 1 already imported):

```python
from datetime import datetime, timezone

from motorcal.models import EventStatus, PublishedEvent, source_uid, synthetic_event_uid
```

Append to the end of the file:

```python
@dataclass
class PreviousPublishedState:
    """A lightweight snapshot of what was previously published for one UID.

    Kept independent of sqlite3.Row so build_published_event_* stay pure and
    testable without a database — the caller (Task 4's orchestration) converts
    a fetched published_events row into this shape before calling in.
    """

    fingerprint: str
    sequence: int
    dtstamp: str
    last_modified: str


def build_description(
    *,
    venue: str | None,
    country: str | None,
    round_number: int | None,
    race_only: bool,
    time_confirmed: bool,
    time_source: str,
    note: str | None,
) -> str:
    """Build the human-readable DESCRIPTION text for one published event."""
    lines: list[str] = []
    if venue:
        lines.append(f"Venue: {venue}")
    if country:
        lines.append(f"Country: {country}")
    if round_number is not None:
        lines.append(f"Round: {round_number}")
    lines.append("Source: TheSportsDB" if time_source != "synthetic" else "Source: local synthetic event")
    if race_only:
        lines.append("This series' feed includes race sessions only.")
    if time_source == "patch":
        lines.append("Time confirmed by local override.")
    elif time_source == "synthetic":
        lines.append("Time supplied by local synthetic event definition.")
    elif not time_confirmed:
        lines.append("Time not yet confirmed by the source (TBC).")
    else:
        lines.append("Time confirmed by source.")
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def _resolve_status(
    *,
    is_disappeared: bool,
    is_future_or_active: bool,
    patch_status: str | None,
    previous_status: EventStatus | None,
) -> EventStatus:
    """Cancellation is sticky: once CANCELLED, stay CANCELLED regardless of later rebuilds."""
    if is_disappeared:
        if previous_status == EventStatus.CANCELLED:
            return EventStatus.CANCELLED
        if is_future_or_active:
            return EventStatus.CANCELLED
        # A past event disappearing for the first time remains last-known-good, unchanged.
        return previous_status if previous_status is not None else EventStatus.CONFIRMED
    if patch_status is not None:
        return EventStatus(patch_status)
    return EventStatus.CONFIRMED


def build_published_event_from_source(
    *,
    source_event: SourceEvent,
    session_type: SessionType,
    is_disappeared: bool,
    matched_patch: PatchConfig | None,
    uid_domain: str,
    race_only: bool,
    series_config: SeriesConfig,
    root_config: RootConfig,
    previous: PreviousPublishedState | None,
    now: datetime,
) -> PublishedEvent:
    """Build (or rebuild) the published state for one source-backed event."""
    uid = source_uid(source_event.key.id_event, uid_domain)

    patched_start = matched_patch.start if matched_patch else None
    if patched_start:
        start_dt = datetime.fromisoformat(patched_start.replace("Z", "+00:00"))
        time_confirmed = (
            matched_patch.time_confirmed if matched_patch.time_confirmed is not None else True
        )
        time_source = "patch"
    else:
        if source_event.time is None or source_event.time == "00:00:00":
            time_confirmed = False
            start_dt = None
        else:
            start_dt = datetime.fromisoformat(f"{source_event.date}T{source_event.time}+00:00")
            time_confirmed = True
        time_source = "provider"

    summary = (matched_patch.summary if matched_patch and matched_patch.summary else source_event.name)
    location = (
        matched_patch.location
        if matched_patch and matched_patch.location
        else f"{source_event.venue}, {source_event.country}"
        if source_event.venue and source_event.country
        else source_event.venue or source_event.country
    )

    if not time_confirmed:
        summary = summary + root_config.unknown_time.summary_suffix
        all_day_date: str | None = source_event.date
        start: datetime | None = None
        duration_seconds: int | None = None
        alarms: list[str] = []
    else:
        all_day_date = None
        start = start_dt
        own_duration = parse_duration(matched_patch.duration) if matched_patch and matched_patch.duration else None
        duration_seconds = resolve_duration(
            session_type, own_duration_seconds=own_duration,
            series_config=series_config, root_config=root_config,
        )
        alarms = resolve_alarms(
            session_type, is_synthetic=False, own_alarms=None,
            time_confirmed=True, root_config=root_config,
        )

    is_future_or_active = _event_effective_end(start, all_day_date, duration_seconds) >= now
    patch_status = matched_patch.status if matched_patch else None
    previous_status = EventStatus(previous.status) if previous else None
    status = _resolve_status(
        is_disappeared=is_disappeared, is_future_or_active=is_future_or_active,
        patch_status=patch_status, previous_status=previous_status,
    )

    description = build_description(
        venue=source_event.venue, country=source_event.country, round_number=source_event.round,
        race_only=race_only, time_confirmed=time_confirmed, time_source=time_source,
        note=matched_patch.note if matched_patch else None,
    )

    fingerprint = compute_fingerprint(
        summary=summary, description=description, location=location, status=status.value,
        start=start.isoformat() if start else None, all_day_date=all_day_date,
        duration_seconds=duration_seconds, alarms=alarms,
    )

    now_unix_minute = int(now.timestamp() // 60)
    if previous is not None and previous.fingerprint == fingerprint:
        sequence = previous.sequence
        dtstamp = datetime.fromisoformat(previous.dtstamp)
        last_modified = datetime.fromisoformat(previous.last_modified)
    else:
        sequence = next_sequence(previous.sequence if previous else None, now_unix_minute)
        dtstamp = now
        last_modified = now

    return PublishedEvent(
        uid=uid, series=source_event.series, session_type=session_type, summary=summary,
        start=start, all_day_date=all_day_date, time_confirmed=time_confirmed,
        duration_seconds=duration_seconds, location=location, description=description,
        status=status, sequence=sequence, dtstamp=dtstamp, last_modified=last_modified,
        fingerprint=fingerprint, alarms=alarms, source_id_event=source_event.key.id_event,
        synthetic_uid=None,
    )


def build_published_event_from_synthetic(
    *,
    uid: str,
    series: str,
    summary: str,
    start: str | None,
    date: str | None,
    duration_seconds: int | None,
    location: str | None,
    note: str | None,
    alarms: list[str],
    is_cancelled: bool,
    uid_domain: str,
    root_config: RootConfig,
    previous: PreviousPublishedState | None,
    now: datetime,
) -> PublishedEvent:
    """Build (or rebuild) the published state for one synthetic event."""
    full_uid = synthetic_event_uid(uid, uid_domain)

    if start:
        start_dt: datetime | None = datetime.fromisoformat(start.replace("Z", "+00:00"))
        all_day_date: str | None = None
    else:
        start_dt = None
        all_day_date = date

    status = EventStatus.CANCELLED if is_cancelled else EventStatus.CONFIRMED

    description = build_description(
        venue=None, country=None, round_number=None, race_only=False,
        time_confirmed=True, time_source="synthetic", note=note,
    )

    fingerprint = compute_fingerprint(
        summary=summary, description=description, location=location, status=status.value,
        start=start_dt.isoformat() if start_dt else None, all_day_date=all_day_date,
        duration_seconds=duration_seconds, alarms=alarms,
    )

    now_unix_minute = int(now.timestamp() // 60)
    if previous is not None and previous.fingerprint == fingerprint:
        sequence = previous.sequence
        dtstamp = datetime.fromisoformat(previous.dtstamp)
        last_modified = datetime.fromisoformat(previous.last_modified)
    else:
        sequence = next_sequence(previous.sequence if previous else None, now_unix_minute)
        dtstamp = now
        last_modified = now

    return PublishedEvent(
        uid=full_uid, series=series, session_type=SessionType.RACE, summary=summary,
        start=start_dt, all_day_date=all_day_date, time_confirmed=True,
        duration_seconds=duration_seconds, location=location, description=description,
        status=status, sequence=sequence, dtstamp=dtstamp, last_modified=last_modified,
        fingerprint=fingerprint, alarms=alarms, source_id_event=None, synthetic_uid=uid,
    )


def _event_effective_end(
    start: datetime | None, all_day_date: str | None, duration_seconds: int | None
) -> datetime:
    """The last instant this event is considered 'happening', for retention/cancellation decisions."""
    from datetime import timedelta

    if start is not None:
        if duration_seconds:
            return start + timedelta(seconds=duration_seconds)
        return start
    day = datetime.fromisoformat(all_day_date).replace(tzinfo=timezone.utc)
    return day + timedelta(days=1)
```

Note: `build_published_event_from_synthetic` always sets `session_type=SessionType.RACE` — synthetic events represent whole race-weekend-style entries by convention (the spec's only worked example, `imsa-2026-rolex-24`, is a race) and have no provider-classified session type; this is a deliberate simplification, not an oversight.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_merge_build_event.py -v`
Expected: PASS, 15 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-5 and Phase 6 Task 1 (189) plus this task's 15 pass — 204 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/merge.py tests/test_merge_build_event.py
git commit -m "Add build_published_event_from_source/synthetic with disappearance-to-cancellation logic"
```

---

### Task 3: Retention window queries and pruning (`store.py`)

**Files:**
- Modify: `src/motorcal/store.py`
- Test: `tests/test_store_retention.py`

**Interfaces:**
- Consumes: `connect`, `init_schema`, `transaction`, `upsert_source_event`, `upsert_published_event` from Phases 2/4.
- Produces (used by Task 4):
  - `def list_all_source_events(conn: sqlite3.Connection) -> list[sqlite3.Row]` — every row in `source_events`, unscoped (Task 4's full rebuild needs every series/season at once, unlike Phase 4's scope-limited query).
  - `def list_published_events(conn: sqlite3.Connection) -> list[sqlite3.Row]` — every row in `published_events`.
  - `def delete_source_event(conn: sqlite3.Connection, provider: str, id_event: str) -> None`.
  - `def delete_published_event(conn: sqlite3.Connection, uid: str) -> None`.
  - `def purge_synthetic_event(conn: sqlite3.Connection, uid: str) -> None` — permanently deletes a synthetic event's row (the "separate explicit purge action" the spec describes; this phase provides the primitive, CLI wiring is a later phase).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_retention.py
from motorcal.store import (
    connect,
    delete_published_event,
    delete_source_event,
    get_published_event,
    get_source_event,
    get_synthetic_event,
    init_schema,
    list_all_source_events,
    list_published_events,
    purge_synthetic_event,
    transaction,
    upsert_published_event,
    upsert_source_event,
    upsert_synthetic_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert_source(conn, id_event, series="wec", season="2026"):
    upsert_source_event(
        conn, provider="thesportsdb", id_event=id_event, series=series, season=season,
        round=1, name=f"Event {id_event}", date="2026-04-19", time="13:00:00",
        venue="V", country="C", raw_json="{}", seen_at="t0",
    )


def _insert_published(conn, uid):
    upsert_published_event(
        conn, uid=uid, series="wec", session_type="race", summary="S", start="2026-04-19T13:00:00+00:00",
        all_day_date=None, time_confirmed=True, duration_seconds=3600, location="L",
        description="D", status="CONFIRMED", sequence=1, dtstamp="t0", last_modified="t0",
        fingerprint="fp", alarms_json="[]", source_provider="thesportsdb", source_id_event="1",
        synthetic_uid=None, cancelled_at=None, retain_until=None,
    )


def test_list_all_source_events_across_multiple_scopes(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_source(conn, "1", series="wec", season="2026")
        _insert_source(conn, "2", series="f1", season="2026")
        _insert_source(conn, "3", series="wec", season="2027")

    rows = list_all_source_events(conn)
    assert {row["id_event"] for row in rows} == {"1", "2", "3"}


def test_list_published_events_returns_all_rows(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "uid-1")
        _insert_published(conn, "uid-2")

    rows = list_published_events(conn)
    assert {row["uid"] for row in rows} == {"uid-1", "uid-2"}


def test_delete_source_event_removes_only_the_targeted_row(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_source(conn, "1")
        _insert_source(conn, "2")

    with transaction(conn):
        delete_source_event(conn, "thesportsdb", "1")

    assert get_source_event(conn, "thesportsdb", "1") is None
    assert get_source_event(conn, "thesportsdb", "2") is not None


def test_delete_published_event_removes_only_the_targeted_row(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert_published(conn, "uid-1")
        _insert_published(conn, "uid-2")

    with transaction(conn):
        delete_published_event(conn, "uid-1")

    assert get_published_event(conn, "uid-1") is None
    assert get_published_event(conn, "uid-2") is not None


def test_purge_synthetic_event_deletes_the_row(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_synthetic_event(
            conn, uid="uid-1", series="imsa", summary="S", start="2026-01-01T00:00:00+00:00",
            date=None, duration_seconds=3600, location=None, status="CONFIRMED", note=None,
            alarms_json="[]",
        )

    with transaction(conn):
        purge_synthetic_event(conn, "uid-1")

    assert get_synthetic_event(conn, "uid-1") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_retention.py -v`
Expected: FAIL / collection error — `list_all_source_events`, `list_published_events`, `delete_source_event`, `delete_published_event`, `purge_synthetic_event` do not exist yet.

- [ ] **Step 3: Append to `src/motorcal/store.py`**

```python
def list_all_source_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM source_events").fetchall()


def list_published_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM published_events").fetchall()


def delete_source_event(conn: sqlite3.Connection, provider: str, id_event: str) -> None:
    conn.execute(
        "DELETE FROM source_events WHERE provider = ? AND id_event = ?", (provider, id_event)
    )


def delete_published_event(conn: sqlite3.Connection, uid: str) -> None:
    conn.execute("DELETE FROM published_events WHERE uid = ?", (uid,))


def purge_synthetic_event(conn: sqlite3.Connection, uid: str) -> None:
    conn.execute("DELETE FROM synthetic_events WHERE uid = ?", (uid,))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_retention.py -v`
Expected: PASS, 5 passed.

- [ ] **Step 5: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-5 and Phase 6 Tasks 1-2 (204) plus this task's 5 pass — 209 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/store.py tests/test_store_retention.py
git commit -m "Add unscoped list/delete queries and synthetic-event purge for retention"
```

---

### Task 4: `rebuild_publication` — full orchestration

**Files:**
- Modify: `src/motorcal/merge.py`
- Test: `tests/test_merge_rebuild.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3, plus `classify_event` (Phase 4), `match_all_patches` (Phase 5), `list_all_source_events`/`list_published_events`/`get_published_event`/`upsert_published_event`/`delete_source_event`/`delete_published_event`/`list_synthetic_events`/`transaction` (`motorcal.store`), `SourceEvent`/`SourceEventKey` (`motorcal.models`), `OverridesConfig`/`RootConfig` (`motorcal.config`).
- Produces (used by Phase 9's scheduler and Phase 8's `/status`/feed routes, indirectly via the `published_events` table it writes):
  - `@dataclass class RebuildReport` fields: `events_published: int`, `events_cancelled: int`, `events_pruned: int`, `patch_errors: list[PatchMatchError]`, `unknown_events: list[str]` (UIDs classified `unknown`, for `/status` to surface — Phase 8's concern to display, this phase's concern to collect).
  - `def rebuild_publication(conn: sqlite3.Connection, *, root_config: RootConfig, overrides: OverridesConfig, uid_domain: str, now: datetime) -> RebuildReport` — reads every `source_events` and `synthetic_events` row, matches patches, builds every `PublishedEvent`, writes them all via `upsert_published_event`, prunes expired rows per the retention windows, and returns a summary — all inside one `store.transaction()` call.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_merge_rebuild.py
import json
from datetime import datetime, timezone

from motorcal.config import (
    DefaultsConfig,
    OverridesConfig,
    PatchConfig,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    SyntheticEventConfig,
    UnknownTimeConfig,
)
from motorcal.merge import rebuild_publication
from motorcal.models import source_uid, synthetic_event_uid
from motorcal.store import (
    connect,
    get_published_event,
    get_source_event,
    init_schema,
    reconcile_synthetic_events,
    transaction,
    upsert_source_event,
)

UID_DOMAIN = "x.example.com"


def _root_config(series=None):
    return RootConfig(
        server={"base_url": f"https://{UID_DOMAIN}", "uid_domain": UID_DOMAIN},
        source={"refresh_cron": "0 * * * *"},
        retention=RetentionConfig(historical_days=180, cancelled_after_event_days=90),
        defaults=DefaultsConfig(
            durations={},
            alerts={"race": ["-1d"]},
            include_sessions=["race"],
        ),
        unknown_time=UnknownTimeConfig(),
        series=series or {"wec": SeriesConfig(league_id=4413, name="WEC", max_round=20)},
    )


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_rebuild_publishes_a_confirmed_source_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-04-19", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert report.events_published == 1
    uid = source_uid("1", UID_DOMAIN)
    row = get_published_event(conn, uid)
    assert row is not None
    assert row["status"] == "CONFIRMED"


def test_rebuild_applies_a_matched_patch(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-04-19", time="00:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )
    overrides = OverridesConfig(
        patches=[PatchConfig(id_event="1", start="2026-04-19T13:00:00Z", duration="6h")]
    )

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=overrides,
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert report.patch_errors == []
    row = get_published_event(conn, source_uid("1", UID_DOMAIN))
    assert row["time_confirmed"] == 1
    assert row["duration_seconds"] == 6 * 3600


def test_rebuild_reports_an_unmatched_patch_as_an_error_without_crashing(tmp_path):
    conn = _fresh_conn(tmp_path)
    overrides = OverridesConfig(patches=[PatchConfig(id_event="does-not-exist")])

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=overrides,
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert len(report.patch_errors) == 1
    assert report.patch_errors[0].reason == "no_match"


def test_rebuild_cancels_a_disappeared_future_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-04-19", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )
    rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with transaction(conn):
        from motorcal.store import mark_source_event_disappeared
        mark_source_event_disappeared(conn, "thesportsdb", "1", "t1")

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 2, tzinfo=timezone.utc),  # still before the event
    )

    assert report.events_cancelled == 1
    row = get_published_event(conn, source_uid("1", UID_DOMAIN))
    assert row["status"] == "CANCELLED"


def test_rebuild_publishes_a_synthetic_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24", series="imsa", summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z", duration="24h",
    )
    with transaction(conn):
        reconcile_synthetic_events(conn, [cfg], now="t0")

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(events=[cfg]),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert report.events_published == 1
    uid = synthetic_event_uid("imsa-2026-rolex-24", UID_DOMAIN)
    row = get_published_event(conn, uid)
    assert row is not None
    assert row["duration_seconds"] == 24 * 3600


def test_rebuild_prunes_a_long_cancelled_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-01-01", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )
    rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2025, 12, 1, tzinfo=timezone.utc),
    )
    with transaction(conn):
        from motorcal.store import mark_source_event_disappeared
        mark_source_event_disappeared(conn, "thesportsdb", "1", "t1")
    rebuild_publication(  # this rebuild cancels it (event was still in the future relative to this `now`)
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2025, 12, 2, tzinfo=timezone.utc),
    )

    # Now simulate 91+ days after the (cancelled) event's own scheduled end (cancelled_after_event_days=90)
    far_future = datetime(2026, 4, 15, tzinfo=timezone.utc)  # >90 days after 2026-01-01
    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=far_future,
    )

    assert report.events_pruned >= 1
    assert get_published_event(conn, source_uid("1", UID_DOMAIN)) is None


def test_rebuild_prunes_a_long_past_non_cancelled_event(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="6 Hours of Imola", date="2026-01-01", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )
    rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    far_future = datetime(2026, 7, 1, tzinfo=timezone.utc)  # >180 days after 2026-01-01
    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=far_future,
    )

    assert report.events_pruned >= 1
    assert get_published_event(conn, source_uid("1", UID_DOMAIN)) is None
    assert get_source_event(conn, "thesportsdb", "1") is None


def test_rebuild_reports_unknown_classified_events(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        upsert_source_event(
            conn, provider="thesportsdb", id_event="1", series="wec", season="2026",
            round=1, name="Drivers Parade", date="2026-04-19", time="13:00:00",
            venue="Imola", country="Italy", raw_json="{}", seen_at="t0",
        )

    report = rebuild_publication(
        conn, root_config=_root_config(), overrides=OverridesConfig(),
        uid_domain=UID_DOMAIN, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert len(report.unknown_events) == 1
    assert report.events_published == 1  # still published — unknown is visible, not dropped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_merge_rebuild.py -v`
Expected: FAIL / collection error — `rebuild_publication`, `RebuildReport` do not exist yet.

- [ ] **Step 3: Append to `src/motorcal/merge.py`**

Add these imports (check first for duplicates):

```python
from motorcal.classify import classify_event
from motorcal.config import OverridesConfig
from motorcal.models import SourceEvent, SourceEventKey
from motorcal.store import (
    delete_published_event,
    delete_source_event,
    get_published_event,
    list_all_source_events,
    list_published_events,
    list_synthetic_events,
    upsert_published_event,
)
```

Append to the end of the file:

```python
@dataclass
class RebuildReport:
    events_published: int
    events_cancelled: int
    events_pruned: int
    patch_errors: list[PatchMatchError]
    unknown_events: list[str]


def _row_to_source_event(row: sqlite3.Row) -> SourceEvent:
    return SourceEvent(
        key=SourceEventKey(provider=row["provider"], id_event=row["id_event"]),
        series=row["series"], season=row["season"], round=row["round"], name=row["name"],
        date=row["date"], time=row["time"], venue=row["venue"], country=row["country"],
        raw=json.loads(row["raw_json"]),
    )


def _previous_state(row: sqlite3.Row | None) -> PreviousPublishedState | None:
    if row is None:
        return None
    return PreviousPublishedState(
        fingerprint=row["fingerprint"], sequence=row["sequence"],
        dtstamp=row["dtstamp"], last_modified=row["last_modified"], status=row["status"],
    )


def _write_published_event(conn: sqlite3.Connection, event: PublishedEvent) -> None:
    upsert_published_event(
        conn, uid=event.uid, series=event.series, session_type=event.session_type.value,
        summary=event.summary, start=event.start.isoformat() if event.start else None,
        all_day_date=event.all_day_date, time_confirmed=event.time_confirmed,
        duration_seconds=event.duration_seconds, location=event.location,
        description=event.description, status=event.status.value, sequence=event.sequence,
        dtstamp=event.dtstamp.isoformat(), last_modified=event.last_modified.isoformat(),
        fingerprint=event.fingerprint, alarms_json=json.dumps(event.alarms),
        source_provider="thesportsdb" if event.source_id_event else None,
        source_id_event=event.source_id_event, synthetic_uid=event.synthetic_uid,
        cancelled_at=None, retain_until=None,
    )


def rebuild_publication(
    conn: sqlite3.Connection,
    *,
    root_config: RootConfig,
    overrides: OverridesConfig,
    uid_domain: str,
    now: datetime,
) -> RebuildReport:
    """Rebuild every published event from current source/synthetic state, atomically."""
    source_rows = list_all_source_events(conn)
    source_events = [_row_to_source_event(row) for row in source_rows]
    matches, patch_errors = match_all_patches(overrides.patches, source_events)
    patch_by_id_event = {m.source_event.key.id_event: m.patch for m in matches}

    events_published = 0
    events_cancelled = 0
    unknown_events: list[str] = []

    with transaction(conn):
        for row, source_event in zip(source_rows, source_events):
            session_type = classify_event(source_event.series, source_event.name, source_event.round)
            series_config = root_config.series[source_event.series]
            matched_patch = patch_by_id_event.get(source_event.key.id_event)
            previous_row = get_published_event(conn, source_uid(source_event.key.id_event, uid_domain))

            event = build_published_event_from_source(
                source_event=source_event, session_type=session_type,
                is_disappeared=row["disappeared_at"] is not None, matched_patch=matched_patch,
                uid_domain=uid_domain, race_only=series_config.race_only,
                series_config=series_config, root_config=root_config,
                previous=_previous_state(previous_row), now=now,
            )
            _write_published_event(conn, event)
            events_published += 1
            if event.status == EventStatus.CANCELLED:
                events_cancelled += 1
            if session_type == SessionType.UNKNOWN:
                unknown_events.append(event.uid)

        for row in list_synthetic_events(conn):
            uid = synthetic_event_uid(row["uid"], uid_domain)
            previous_row = get_published_event(conn, uid)
            alarms = json.loads(row["alarms_json"])
            event = build_published_event_from_synthetic(
                uid=row["uid"], series=row["series"], summary=row["summary"], start=row["start"],
                date=row["date"], duration_seconds=row["duration_seconds"], location=row["location"],
                note=row["note"], alarms=alarms, is_cancelled=row["cancelled_at"] is not None,
                uid_domain=uid_domain, root_config=root_config,
                previous=_previous_state(previous_row), now=now,
            )
            _write_published_event(conn, event)
            events_published += 1
            if event.status == EventStatus.CANCELLED:
                events_cancelled += 1

        events_pruned = _prune_expired(
            conn, retention=root_config.retention, now=now,
        )

    return RebuildReport(
        events_published=events_published, events_cancelled=events_cancelled,
        events_pruned=events_pruned, patch_errors=patch_errors, unknown_events=unknown_events,
    )


def _prune_expired(conn: sqlite3.Connection, *, retention, now: datetime) -> int:
    """Delete published (and, where applicable, source) events past their retention window."""
    from datetime import timedelta

    pruned = 0
    for row in list_published_events(conn):
        start = datetime.fromisoformat(row["start"]) if row["start"] else None
        effective_end = _event_effective_end(start, row["all_day_date"], row["duration_seconds"])
        if effective_end >= now:
            continue  # still current/future -- never prune

        if row["status"] == "CANCELLED":
            cutoff = effective_end + timedelta(days=retention.cancelled_after_event_days)
        else:
            cutoff = effective_end + timedelta(days=retention.historical_days)

        if now > cutoff:
            delete_published_event(conn, row["uid"])
            if row["source_id_event"] is not None:
                delete_source_event(conn, row["source_provider"], row["source_id_event"])
            pruned += 1

    return pruned
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_merge_rebuild.py -v`
Expected: PASS, 8 passed.

- [ ] **Step 5: Run the entire test suite**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-5 and Phase 6 Tasks 1-3 (209) plus this task's 8 pass — 217 passed total.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/merge.py tests/test_merge_rebuild.py
git commit -m "Add rebuild_publication: full orchestration with patch application, cancellation, and retention pruning"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: full merge/publish order steps 3-7 (Merge and time handling section); fingerprint covering every client-visible field, sequence advancement formula with the "unchanged fingerprint preserves sequence/timestamps" determinism guarantee, and both UID formats (Canonical and published event model section); 4-tier duration resolution and alarm resolution rules including the all-day-TBC-gets-no-alarm rule (Merge and time handling + Overrides sections); disappearance-to-cancellation translation with future-vs-past distinction and both retention windows (Season and retention policy section); synthetic-event cancellation and purge primitive (Overrides and synthetic events section).
- Explicitly out of scope for this phase (later phases own them): actually rendering any of this into ICS bytes/VEVENT blocks (Phase 7); exposing `RebuildReport.unknown_events`/`patch_errors` on an HTTP `/status` route (Phase 8); wiring `rebuild_publication` into a cron schedule or a config-file-change poller, and deciding what "current vs. future season" means from today's date (Phase 9); wiring `purge_synthetic_event` into a CLI command (Phase 10).
- Type consistency check: `build_published_event_from_source`/`_from_synthetic` both return `motorcal.models.PublishedEvent` (Phase 1) — their `status`/`session_type` fields are the enum types (`EventStatus`/`SessionType`), not raw strings; `_write_published_event` (Task 4) is the single place that converts them to `.value` strings for SQLite storage, and `_previous_state`/`_row_to_source_event` are the single places that convert SQLite rows back into the pure-Python types Tasks 1-2's functions expect. If a later phase needs to read `published_events` rows directly (e.g. Phase 7's ICS renderer), it will need its own small `EventStatus(row["status"])`/`SessionType(row["session_type"])` conversion — this phase does not export a shared "row → PublishedEvent" helper because Phase 7 will likely want a different shape (grouped per series, sorted, etc.) rather than reusing this phase's internal one.
