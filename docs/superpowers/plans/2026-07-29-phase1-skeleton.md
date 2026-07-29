# Motorsports Calendar — Phase 1: Skeleton, Config, Models, Fixtures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the project skeleton for `motorcal` — a Python 3.13 service that publishes per-series ICS calendars from TheSportsDB — with a validated configuration/overrides schema, canonical data models, and a captured corpus of real TheSportsDB fixtures for later phases to build on.

**Architecture:** A `src/motorcal` package managed by `uv`. This phase produces no runtime behavior yet — it produces the typed building blocks (`config.py`, `models.py`) and test fixtures that phases 2-10 will import. Everything here must be independently unit-testable with `pytest`.

**Tech Stack:** Python 3.13, `uv` for packaging/deps, `pydantic` v2 for config validation, `pytest` for tests. Runtime deps for later phases (`fastapi`, `uvicorn`, `httpx`, `icalendar`, `apscheduler`) are declared now so `uv sync` works throughout, but are not used until their respective phases.

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous.
- Repository layout is fixed (do not deviate):
  ```
  motorsports-calendar/
    pyproject.toml
    config/
      config.example.yaml
      overrides.example.yaml
    src/motorcal/
      config.py
      models.py
      providers/thesportsdb.py
      classify.py
      store.py
      merge.py
      ics.py
      refresh.py
      web.py
      cli.py
    tests/
      fixtures/
    Dockerfile
    compose.yaml
    .env.example
    .gitignore
  ```
- League IDs: F1 `4370`, IndyCar `4373`, WEC `4413`, IMSA `4488`.
- Session types (fixed vocabulary): `practice`, `qualifying`, `hyperpole`, `sprint_qualifying`, `sprint`, `race`, `testing`, `unknown`.
- Event statuses: `CONFIRMED`, `TENTATIVE`, `CANCELLED`.
- Source-backed ICS UID: `thesportsdb-{id_event}@{uid_domain}`. Synthetic ICS UID: `local-{configured_uid}@{uid_domain}`.
- `uid_domain` is explicit immutable config — never derived from `base_url`.
- Default `rate_limit_per_min` is `28`. Default retention: `historical_days: 180`, `cancelled_after_event_days: 90`.
- Default `include_sessions`: `practice, qualifying, hyperpole, sprint_qualifying, sprint, race`.
- Required environment variables (documented, not necessarily read until Phase 8/9): `THESPORTSDB_API_KEY`, `MOTORCAL_TOKENS` (comma-separated).
- Secrets never belong in committed YAML — only in environment variables. `config/*.example.yaml` contain placeholders only.
- No pip: dependency management is `uv` only (`uv sync`, `uv add`).
- Duration strings look like `"1h"`, `"45m"`, `"24h"`, `"6h"` — integer count + unit `h`/`m`. Alarm-offset strings look like `"-1d"`, `"-30m"`, `"-15m"` — a minus sign, integer count, unit `d`/`h`/`m`.

---

### Task 1: Project skeleton and packaging

**Files:**
- Create: `pyproject.toml`
- Create: `src/motorcal/__init__.py`
- Create: `src/motorcal/providers/__init__.py`
- Create: `.env.example`
- Create: `tests/__init__.py`
- Create: `tests/fixtures/.gitkeep`

**Interfaces:**
- Produces: an installable `motorcal` package importable as `import motorcal`; `uv run pytest` works from repo root.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "motorcal"
version = "0.1.0"
description = "Self-hosted per-series motorsports ICS calendar publisher"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pyyaml>=6.0",
    "httpx>=0.27",
    "icalendar>=6.0",
    "apscheduler>=3.10",
]

[project.scripts]
motorcal = "motorcal.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/motorcal"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create package directories and empty `__init__.py` files**

```bash
mkdir -p src/motorcal/providers tests/fixtures
touch src/motorcal/__init__.py src/motorcal/providers/__init__.py tests/__init__.py
touch tests/fixtures/.gitkeep
```

- [ ] **Step 3: Write `.env.example`**

```bash
# Required: TheSportsDB API key (paid/free tier key, NOT the public "3" test key, for production use)
THESPORTSDB_API_KEY=changeme

# Required: comma-separated list of feed access tokens. Multiple values support rotation.
MOTORCAL_TOKENS=changeme-token-one,changeme-token-two
```

- [ ] **Step 4: Sync dependencies and verify the environment resolves**

Run: `uv sync`
Expected: completes without error and creates/updates `uv.lock`.

- [ ] **Step 5: Verify pytest runs (no tests yet, should report 0 collected)**

