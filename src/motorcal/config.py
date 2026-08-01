"""Configuration schema and loader.

The data directory is the source of truth. `defaults.yaml` holds the settings
that can't belong to any one series; every other `*.yaml` file is one series,
keyed by its filename stem, holding that series' settings and its full event list.

The refresh cycle writes series files back (see `sync.py`), so loading and saving
are both here. Comments inside a series file do not survive a rewrite -- the
per-event `note:` field is the durable place for annotations.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from motorcal.models import EventStatus, SessionType, session_uid

GLOBAL_FILENAME = "defaults.yaml"
# Served at /motorsports.ics as every series combined, so no series file may claim it.
COMBINED_SERIES_KEY = "motorsports"

_DURATION_RE = re.compile(r"^([1-9]\d*)(h|m)$")
_ALARM_OFFSET_RE = re.compile(r"^0$|^-[1-9]\d*[dhm]$")
_VALID_SESSION_NAMES = {member.value for member in SessionType}
_VALID_STATUS_NAMES = {member.value for member in EventStatus}


class ConfigError(Exception):
    """Raised for any invalid or unreadable configuration."""


class StrictModel(BaseModel):
    """Base class for every config model: rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


def _validate_duration_string(value: str | None) -> str | None:
    if value is not None and not _DURATION_RE.match(value):
        raise ValueError(f"Invalid duration string: {value!r}")
    return value


def _validate_alarm_list(value: list[str] | None) -> list[str] | None:
    for offset in value or []:
        if not _ALARM_OFFSET_RE.match(offset):
            raise ValueError(f"Invalid alarm offset {offset!r} (expected e.g. '-1d', '-30m', '0')")
    return value


def _validate_alerts_dict(value: dict[str, list[str]]) -> dict[str, list[str]]:
    unknown = set(value) - _VALID_SESSION_NAMES
    if unknown:
        raise ValueError(f"Unknown session type(s) in alerts: {sorted(unknown)}")
    for session, offsets in value.items():
        try:
            _validate_alarm_list(offsets)
        except ValueError as exc:
            raise ValueError(f"{exc} for session {session!r}") from exc
    return value


def parse_duration(value: str) -> int:
    """Parse a duration string like '1h' or '45m' into whole seconds."""
    match = _DURATION_RE.match(value)
    if not match:
        raise ConfigError(f"Invalid duration string: {value!r} (expected e.g. '1h', '45m')")
    amount, unit = match.groups()
    return int(amount) * (3600 if unit == "h" else 60)


def parse_alarm_offset(value: str) -> int:
    """Parse an alarm-offset string like '-1d' or '-30m' into whole seconds (negative).

    '0' means an alert exactly at event start.
    """
    if not _ALARM_OFFSET_RE.match(value):
        raise ConfigError(f"Invalid alarm offset: {value!r} (expected e.g. '-1d', '-30m', '0')")
    if value == "0":
        return 0
    amount, unit = int(value[1:-1]), value[-1]
    return -(amount * {"d": 86400, "h": 3600, "m": 60}[unit])


def _load_yaml_mapping(path: Path, kind: str) -> Any:
    """Read, parse, and mapping-check a YAML file, wrapping all failures in ConfigError."""
    try:
        raw_text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"Could not read {kind} file {path}: {exc}") from exc

    try:
        raw: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{kind.capitalize()} file {path} did not parse to a mapping")

    return raw


# --------------------------------------------------------------------------- globals


class SourceSettings(StrictModel):
    rate_limit_per_min: int = 28
    refresh_cron: str
    next_season_from: str = "10-01"

    @field_validator("refresh_cron")
    @classmethod
    def validate_refresh_cron(cls, value: str) -> str:
        """Reject a malformed cron expression at config-validation time -- catching it
        later, when APScheduler builds a CronTrigger during a hot reload, would mean
        the new bundle was already partially activated."""
        try:
            CronTrigger.from_crontab(value)
        except ValueError as exc:
            raise ValueError(f"Invalid refresh_cron: {value!r} ({exc})") from exc
        return value


class RetentionConfig(StrictModel):
    historical_days: int = 180
    cancelled_after_event_days: int = 90


class DurationDefaults(StrictModel):
    practice: str | None = None
    qualifying: str | None = None
    hyperpole: str | None = None
    sprint_qualifying: str | None = None
    sprint: str | None = None
    race: str | None = None

    @field_validator("*")
    @classmethod
    def validate_duration_format(cls, value: str | None) -> str | None:
        return _validate_duration_string(value)


