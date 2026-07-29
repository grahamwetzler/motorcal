"""Configuration schema and loader for motorcal's config.yaml."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from motorcal.models import EventStatus, SessionType

_DURATION_RE = re.compile(r"^([1-9]\d*)(h|m)$")
_ALARM_OFFSET_RE = re.compile(r"^-[1-9]\d*[dhm]$")
_VALID_SESSION_NAMES = {member.value for member in SessionType}
_VALID_STATUS_NAMES = {member.value for member in EventStatus}


class ConfigError(Exception):
    """Raised for any invalid or unreadable configuration."""


class _StrictModel(BaseModel):
    """Base class for all config/overrides models: rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


def _validate_duration_string(value: str | None) -> str | None:
    """Shared field-validator body for duration-string fields."""
    if value is not None and not _DURATION_RE.match(value):
        raise ValueError(f"Invalid duration string: {value!r}")
    return value


def parse_duration(value: str) -> int:
    """Parse a duration string like '1h' or '45m' into whole seconds."""
    match = _DURATION_RE.match(value)
    if not match:
        raise ConfigError(f"Invalid duration string: {value!r} (expected e.g. '1h', '45m')")
    amount, unit = match.groups()
    amount = int(amount)
    return amount * 3600 if unit == "h" else amount * 60


def parse_alarm_offset(value: str) -> int:
    """Parse an alarm-offset string like '-1d' or '-30m' into whole seconds (negative)."""
    match = _ALARM_OFFSET_RE.match(value)
    if not match:
        raise ConfigError(
            f"Invalid alarm offset: {value!r} (expected e.g. '-1d', '-30m', '-15m')"
        )
    amount = int(value[1:-1])
    unit = value[-1]
    if unit == "d":
        seconds = amount * 86400
    elif unit == "h":
        seconds = amount * 3600
    else:
        seconds = amount * 60
    return -seconds


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


class ServerConfig(_StrictModel):
    base_url: str
    uid_domain: str


class SourceSettings(_StrictModel):
    rate_limit_per_min: int = 28
    refresh_cron: str
    next_season_from: str = "10-01"


class RetentionConfig(_StrictModel):
    historical_days: int = 180
    cancelled_after_event_days: int = 90


class DurationDefaults(_StrictModel):
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
        return _validate_duration_string(value)


class UnknownTimeConfig(_StrictModel):
    mode: str = "all_day"
    summary_suffix: str = " (time TBC)"

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value != "all_day":
            raise ValueError(
                f"Invalid unknown_time.mode: {value!r} (only 'all_day' is currently supported)"
            )
        return value


class DefaultsConfig(_StrictModel):
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


class SeriesConfig(_StrictModel):
    league_id: int
    name: str
    max_round: int
    race_only: bool = False


class RootConfig(_StrictModel):
    server: ServerConfig
    source: SourceSettings
    retention: RetentionConfig
    defaults: DefaultsConfig
    include_non_championship: bool = False
    unknown_time: UnknownTimeConfig
    series: dict[str, SeriesConfig]


def load_config(path: Path) -> RootConfig:
    """Load and validate a config.yaml bundle. Raises ConfigError on any failure."""
    raw = _load_yaml_mapping(path, "config")
    try:
        return RootConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {path}: {exc}") from exc


class PatchMatcher(_StrictModel):
    series: str
    date: str
    contains: str


class PatchConfig(_StrictModel):
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
        return _validate_duration_string(value)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in _VALID_STATUS_NAMES:
            raise ValueError(f"Invalid status: {value!r} (expected one of {sorted(_VALID_STATUS_NAMES)})")
        return value

    @model_validator(mode="after")
    def validate_exactly_one_matcher(self) -> "PatchConfig":
        if bool(self.id_event) == bool(self.match):
            raise ValueError("A patch must set exactly one of id_event or match")
        return self


class SyntheticEventConfig(_StrictModel):
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
        return _validate_duration_string(value)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in _VALID_STATUS_NAMES:
            raise ValueError(f"Invalid status: {value!r} (expected one of {sorted(_VALID_STATUS_NAMES)})")
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


class OverridesConfig(_StrictModel):
    patches: list[PatchConfig] = []
    events: list[SyntheticEventConfig] = []


def load_overrides(path: Path) -> OverridesConfig:
    """Load and validate an overrides.yaml bundle. Raises ConfigError on any failure."""
    raw = _load_yaml_mapping(path, "overrides")
    try:
        return OverridesConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid overrides in {path}: {exc}") from exc
