"""Per-series, ordered-regex session classification. Pure function, no I/O."""
from __future__ import annotations

import re

from motorcal.models import SessionType

_TESTING_ROUND = 500

_F1_RULES: list[tuple[re.Pattern[str], SessionType]] = [
    (re.compile(r"sprint qualifying", re.IGNORECASE), SessionType.SPRINT_QUALIFYING),
    (re.compile(r"sprint", re.IGNORECASE), SessionType.SPRINT),
    (re.compile(r"practice", re.IGNORECASE), SessionType.PRACTICE),
    (re.compile(r"qualifying", re.IGNORECASE), SessionType.QUALIFYING),
    (re.compile(r"^.+ grand prix$", re.IGNORECASE), SessionType.RACE),
]

_WEC_RULES: list[tuple[re.Pattern[str], SessionType]] = [
    (re.compile(r"hyperpole", re.IGNORECASE), SessionType.HYPERPOLE),
    (re.compile(r"qualifying", re.IGNORECASE), SessionType.QUALIFYING),
    (re.compile(r"practice", re.IGNORECASE), SessionType.PRACTICE),
    (re.compile(r"^\d+ hours? of .+$", re.IGNORECASE), SessionType.RACE),
]

_SERIES_RULES: dict[str, list[tuple[re.Pattern[str], SessionType]]] = {
    "f1": _F1_RULES,
    "wec": _WEC_RULES,
}

# Series that expose only race-level events from the provider (no practice/qualifying
# breakdown exists in the source data). Every event in one of these series is a race,
# except round 500 (still testing, checked first, unconditionally).
_RACE_ONLY_SERIES = {"indycar", "imsa"}


def classify_event(series: str, name: str, round_number: int) -> SessionType:
    """Classify one event's session type from its series, name, and round number."""
    if round_number == _TESTING_ROUND:
        return SessionType.TESTING

    if series in _RACE_ONLY_SERIES:
        return SessionType.RACE

    rules = _SERIES_RULES.get(series)
    if rules is None:
        return SessionType.UNKNOWN

    for pattern, session_type in rules:
        if pattern.search(name):
            return session_type

    return SessionType.UNKNOWN
