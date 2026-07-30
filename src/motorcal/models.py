"""Canonical data models shared across motorcal."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from motorcal.config import EventConfig


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


@dataclass
class PublishedEvent:
    """A fully resolved event ready for ICS rendering."""

    uid: str
    series: str
    session_type: SessionType
    summary: str
    start: datetime | None  # None when the time is unconfirmed (rendered all-day)
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
    event_key: str = ""  # the id_event/uid this was built from, for config lookups


def event_uid(event: "EventConfig", uid_domain: str) -> str:
    """The stable ICS UID for a configured event.

    Provider-backed and manual events keep distinct prefixes so a manual event can
    never collide with a provider id, and so the UID a subscriber already has does
    not change if an event later gains or loses provider backing.
    """
    if event.id_event is not None:
        return f"thesportsdb-{event.id_event}@{uid_domain}"
    return f"local-{event.uid}@{uid_domain}"
