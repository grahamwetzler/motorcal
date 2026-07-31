"""Session classification from a session's label. Pure function, no I/O.

The label is what a provider event's name has left over once the race event's own
name is stripped off it -- "Australian Grand Prix Practice 1" under the event
"Australian Grand Prix" leaves "Practice 1". That makes classification series
agnostic: an empty label is the session named after the event itself, i.e. the
race, and everything else is named after what it is.
"""
from __future__ import annotations

import re

from motorcal.models import SessionType

_TESTING_ROUND = 500

# Ordered: the first match wins, so the more specific label comes first
# ("Sprint Qualifying" is not a plain sprint, "Hyperpole Qualifying" is not a
# plain qualifying).
_RULES: list[tuple[re.Pattern[str], SessionType]] = [
    (re.compile(r"sprint qualifying", re.IGNORECASE), SessionType.SPRINT_QUALIFYING),
    (re.compile(r"sprint", re.IGNORECASE), SessionType.SPRINT),
    (re.compile(r"hyperpole", re.IGNORECASE), SessionType.HYPERPOLE),
    (re.compile(r"qualifying", re.IGNORECASE), SessionType.QUALIFYING),
    (re.compile(r"practice", re.IGNORECASE), SessionType.PRACTICE),
    # Last: a weekend running two races labels them "Race 1"/"Race 2" rather than
    # leaving the label empty, and those are still races.
    (re.compile(r"\brace\b", re.IGNORECASE), SessionType.RACE),
]

# A trailing session word and anything after it, for recovering a weekend's name
# from session names that share no full-name prefix ("... 250 Race #1" -> "... 250").
_SESSION_SUFFIX_RE = re.compile(
    r"\s+(sprint qualifying|free practice|practice|sprint|hyperpole|qualifying|race)\b.*$",
    re.IGNORECASE,
)


def strip_session_suffix(name: str) -> str:
    """Drop a trailing session word from a name, leaving the weekend it belongs to."""
    return _SESSION_SUFFIX_RE.sub("", name).strip()


def classify_session(label: str, round_number: int) -> SessionType:
    """Classify one session from its label and its event's round number."""
    if round_number == _TESTING_ROUND:
        return SessionType.TESTING

    if not label:
        return SessionType.RACE

    for pattern, session_type in _RULES:
        if pattern.search(label):
            return session_type

    return SessionType.UNKNOWN