Run: `uv run pytest`
Expected: `no tests ran` / `collected 0 items`, exit code 0 (pytest with zero tests still exits 0 when there are no collection errors).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/motorcal/__init__.py src/motorcal/providers/__init__.py tests/__init__.py tests/fixtures/.gitkeep .env.example
git commit -m "Project skeleton: pyproject.toml, package layout, env template"
```

---

### Task 2: Canonical models (`models.py`)

**Files:**
- Create: `src/motorcal/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing (base layer).
- Produces (used by every later phase):
  - `class SessionType(str, Enum)` members `PRACTICE="practice"`, `QUALIFYING="qualifying"`, `HYPERPOLE="hyperpole"`, `SPRINT_QUALIFYING="sprint_qualifying"`, `SPRINT="sprint"`, `RACE="race"`, `TESTING="testing"`, `UNKNOWN="unknown"`.
  - `class EventStatus(str, Enum)` members `CONFIRMED="CONFIRMED"`, `TENTATIVE="TENTATIVE"`, `CANCELLED="CANCELLED"`.
  - `@dataclass(frozen=True) class SourceEventKey` fields `provider: str`, `id_event: str`.
  - `@dataclass class SourceEvent` fields `key: SourceEventKey`, `series: str`, `season: str`, `round: int`, `name: str`, `date: str`, `time: str | None`, `venue: str | None`, `country: str | None`, `raw: dict`.
  - `@dataclass class PublishedEvent` fields `uid: str`, `series: str`, `session_type: SessionType`, `summary: str`, `start: datetime | None`, `all_day_date: str | None`, `time_confirmed: bool`, `duration_seconds: int | None`, `location: str | None`, `description: str`, `status: EventStatus`, `sequence: int`, `dtstamp: datetime`, `last_modified: datetime`, `fingerprint: str`, `alarms: list[str]`, `source_id_event: str | None`, `synthetic_uid: str | None`.
  - `def source_uid(id_event: str, uid_domain: str) -> str` returns `f"thesportsdb-{id_event}@{uid_domain}"`.
  - `def synthetic_event_uid(configured_uid: str, uid_domain: str) -> str` returns `f"local-{configured_uid}@{uid_domain}"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_models.py
from datetime import datetime, timezone

from motorcal.models import (
    EventStatus,
    PublishedEvent,
    SessionType,
    SourceEvent,
    SourceEventKey,
    source_uid,
    synthetic_event_uid,
)


def test_source_uid_format():
    assert source_uid("2421035", "racing.example.com") == "thesportsdb-2421035@racing.example.com"


def test_synthetic_event_uid_format():
    assert (
        synthetic_event_uid("imsa-2026-rolex-24", "racing.example.com")
        == "local-imsa-2026-rolex-24@racing.example.com"
    )


def test_source_event_key_is_hashable_and_frozen():
    key1 = SourceEventKey(provider="thesportsdb", id_event="2421035")
    key2 = SourceEventKey(provider="thesportsdb", id_event="2421035")
    assert key1 == key2
    assert hash(key1) == hash(key2)


def test_source_event_construction():
    ev = SourceEvent(
        key=SourceEventKey(provider="thesportsdb", id_event="2421035"),
        series="wec",
        season="2026",
        round=1,
        name="6 Hours of Imola",
        date="2026-04-19",
        time="00:00:00",
        venue="Autodromo Enzo e Dino Ferrari",
        country="Italy",
        raw={"idEvent": "2421035"},
    )
    assert ev.series == "wec"
    assert ev.time == "00:00:00"


def test_published_event_construction():
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    pub = PublishedEvent(
        uid="thesportsdb-2421035@racing.example.com",
        series="wec",
        session_type=SessionType.RACE,
        summary="6 Hours of Imola",
        start=now,
        all_day_date=None,
        time_confirmed=True,
        duration_seconds=6 * 3600,
        location="Imola, Italy",
        description="Round 1 of WEC",
        status=EventStatus.CONFIRMED,
        sequence=1,
        dtstamp=now,
        last_modified=now,
        fingerprint="deadbeef",
        alarms=["-1d", "-30m"],
        source_id_event="2421035",
        synthetic_uid=None,
    )
    assert pub.session_type is SessionType.RACE
    assert pub.status is EventStatus.CONFIRMED


def test_session_type_values_are_fixed_vocabulary():
    assert {m.value for m in SessionType} == {
        "practice",
        "qualifying",
        "hyperpole",
        "sprint_qualifying",
        "sprint",
        "race",
        "testing",
        "unknown",
    }


def test_event_status_values_are_fixed_vocabulary():
    assert {m.value for m in EventStatus} == {"CONFIRMED", "TENTATIVE", "CANCELLED"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL / collection error — `motorcal.models` does not exist yet.

- [ ] **Step 3: Write `src/motorcal/models.py`**

```python
"""Canonical data models shared by every phase of motorcal."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SessionType(str, Enum):
    PRACTICE = "practice"
    QUALIFYING = "qualifying"
    HYPERPOLE = "hyperpole"
    SPRINT_QUALIFYING = "sprint_qualifying"
    SPRINT = "sprint"
    RACE = "race"
    TESTING = "testing"
    UNKNOWN = "unknown"


class EventStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    TENTATIVE = "TENTATIVE"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class SourceEventKey:
    """Identity of an event as seen by a provider: {provider, id_event}."""

    provider: str
    id_event: str


@dataclass
class SourceEvent:
    """A normalized event as reported by a provider, before classification or merge."""

    key: SourceEventKey
    series: str
    season: str
    round: int
    name: str
    date: str  # "YYYY-MM-DD" as returned by the provider
    time: str | None  # "HH:MM:SS", or None if the provider omitted it
    venue: str | None
    country: str | None
    raw: dict = field(default_factory=dict)


@dataclass
class PublishedEvent:
    """A fully resolved event ready for ICS rendering."""

    uid: str
    series: str
    session_type: SessionType
    summary: str
    start: datetime | None  # None when unconfirmed (all-day) or omitted
    all_day_date: str | None  # "YYYY-MM-DD" when rendered as an all-day event
    time_confirmed: bool
    duration_seconds: int | None
    location: str | None
    description: str
    status: EventStatus
    sequence: int
    dtstamp: datetime
    last_modified: datetime
    fingerprint: str
    alarms: list[str] = field(default_factory=list)
    source_id_event: str | None = None
    synthetic_uid: str | None = None


def source_uid(id_event: str, uid_domain: str) -> str:
    """Build the stable ICS UID for a provider-sourced event."""
    return f"thesportsdb-{id_event}@{uid_domain}"


def synthetic_event_uid(configured_uid: str, uid_domain: str) -> str:
    """Build the stable ICS UID for a locally configured synthetic event."""
    return f"local-{configured_uid}@{uid_domain}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS, 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/motorcal/models.py tests/test_models.py
git commit -m "Add canonical SourceEvent/PublishedEvent models and UID builders"
```

---

### Task 3: Config schema, loader, and example file

**Files:**
- Create: `src/motorcal/config.py`
- Create: `config/config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (used by phases 3, 4, 6, 7, 9):
  - `class ConfigError(Exception)`.
  - `class ServerConfig(BaseModel)`: `base_url: str`, `uid_domain: str`.
  - `class SourceSettings(BaseModel)`: `rate_limit_per_min: int = 28`, `refresh_cron: str`, `next_season_from: str = "10-01"`.
  - `class RetentionConfig(BaseModel)`: `historical_days: int = 180`, `cancelled_after_event_days: int = 90`.
  - `class DurationDefaults(BaseModel)`: `practice: str | None`, `qualifying: str | None`, `hyperpole: str | None`, `sprint_qualifying: str | None`, `sprint: str | None`, `race: str | None` (all optional, default `None`).
  - `class UnknownTimeConfig(BaseModel)`: `mode: str = "all_day"`, `summary_suffix: str = " (time TBC)"`.
  - `class DefaultsConfig(BaseModel)`: `durations: DurationDefaults`, `alerts: dict[str, list[str]]`, `include_sessions: list[str]`.
  - `class SeriesConfig(BaseModel)`: `league_id: int`, `name: str`, `max_round: int`, `race_only: bool = False`.
  - `class RootConfig(BaseModel)`: `server: ServerConfig`, `source: SourceSettings`, `retention: RetentionConfig`, `defaults: DefaultsConfig`, `include_non_championship: bool = False`, `unknown_time: UnknownTimeConfig`, `series: dict[str, SeriesConfig]`.
  - `def parse_duration(value: str) -> int` — returns seconds; raises `ConfigError` on bad format.
  - `def load_config(path: Path) -> RootConfig` — reads + validates YAML, raises `ConfigError` with a human-readable message on any failure.
  - Valid `SessionType` values (from `motorcal.models.SessionType`) are the only allowed entries in `include_sessions` and in `defaults.alerts` keys.
  - Valid alarm-offset strings match `^-\d+[dhm]$` (e.g. `-1d`, `-30m`, `-15m`); `defaults.alerts` values must all match this pattern.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
from pathlib import Path

import pytest

from motorcal.config import ConfigError, load_config, parse_duration

EXAMPLE_CONFIG = Path("config/config.example.yaml")


def test_parse_duration_hours():
    assert parse_duration("1h") == 3600


def test_parse_duration_minutes():
    assert parse_duration("45m") == 45 * 60


def test_parse_duration_rejects_bad_format():
    with pytest.raises(ConfigError):
        parse_duration("banana")


def test_load_example_config_succeeds():
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.server.base_url == "https://racing.example.com"
    assert cfg.server.uid_domain == "racing.example.com"
    assert cfg.source.rate_limit_per_min == 28
    assert cfg.retention.historical_days == 180
    assert cfg.retention.cancelled_after_event_days == 90
    assert cfg.series["f1"].league_id == 4370
    assert cfg.series["wec"].league_id == 4413
    assert cfg.series["indycar"].league_id == 4373
    assert cfg.series["indycar"].race_only is True
    assert cfg.series["imsa"].league_id == 4488
    assert cfg.series["imsa"].race_only is True
    assert "race" in cfg.defaults.include_sessions
    assert cfg.unknown_time.mode == "all_day"


def test_load_config_missing_file_raises_config_error():
    with pytest.raises(ConfigError):
        load_config(Path("config/does-not-exist.yaml"))


def test_load_config_rejects_unknown_session_in_include_sessions(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        EXAMPLE_CONFIG.read_text().replace(
            "include_sessions:\n    - practice",
            "include_sessions:\n    - not_a_real_session\n    - practice",
        )
    )
    with pytest.raises(ConfigError):
        load_config(bad)


def test_load_config_rejects_missing_required_field(tmp_path):
    bad = tmp_path / "bad.yaml"
    text = EXAMPLE_CONFIG.read_text().replace('uid_domain: "racing.example.com"\n', "")
    bad.write_text(text)
    with pytest.raises(ConfigError):
        load_config(bad)


def test_load_config_rejects_bad_alarm_offset(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        EXAMPLE_CONFIG.read_text().replace(
            'race: ["-1d", "-30m"]', 'race: ["-1d", "thirty minutes"]'
        )
    )
    with pytest.raises(ConfigError):
        load_config(bad)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL / collection error — `motorcal.config` and `config/config.example.yaml` do not exist yet.

- [ ] **Step 3: Write `config/config.example.yaml`**

```yaml
server:
  base_url: "https://racing.example.com"
  uid_domain: "racing.example.com"

source:
  rate_limit_per_min: 28
  refresh_cron: "17 */6 * * *"
  next_season_from: "10-01"

retention:
  historical_days: 180
  cancelled_after_event_days: 90

defaults:
  durations:
    practice: "1h"
    qualifying: "1h"
    sprint: "45m"
  alerts:
    race: ["-1d", "-30m"]
    qualifying: ["-15m"]
    hyperpole: ["-15m"]
    sprint: ["-15m"]
    sprint_qualifying: ["-15m"]
    practice: []
  include_sessions:
    - practice
    - qualifying
    - hyperpole
    - sprint_qualifying
    - sprint
    - race

include_non_championship: false
unknown_time:
  mode: all_day
  summary_suffix: " (time TBC)"

series:
  f1:
    league_id: 4370
    name: "Formula 1"
    max_round: 30
  wec:
    league_id: 4413
    name: "WEC"
    max_round: 20
  indycar:
    league_id: 4373
    name: "IndyCar"
    max_round: 30
    race_only: true
  imsa:
    league_id: 4488
    name: "IMSA"
    max_round: 30
    race_only: true
```

- [ ] **Step 4: Write `src/motorcal/config.py`**

```python
"""Configuration schema and loader for motorcal's config.yaml."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from motorcal.models import SessionType

