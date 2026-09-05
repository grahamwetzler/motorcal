"""Canonical data models shared across motorcal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from motorcal.config import SessionConfig


class SessionType(str, Enum):
    PRACTICE = "practice"
    WARMUP = "warmup"
    QUALIFYING = "qualifying"
    HYPERPOLE = "hyperpole"
    SPRINT_QUALIFYING = "sprint_qualifying"
    SPRINT = "sprint"
    RACE = "race"
    TESTING = "testing"


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
    event_name: str  # the weekend's EventConfig.name -- for display, not unique alone
    # The weekend's own EventConfig.round (not session.round, which may differ for a
    # double-header's second session) -- paired with event_name, the two together
    # are what actually identify one weekend: a series can and does reuse an event
    # name across seasons (e.g. WEC's "Lone Star Le Mans" every year), each with its
    # own round. Grouping key only, never part of the fingerprint.
    event_round: int | None
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
    url: str | None = None
    alarms: list[str] = field(default_factory=list)


def session_uid(session: SessionConfig, uid_domain: str) -> str:
    """The stable ICS UID for a configured session.

    The `local-` prefix predates the data directory becoming the only source, and
    is kept because changing it would republish every event under a new UID.
    """
    return f"local-{session.uid}@{uid_domain}"
