"""Configuration schema and loader.

The data directory is the source of truth -- the only one. `defaults.yaml` holds
the settings that can't belong to any one series; every other `*.yaml` file is
one series, keyed by its filename stem, holding that series' settings and its
full event list.

Nothing in the app writes these files: they are maintained by hand and by the
scheduled agent that reads the official timetables. So this module only loads
them, and comments in them survive.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

from motorcal.models import EventStatus, SessionType, session_uid

GLOBAL_FILENAME = "defaults.yaml"
# Served at /events.ics as every series combined, so no series file may claim it.
COMBINED_SERIES_KEY = "events"

_DURATION_RE = re.compile(r"^([1-9]\d*)(h|m)$")
# Digits capped at 5 so a pathological offset never reaches int() as a long
# digit string; the real bound is MAX_ALARM_OFFSET_SECONDS below.
_ALARM_OFFSET_RE = re.compile(r"^0[dhm]?$|^-[1-9]\d{0,4}[dhm]$")
# No alert needs more than a week's notice. Also keeps parse_alarm_offset's
# result far under timedelta's range, so a crafted offset can't overflow it
# building the VALARM in ics.py.
MAX_ALARM_OFFSET_SECONDS = 7 * 86400
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
        try:
            parse_alarm_offset(offset)
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
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
        raise ConfigError(
            f"Invalid duration string: {value!r} (expected e.g. '1h', '45m')"
        )
    amount, unit = match.groups()
    return int(amount) * (3600 if unit == "h" else 60)


def parse_alarm_offset(value: str) -> int:
    """Parse an alarm-offset string like '-1d' or '-30m' into whole seconds (negative).

    '0' means an alert exactly at event start.
    """
    if not _ALARM_OFFSET_RE.match(value):
        raise ConfigError(
            f"Invalid alarm offset: {value!r} (expected e.g. '-1d', '-30m', '0')"
        )
    if value[0] == "0":
        return 0
    amount, unit = int(value[1:-1]), value[-1]
    seconds = amount * {"d": 86400, "h": 3600, "m": 60}[unit]
    if seconds > MAX_ALARM_OFFSET_SECONDS:
        raise ConfigError(
            f"Alarm offset {value!r} exceeds the {MAX_ALARM_OFFSET_SECONDS // 86400}-day maximum"
        )
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


# --------------------------------------------------------------------------- globals


class RetentionConfig(StrictModel):
    historical_days: int = 180
    cancelled_after_event_days: int = 90


def _validate_durations_dict(value: dict[str, str]) -> dict[str, str]:
    unknown = set(value) - _VALID_SESSION_NAMES
    if unknown:
        raise ValueError(f"Unknown session type(s) in durations: {sorted(unknown)}")
    for session, duration in value.items():
        try:
            _validate_duration_string(duration)
        except ValueError as exc:
            raise ValueError(f"{exc} for session {session!r}") from exc
    return value


class UnknownTimeConfig(StrictModel):
    summary_suffix: str = " (time TBC)"


class DefaultsConfig(StrictModel):
    durations: dict[str, str] = {}
    alerts: dict[str, list[str]] = {}

    @field_validator("durations")
    @classmethod
    def validate_durations(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_durations_dict(value)

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
    retention: RetentionConfig = RetentionConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    unknown_time: UnknownTimeConfig = UnknownTimeConfig()


# --------------------------------------------------------------------------- events


class SessionConfig(StrictModel):
    """One session of a race event: a practice, a qualifying, the race itself.

    Everything here is specific to this session; whatever the whole weekend shares
    (name, location, round) lives once on the `EventConfig` holding it.

    `uid` is this session's identity: it is what the published ICS UID is built
    from, so renaming one republishes the session as a new event for subscribers.
    """

    uid: str
    label: str = ""  # appended to the event name: "Practice 1", "Qualifying", "Race"
    type: SessionType
    start: str | None = None  # confirmed time, ISO 8601
    date: str | None = None  # all-day, "YYYY-MM-DD"
    tbc: bool = False  # the official timetable hasn't announced this session's time
    duration: str | None = None
    status: str = EventStatus.CONFIRMED.value
    note: str | None = None
    alarms: list[str] | None = None  # None = fall back to series/global defaults
    round: int | None = None  # only when it differs from the event's -- see EventConfig

    @field_validator("uid")
    @classmethod
    def validate_uid(cls, value: str) -> str:
        # An empty uid still satisfies `str`, and would publish every session that
        # has one under the same "local-@<domain>" identity.
        if not value.strip():
            raise ValueError("A session's uid must not be empty")
        return value

    @field_validator("start")
    @classmethod
    def validate_start(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("start must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("start must include a UTC offset")
        return value

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date must be an ISO 8601 date") from exc
        return value

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
    def validate_timing(self) -> SessionConfig:
        if (self.start is None) == (self.date is None):
            raise ValueError("A session must set exactly one of start or date")
        if self.tbc and self.start is not None:
            raise ValueError("A session with a confirmed start cannot also be tbc")
        return self


class EventConfig(StrictModel):
    """One race event -- a weekend -- and its list of sessions.

    Name, location and round are stored once here rather than repeated on every
    session, and each published session is summarised as "{name} {label}".

    `round` is the weekend's first championship round. A double-header runs two
    rounds over one weekend; its second race carries its own `round` to say so.
    """

    name: str
    url: HttpUrl | None = None
    location: str | None = None
    round: int | None = None
    sessions: list[SessionConfig] = []

    @model_validator(mode="after")
    def validate_has_sessions(self) -> EventConfig:
        if not self.sessions:
            raise ValueError(f"Event {self.name!r} has no sessions")
        return self


class SeriesConfig(StrictModel):
    """One data/<series>.yaml. The series key is the filename stem."""

    name: str
    schedule_url: str | None = (
        None  # the official timetable this series is kept in step with
    )
    durations: dict[str, str] | None = None
    alerts: dict[str, list[str]] | None = None
    events: list[EventConfig] = []

    @field_validator("durations")
    @classmethod
    def validate_durations(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _validate_durations_dict(value) if value is not None else None

    @field_validator("alerts")
    @classmethod
    def validate_alerts(
        cls, value: dict[str, list[str]] | None
    ) -> dict[str, list[str]] | None:
        return _validate_alerts_dict(value) if value is not None else None

    @model_validator(mode="after")
    def validate_unique_session_keys(self) -> SeriesConfig:
        seen: set[str] = set()
        for _, session in self.iter_sessions():
            if session.uid in seen:
                raise ValueError(
                    f"Duplicate session uid in this series: {session.uid!r}"
                )
            seen.add(session.uid)
        return self

    def iter_sessions(self) -> list[tuple[EventConfig, SessionConfig]]:
        """Every (event, session) pair in this series, in file order."""
        return [(event, session) for event in self.events for session in event.sessions]


# --------------------------------------------------------------------------- loading


class Config(StrictModel):
    """The whole data directory: globals plus every series, keyed by filename stem."""

    globals: GlobalConfig
    series: dict[str, SeriesConfig]


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
        globals_ = GlobalConfig.model_validate({
            **raw_globals,
            "uid_domain": uid_domain,
        })
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {global_path}: {exc}") from exc

    series: dict[str, SeriesConfig] = {}
    for path in sorted(config_dir.glob("*.yaml")):
        # state.yaml (and its dated backups, see docs/operations.md) share this
        # directory but aren't series data -- skip them like the global file.
        if (
            path.name == GLOBAL_FILENAME
            or path.name.startswith(".")
            or path.name.startswith("state")
        ):
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
        raise ConfigError(
            f"No series files found in {config_dir} (expected e.g. f1.yaml)"
        )

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
                    f"Duplicate session uid {session.uid!r} in {key}.yaml: already used in "
                    f"{owner[uid]}.yaml. A uid must be unique across the whole data directory."
                )
            owner[uid] = key

            # A timed session with no duration anywhere publishes as a zero-length
            # event, which reads as a glitch in a calendar client rather than an
            # omission. Same three tiers merge.resolve_duration walks.
            if (
                session.start is not None
                and session.duration is None
                and session.type.value not in (series_config.durations or {})
                and session.type.value not in globals_.defaults.durations
            ):
                raise ConfigError(
                    f"Session {session.uid!r} in {key}.yaml has no duration: set one on the "
                    f"session, or add a {session.type.value!r} default to {key}.yaml or "
                    f"{GLOBAL_FILENAME}."
                )

    return Config(globals=globals_, series=series)