_DURATION_RE = re.compile(r"^(\d+)(h|m)$")
_ALARM_OFFSET_RE = re.compile(r"^-\d+[dhm]$")
_VALID_SESSION_NAMES = {member.value for member in SessionType}


class ConfigError(Exception):
    """Raised for any invalid or unreadable configuration."""


def parse_duration(value: str) -> int:
    """Parse a duration string like '1h' or '45m' into whole seconds."""
    match = _DURATION_RE.match(value)
    if not match:
        raise ConfigError(f"Invalid duration string: {value!r} (expected e.g. '1h', '45m')")
    amount, unit = match.groups()
    amount = int(amount)
    return amount * 3600 if unit == "h" else amount * 60


class ServerConfig(BaseModel):
    base_url: str
    uid_domain: str


class SourceSettings(BaseModel):
    rate_limit_per_min: int = 28
    refresh_cron: str
    next_season_from: str = "10-01"


class RetentionConfig(BaseModel):
    historical_days: int = 180
    cancelled_after_event_days: int = 90


class DurationDefaults(BaseModel):
    practice: str | None = None
    qualifying: str | None = None
    hyperpole: str | None = None
    sprint_qualifying: str | None = None
    sprint: str | None = None
    race: str | None = None

    @field_validator(
        "practice", "qualifying", "hyperpole", "sprint_qualifying", "sprint", "race"
    )
    @classmethod
    def validate_duration_format(cls, value: str | None) -> str | None:
        if value is not None and not _DURATION_RE.match(value):
            raise ValueError(f"Invalid duration string: {value!r}")
        return value


