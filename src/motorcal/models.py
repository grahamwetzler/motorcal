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
