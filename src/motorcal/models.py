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
    # A weekend's own identity, for grouping sessions back to the EventConfig they
    # came from -- name and round are not safe for this: a series reuses an event
    # name across seasons (WEC's "Lone Star Le Mans" every year), and round numbers
    # are seasonal ordinals that can coincide across two different seasons. Every
    # session uid is enforced unique across the whole data directory (config.py's
    # `load_config`), so the weekend's first session's uid is a safe, stable stand-in
    # for "which EventConfig this came from" without carrying the object itself
    # through rendering. Grouping key only, never part of the fingerprint.
    event_key: str
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