class UnknownTimeConfig(BaseModel):
    mode: str = "all_day"
    summary_suffix: str = " (time TBC)"


class DefaultsConfig(BaseModel):
    durations: DurationDefaults
    alerts: dict[str, list[str]]
    include_sessions: list[str]

    @field_validator("include_sessions")
    @classmethod
    def validate_include_sessions(cls, value: list[str]) -> list[str]:
        unknown = set(value) - _VALID_SESSION_NAMES
        if unknown:
            raise ValueError(f"Unknown session type(s) in include_sessions: {sorted(unknown)}")
        return value

    @field_validator("alerts")
    @classmethod
    def validate_alerts(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown_keys = set(value) - _VALID_SESSION_NAMES
        if unknown_keys:
            raise ValueError(f"Unknown session type(s) in alerts: {sorted(unknown_keys)}")
        for session, offsets in value.items():
            for offset in offsets:
                if not _ALARM_OFFSET_RE.match(offset):
                    raise ValueError(
                        f"Invalid alarm offset {offset!r} for session {session!r} "
                        "(expected e.g. '-1d', '-30m', '-15m')"
                    )
        return value


class SeriesConfig(BaseModel):
    league_id: int
    name: str
    max_round: int
    race_only: bool = False


class RootConfig(BaseModel):
    server: ServerConfig
    source: SourceSettings
    retention: RetentionConfig
    defaults: DefaultsConfig
    include_non_championship: bool = False
    unknown_time: UnknownTimeConfig
    series: dict[str, SeriesConfig]


def load_config(path: Path) -> RootConfig:
    """Load and validate a config.yaml bundle. Raises ConfigError on any failure."""
    try:
        raw_text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}") from exc

    try:
        raw: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {path} did not parse to a mapping")

    try:
        return RootConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {path}: {exc}") from exc
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS, 7 passed.