class UnknownTimeConfig(StrictModel):
    summary_suffix: str = " (time TBC)"


class DefaultsConfig(StrictModel):
    durations: DurationDefaults = DurationDefaults()
    alerts: dict[str, list[str]] = {}

    @field_validator("alerts")
    @classmethod
    def validate_alerts(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        return _validate_alerts_dict(value)


class GlobalConfig(StrictModel):
    """Everything in data/defaults.yaml -- the settings no single series owns.

    `uid_domain` is the one exception: it's baked into every ICS UID, so getting
    it wrong is unusually costly (see `load_config`), and it's set via the
    UID_DOMAIN environment variable instead of living in the file.
    """

    uid_domain: str
    source: SourceSettings
    retention: RetentionConfig = RetentionConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    unknown_time: UnknownTimeConfig = UnknownTimeConfig()
    include_non_championship: bool = False


# --------------------------------------------------------------------------- events


class SourceSnapshot(StrictModel):
    """What the provider last reported for one event.

    Machine-written and the baseline for the 3-way merge in `sync.py`: a field is
    only overwritten from a new fetch if the provider actually changed it AND the
    stored value still matches what the provider said before. Absent on manual events.
    """

    name: str
    date: str
    time: str | None = None
    venue: str | None = None
    country: str | None = None
    round: int
    season: str


class SessionConfig(StrictModel):
    """One session of a race event: a practice, a qualifying, the race itself.

    Everything here is specific to this session; whatever the whole weekend shares
    (name, location, round) lives once on the `EventConfig` holding it.

    Provider-backed sessions carry `id_event` and `source`; manual sessions carry a
    `uid` you choose and are never touched by a refresh.
    """

    id_event: str | None = None
    uid: str | None = None
    label: str = ""  # appended to the event name: "Practice 1", "Qualifying", "Race"
    type: SessionType = SessionType.UNKNOWN
    start: str | None = None  # confirmed time, ISO 8601
    date: str | None = None  # all-day, "YYYY-MM-DD" -- the time is not yet known
    duration: str | None = None
    status: str = EventStatus.CONFIRMED.value
    note: str | None = None
    alarms: list[str] | None = None  # None = fall back to series/global defaults
    round: int | None = None  # only when it differs from the event's -- see EventConfig
    disappeared_at: str | None = None
    source: SourceSnapshot | None = None

    @field_validator("duration")
    @classmethod
    def validate_duration_format(cls, value: str | None) -> str | None:
        return _validate_duration_string(value)

    @field_validator("alarms")
    @classmethod
    def validate_alarms(cls, value: list[str] | None) -> list[str] | None:
        return _validate_alarm_list(value)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in _VALID_STATUS_NAMES:
            raise ValueError(
                f"Invalid status: {value!r} (expected one of {sorted(_VALID_STATUS_NAMES)})"
            )
        return value

    @model_validator(mode="after")
    def validate_identity_and_timing(self) -> "SessionConfig":
        if bool(self.id_event) == bool(self.uid):
            raise ValueError("A session must set exactly one of id_event or uid")
        if bool(self.start) == bool(self.date):
            raise ValueError("A session must set exactly one of start or date")
        return self

    @property
    def key(self) -> str:
        """The identity this session is matched on within its series file."""
        return self.id_event or self.uid  # type: ignore[return-value]

    @property
    def when(self) -> str:
        """Sort key: whichever of start/date is set, both ISO and so both ordered."""
        return self.start or self.date or ""


class EventConfig(StrictModel):
    """One race event -- a weekend -- and its list of sessions.

    Name, location and round are stored once here rather than repeated on every
    session, and each published session is summarised as "{name} {label}".

    `round` is the weekend's first championship round. A double-header runs two
    rounds over one weekend; its second race carries its own `round` to say so.
    """

    name: str
    location: str | None = None
    round: int | None = None
    sessions: list[SessionConfig] = []

    def round_of(self, session: SessionConfig) -> int | None:
        return session.round if session.round is not None else self.round

    @model_validator(mode="after")
    def validate_has_sessions(self) -> "EventConfig":
        if not self.sessions:
            raise ValueError(f"Event {self.name!r} has no sessions")
        return self

    @property
    def when(self) -> str:
        return min((session.when for session in self.sessions), default="")


class SeriesConfig(StrictModel):
    """One data/<series>.yaml. The series key is the filename stem."""

    league_id: int
    name: str
    max_round: int
    race_only: bool = False
    durations: DurationDefaults | None = None
    alerts: dict[str, list[str]] | None = None
    events: list[EventConfig] = []

    @field_validator("alerts")
    @classmethod
    def validate_alerts(cls, value: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
        return _validate_alerts_dict(value) if value is not None else None

    @model_validator(mode="after")
    def validate_unique_session_keys(self) -> "SeriesConfig":
        seen: set[str] = set()
        for _, session in self.iter_sessions():
            if session.key in seen:
                raise ValueError(f"Duplicate session id_event/uid in this series: {session.key!r}")
            seen.add(session.key)
        return self

    def iter_sessions(self) -> list[tuple[EventConfig, SessionConfig]]:
        """Every (event, session) pair in this series, in file order."""
        return [(event, session) for event in self.events for session in event.sessions]


# --------------------------------------------------------------------------- loading


class Config(StrictModel):
    """The whole data directory: globals plus every series, keyed by filename stem."""

    globals: GlobalConfig
    series: dict[str, SeriesConfig]

    def events_for(self, series: str) -> list[EventConfig]:
        return self.series[series].events


_SERIES_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def load_config(config_dir: Path, *, uid_domain: str) -> Config:
    """Load and validate a whole data directory. Raises ConfigError on any failure.

    `uid_domain` comes from the caller (the UID_DOMAIN environment variable in
    practice), not the file -- getting it wrong silently republishes every event
    under new UIDs, so it must never be something a stray edit to defaults.yaml
    can change.
    """
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        raise ConfigError(f"Config directory not found: {config_dir}")

    global_path = config_dir / GLOBAL_FILENAME
    raw_globals = _load_yaml_mapping(global_path, "config")
    if "uid_domain" in raw_globals:
        raise ConfigError(
            f"{global_path} must not set uid_domain -- set the UID_DOMAIN "
            "environment variable instead"
        )
    try:
        globals_ = GlobalConfig.model_validate({**raw_globals, "uid_domain": uid_domain})
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {global_path}: {exc}") from exc

    series: dict[str, SeriesConfig] = {}
    for path in sorted(config_dir.glob("*.yaml")):
        # state.yaml (and its dated backups, see docs/operations.md) share this
        # directory but aren't series data -- skip them like the global file.
        if path.name == GLOBAL_FILENAME or path.name.startswith(".") or path.name.startswith("state"):
            continue
        key = path.stem
        if key == COMBINED_SERIES_KEY:
            raise ConfigError(
                f"Invalid series filename {path.name!r}: '{COMBINED_SERIES_KEY}' is reserved "
                f"for the combined feed at /{COMBINED_SERIES_KEY}.ics, which would shadow it"
            )
        if not _SERIES_KEY_RE.match(key):
            raise ConfigError(
                f"Invalid series filename {path.name!r}: the stem becomes the series key "
                "and must be lowercase letters, digits, '-' or '_'"
            )
        raw = _load_yaml_mapping(path, "series")
        try:
            series[key] = SeriesConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"Invalid series configuration in {path}: {exc}") from exc

    if not series:
        raise ConfigError(f"No series files found in {config_dir} (expected e.g. f1.yaml)")

    # SeriesConfig only enforces uniqueness within its own file, but a UID is global:
    # the version ledger is keyed by it, so two sessions sharing one would overwrite
    # each other's sequence, and the combined feed would emit two VEVENTs a calendar
    # client is entitled to treat as the same event.
    owner: dict[str, str] = {}
    for key, series_config in series.items():
        for _, session in series_config.iter_sessions():
            uid = session_uid(session, uid_domain)
            if uid in owner:
                raise ConfigError(
                    f"Duplicate session uid {session.key!r} in {key}.yaml: already used in "
                    f"{owner[uid]}.yaml. A uid must be unique across the whole data directory."
                )
            owner[uid] = key

    return Config(globals=globals_, series=series)


def series_path(config_dir: Path, series: str) -> Path:
    return Path(config_dir) / f"{series}.yaml"


def save_series(config_dir: Path, series: str, config: SeriesConfig) -> None:
    """Rewrite one series file atomically, preserving field order and dropping nothing.

    Events are written in the order they appear in `config.events`; `sync.py` keeps
    that list sorted by date so the file stays readable and diffs stay small.
    """
    path = series_path(config_dir, series)
    payload = config.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
    # Sessions carry a lot of optional fields; drop the empty ones per-session so a
    # hand-written file doesn't grow a wall of `null`s the first time it's rewritten.
    payload["events"] = [event.model_dump(mode="json", exclude_none=True) for event in config.events]

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{series}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False, width=100)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