- [ ] **Step 6: Commit**

```bash
git add src/motorcal/config.py config/config.example.yaml tests/test_config.py
git commit -m "Add config schema, loader, and example config.yaml"
```

---

### Task 4: Overrides schema, example file, and duration-string reuse

**Files:**
- Modify: `src/motorcal/config.py`
- Create: `config/overrides.example.yaml`
- Test: `tests/test_overrides.py`

**Interfaces:**
- Consumes: `parse_duration`, `ConfigError`, `_ALARM_OFFSET_RE` pattern from Task 3 (same module).
- Produces (used by phase 5 patch/synthetic-event validation and phase 6 merge):
  - `class PatchMatcher(BaseModel)`: `series: str`, `date: str`, `contains: str`.
  - `class PatchConfig(BaseModel)`: `id_event: str | None = None`, `match: PatchMatcher | None = None`, `start: str | None = None`, `time_confirmed: bool | None = None`, `duration: str | None = None`, `summary: str | None = None`, `location: str | None = None`, `status: str | None = None`, `note: str | None = None`. A model-level validator requires exactly one of `id_event` or `match`.
  - `class SyntheticEventConfig(BaseModel)`: `uid: str`, `series: str`, `summary: str`, `start: str | None = None`, `date: str | None = None`, `duration: str | None = None`, `location: str | None = None`, `status: str | None = None`, `note: str | None = None`, `alarms: list[str] = []`. A model-level validator requires exactly one of `start` or `date`.
  - `class OverridesConfig(BaseModel)`: `patches: list[PatchConfig] = []`, `events: list[SyntheticEventConfig] = []`.
  - `def load_overrides(path: Path) -> OverridesConfig` — same error-wrapping contract as `load_config`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_overrides.py
from pathlib import Path

import pytest

from motorcal.config import ConfigError, load_overrides

EXAMPLE_OVERRIDES = Path("config/overrides.example.yaml")


def test_load_example_overrides_succeeds():
    overrides = load_overrides(EXAMPLE_OVERRIDES)
    assert len(overrides.patches) == 1
    patch = overrides.patches[0]
    assert patch.id_event == "2421035"
    assert patch.start == "2026-04-19T13:00:00Z"
    assert patch.duration == "6h"
    assert patch.note == "official WEC timetable"

    assert len(overrides.events) == 1
    ev = overrides.events[0]
    assert ev.uid == "imsa-2026-rolex-24"
    assert ev.series == "imsa"
    assert ev.summary == "Rolex 24 at Daytona"
    assert ev.start == "2026-01-25T18:40:00Z"
    assert ev.duration == "24h"


def test_patch_requires_id_event_or_match(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches:
  - start: "2026-04-19T13:00:00Z"
events: []
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_patch_rejects_both_id_event_and_match(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches:
  - id_event: "123"
    match:
      series: "wec"
      date: "2026-04-19"
      contains: "Imola"
    start: "2026-04-19T13:00:00Z"
events: []
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_synthetic_event_requires_uid(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches: []
events:
  - series: imsa
    summary: "Rolex 24 at Daytona"
    start: "2026-01-25T18:40:00Z"
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_synthetic_event_requires_start_or_date(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches: []
events:
  - uid: "imsa-2026-rolex-24"
    series: imsa
    summary: "Rolex 24 at Daytona"
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_synthetic_event_rejects_both_start_and_date(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches: []
events:
  - uid: "imsa-2026-rolex-24"
    series: imsa
    summary: "Rolex 24 at Daytona"
    start: "2026-01-25T18:40:00Z"
    date: "2026-01-25"
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_load_overrides_missing_file_raises_config_error():
    with pytest.raises(ConfigError):
        load_overrides(Path("config/does-not-exist-overrides.yaml"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_overrides.py -v`
Expected: FAIL / collection error — `load_overrides` and `config/overrides.example.yaml` do not exist yet.

- [ ] **Step 3: Write `config/overrides.example.yaml`**

```yaml
patches:
  - id_event: "2421035"
    start: "2026-04-19T13:00:00Z"
    duration: "6h"
    note: "official WEC timetable"

events:
  - uid: "imsa-2026-rolex-24"
    series: imsa
    summary: "Rolex 24 at Daytona"
    start: "2026-01-25T18:40:00Z"
    duration: "24h"
    note: "official IMSA timetable"
```

- [ ] **Step 4: Append the overrides schema to `src/motorcal/config.py`**

Add these imports at the top alongside the existing ones:

```python
from pydantic import BaseModel, ValidationError, field_validator, model_validator
```

(Only `model_validator` is new — add it to the existing `from pydantic import ...` line rather than duplicating the import.)

Append to the end of `src/motorcal/config.py`:

```python
class PatchMatcher(BaseModel):
    series: str
    date: str
    contains: str


class PatchConfig(BaseModel):
    id_event: str | None = None
    match: PatchMatcher | None = None
    start: str | None = None
    time_confirmed: bool | None = None
    duration: str | None = None
    summary: str | None = None
    location: str | None = None
    status: str | None = None
    note: str | None = None

    @field_validator("duration")
    @classmethod
    def validate_duration_format(cls, value: str | None) -> str | None:
        if value is not None and not _DURATION_RE.match(value):
            raise ValueError(f"Invalid duration string: {value!r}")
        return value

    @model_validator(mode="after")
    def validate_exactly_one_matcher(self) -> "PatchConfig":
        if bool(self.id_event) == bool(self.match):
            raise ValueError("A patch must set exactly one of id_event or match")
        return self


class SyntheticEventConfig(BaseModel):
    uid: str
    series: str
    summary: str
    start: str | None = None
    date: str | None = None
    duration: str | None = None
    location: str | None = None
    status: str | None = None
    note: str | None = None
    alarms: list[str] = []

    @field_validator("duration")
    @classmethod
    def validate_duration_format(cls, value: str | None) -> str | None:
        if value is not None and not _DURATION_RE.match(value):
            raise ValueError(f"Invalid duration string: {value!r}")
        return value

    @field_validator("alarms")
    @classmethod
    def validate_alarms(cls, value: list[str]) -> list[str]:
        for offset in value:
            if not _ALARM_OFFSET_RE.match(offset):
                raise ValueError(f"Invalid alarm offset {offset!r}")
        return value

    @model_validator(mode="after")
    def validate_exactly_one_of_start_or_date(self) -> "SyntheticEventConfig":
        if bool(self.start) == bool(self.date):
            raise ValueError("A synthetic event must set exactly one of start or date")
        return self


class OverridesConfig(BaseModel):
    patches: list[PatchConfig] = []
    events: list[SyntheticEventConfig] = []


def load_overrides(path: Path) -> OverridesConfig:
    """Load and validate an overrides.yaml bundle. Raises ConfigError on any failure."""
    try:
        raw_text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"Could not read overrides file {path}: {exc}") from exc

    try:
        raw: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Overrides file {path} did not parse to a mapping")

    try:
        return OverridesConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid overrides in {path}: {exc}") from exc
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_overrides.py -v`
Expected: PASS, 7 passed.

- [ ] **Step 6: Run the full test suite so far**

Run: `uv run pytest -v`
Expected: all tests from Tasks 2-4 pass (21 passed).

- [ ] **Step 7: Commit**

```bash
git add src/motorcal/config.py config/overrides.example.yaml tests/test_overrides.py
git commit -m "Add overrides schema (patches + synthetic events) and example overrides.yaml"
```

---

### Task 5: Captured TheSportsDB fixtures

**Context:** Real sample responses were already captured from `https://www.thesportsdb.com/api/v1/json/3/eventsround.php` (the public TheSportsDB test key `3`) for season `2026` and are sitting in
`/private/tmp/claude-501/-Users-graham-Developer-motorsports-calendar/43dffb51-cffe-469e-97cd-67bcc3a8f97e/scratchpad/fixtures_raw/`.
This task copies them into the repo's fixture corpus with descriptive names and adds a smoke test proving the corpus is loadable JSON with the expected shape. These fixtures are the raw material Phase 3 (provider) and Phase 4 (classification) will consume — do not reshape their contents, only rename/organize them.

Observed naming patterns worth preserving verbatim (do not "clean up" or re-type these — copy the files as-is):
- F1 round 1 (`league4370_r1_2026.json`): `"Australian Grand Prix Practice 1/2/3"`, `"...Qualifying"`, bare `"Australian Grand Prix"` (race).
- F1 round 3 (`league4370_r3_2026.json`): `"Chinese Grand Prix Sprint Qualifying"` and `"...Sprint"` alongside `"...Practice 1"`, `"...Qualifying"`, bare race name — proves sprint-qualifying-before-sprint/qualifying ordering matters.
- F1 round 500 (`league4370_r500_2026.json`): `"Bahrain Testing 1 Day 1"` etc. — round 500 is testing regardless of name.
- WEC round 1 (`league4413_r1_2026.json`): `"6 Hours of Imola Free Practice 3"`, `"...Qualifying"`, bare `"6 Hours of Imola"` (id_event `2421035` — matches the example patch in overrides.yaml) with `strTime` `"00:00:00"` (unconfirmed time).
- WEC round 2 (`league4413_r2_2026.json`): `"...Qualifying - LMGT3"` and `"...Qualifying - Hypercar"` — class-suffixed qualifying, hyphen separator.
- WEC round 3 (`league4413_r3_2026.json`): `"...Hyperpole Qualifying – LMP2 & LMGT3"` and `"...Hyperpole Qualifying – Hypercar"` — note the en dash (`–`, U+2013), not a hyphen, and that "Hyperpole" must be classified before "Qualifying".
- WEC round 500 (`league4413_r500_2026.json`): `"Imola Prologue Morning/Afternoon Session"` — round 500 is testing regardless of name.
- IndyCar round 1 (`league4373_r1_2026.json`): bare `"Firestone Grand Prix of St. Petersburg"` — race-only series, no session suffix.
- IMSA round 1 (`league4488_r1_2026.json`): bare `"Rolex 24 At DAYTONA"` with `strTime` `"00:00:00"` (unconfirmed time).
- IMSA round 500 (`league4488_r500_2026.json`): `"Roar Before The Rolex 24"` — round 500 is testing regardless of name.
- IndyCar round 500 (`league4373_r500_2026.json`): empty (`{"events": null}` or similar) — proves an empty round response is normal, not an error.

**Files:**
- Create: `tests/fixtures/thesportsdb/f1_r1_2026.json`
- Create: `tests/fixtures/thesportsdb/f1_r3_2026.json`
- Create: `tests/fixtures/thesportsdb/f1_r500_2026_testing.json`
- Create: `tests/fixtures/thesportsdb/wec_r1_2026.json`
- Create: `tests/fixtures/thesportsdb/wec_r2_2026_class_split.json`
- Create: `tests/fixtures/thesportsdb/wec_r3_2026_hyperpole.json`
- Create: `tests/fixtures/thesportsdb/wec_r500_2026_prologue.json`
- Create: `tests/fixtures/thesportsdb/indycar_r1_2026.json`
- Create: `tests/fixtures/thesportsdb/indycar_r500_2026_empty.json`
- Create: `tests/fixtures/thesportsdb/imsa_r1_2026.json`
- Create: `tests/fixtures/thesportsdb/imsa_r500_2026_roar.json`
- Test: `tests/test_fixtures.py`

**Interfaces:**
- Produces: a directory of real-shape TheSportsDB `eventsround.php` JSON responses under `tests/fixtures/thesportsdb/` that Phase 3 (provider parsing) and Phase 4 (classification) tests will load by filename.

- [ ] **Step 1: Copy the captured raw responses into the fixture directory with descriptive names**

```bash
mkdir -p tests/fixtures/thesportsdb
RAW=/private/tmp/claude-501/-Users-graham-Developer-motorsports-calendar/43dffb51-cffe-469e-97cd-67bcc3a8f97e/scratchpad/fixtures_raw
cp "$RAW/league4370_r1_2026.json"   tests/fixtures/thesportsdb/f1_r1_2026.json
cp "$RAW/league4370_r3_2026.json"   tests/fixtures/thesportsdb/f1_r3_2026.json
cp "$RAW/league4370_r500_2026.json" tests/fixtures/thesportsdb/f1_r500_2026_testing.json
cp "$RAW/league4413_r1_2026.json"   tests/fixtures/thesportsdb/wec_r1_2026.json
cp "$RAW/league4413_r2_2026.json"   tests/fixtures/thesportsdb/wec_r2_2026_class_split.json
cp "$RAW/league4413_r3_2026.json"   tests/fixtures/thesportsdb/wec_r3_2026_hyperpole.json
cp "$RAW/league4413_r500_2026.json" tests/fixtures/thesportsdb/wec_r500_2026_prologue.json
cp "$RAW/league4373_r1_2026.json"   tests/fixtures/thesportsdb/indycar_r1_2026.json
cp "$RAW/league4373_r500_2026.json" tests/fixtures/thesportsdb/indycar_r500_2026_empty.json
cp "$RAW/league4488_r1_2026.json"   tests/fixtures/thesportsdb/imsa_r1_2026.json
cp "$RAW/league4488_r500_2026.json" tests/fixtures/thesportsdb/imsa_r500_2026_roar.json
ls tests/fixtures/thesportsdb/
```

Expected: 11 files listed.

- [ ] **Step 2: Write the failing smoke test**

```python
# tests/test_fixtures.py
import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path("tests/fixtures/thesportsdb")

FIXTURE_FILES = [
    "f1_r1_2026.json",
    "f1_r3_2026.json",
    "f1_r500_2026_testing.json",
    "wec_r1_2026.json",
    "wec_r2_2026_class_split.json",
    "wec_r3_2026_hyperpole.json",
    "wec_r500_2026_prologue.json",
    "indycar_r1_2026.json",
    "indycar_r500_2026_empty.json",
    "imsa_r1_2026.json",
    "imsa_r500_2026_roar.json",
]


@pytest.mark.parametrize("filename", FIXTURE_FILES)
def test_fixture_is_valid_json_with_events_key(filename):
    data = json.loads((FIXTURE_DIR / filename).read_text())
    assert "events" in data


def test_f1_round1_has_practice_qualifying_and_race():
    data = json.loads((FIXTURE_DIR / "f1_r1_2026.json").read_text())
    names = [e["strEvent"] for e in data["events"]]
    assert any("Practice 1" in n for n in names)
    assert any("Qualifying" in n and "Sprint" not in n for n in names)
    assert "Australian Grand Prix" in names


def test_f1_round3_has_sprint_qualifying_and_sprint():
    data = json.loads((FIXTURE_DIR / "f1_r3_2026.json").read_text())
    names = [e["strEvent"] for e in data["events"]]
    assert any("Sprint Qualifying" in n for n in names)
    assert any(n.endswith("Sprint") for n in names)


def test_wec_round1_race_id_matches_overrides_example():
    data = json.loads((FIXTURE_DIR / "wec_r1_2026.json").read_text())
    race = next(e for e in data["events"] if e["strEvent"] == "6 Hours of Imola")
    assert race["idEvent"] == "2421035"
    assert race["strTime"] == "00:00:00"


def test_wec_round3_hyperpole_uses_en_dash():
    data = json.loads((FIXTURE_DIR / "wec_r3_2026_hyperpole.json").read_text())
    names = [e["strEvent"] for e in data["events"]]
    hyperpole_names = [n for n in names if "Hyperpole" in n]
    assert hyperpole_names
    assert any("–" in n for n in hyperpole_names)


def test_wec_round2_class_split_qualifying_uses_hyphen():
    data = json.loads((FIXTURE_DIR / "wec_r2_2026_class_split.json").read_text())
    names = [e["strEvent"] for e in data["events"]]
    assert any("Qualifying - LMGT3" in n or "Qualifying - Hypercar" in n for n in names)


def test_indycar_round500_is_empty():
    data = json.loads((FIXTURE_DIR / "indycar_r500_2026_empty.json").read_text())
    assert data["events"] in (None, [])


def test_imsa_round1_is_bare_race_name_with_unconfirmed_time():
    data = json.loads((FIXTURE_DIR / "imsa_r1_2026.json").read_text())
    race = data["events"][0]
    assert race["strEvent"] == "Rolex 24 At DAYTONA"
    assert race["strTime"] == "00:00:00"


def test_round_500_fixtures_are_non_championship_named():
    f1 = json.loads((FIXTURE_DIR / "f1_r500_2026_testing.json").read_text())
    wec = json.loads((FIXTURE_DIR / "wec_r500_2026_prologue.json").read_text())
    imsa = json.loads((FIXTURE_DIR / "imsa_r500_2026_roar.json").read_text())
    assert any("Testing" in e["strEvent"] for e in f1["events"])
    assert any("Prologue" in e["strEvent"] for e in wec["events"])
    assert any("Roar" in e["strEvent"] for e in imsa["events"])
```

- [ ] **Step 3: Run tests to verify they fail before the copy (sanity check on a clean checkout)**

Run: `uv run pytest tests/test_fixtures.py -v`
Expected (only if Step 1 has not yet run in this shell): FAIL with missing files. If Step 1 already ran, skip to Step 4 — this step exists so a re-run from a clean clone demonstrates the fixtures are what make the test pass, not a hidden default.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fixtures.py -v`
Expected: PASS, 18 passed (11 parametrized + 7 named).

- [ ] **Step 5: Run the entire Phase 1 test suite**

Run: `uv run pytest -v`
Expected: all tests from Tasks 2-5 pass (39 passed total: 7 models + 7 config + 7 overrides + 18 fixtures).

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/thesportsdb/ tests/test_fixtures.py
git commit -m "Add captured TheSportsDB fixture corpus for F1/WEC/IndyCar/IMSA"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: pyproject/skeleton (Build order #1), config schema + example (Configuration section), overrides schema + example (Overrides and synthetic events section), canonical models (Canonical and published event model section — deferred: fingerprint *computation* and sequence *advancement* logic are Phase 6, not this phase; the field exists on `PublishedEvent` as a plain string here), captured fixtures (Provider and snapshot semantics section, "fixture directory must be created and populated during implementation").
- Explicitly out of scope for this phase (later phases own them): SQLite persistence, HTTP fetching/rate limiting, classification regex rules, patch matching *logic* (only the config *schema* is built here), fingerprint/sequence computation, ICS rendering, FastAPI routes, scheduler, Docker.
- Type consistency check: `SessionType` values match between `models.py` and the `include_sessions`/`alerts` validators in `config.py`. `parse_duration` in `config.py` is the single source of truth for duration-string parsing; Task 4's `PatchConfig`/`SyntheticEventConfig` validators reuse the same `_DURATION_RE` and `_ALARM_OFFSET_RE` module-level patterns rather than redefining them.
