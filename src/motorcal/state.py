"""The machine-only sidecar: data/state.yaml.

Everything a human writes lives in the data directory. This file holds only what
the app must remember and nobody should have to read: which uid_domain the feed is
keyed under, when each scope was last fetched completely, and the per-UID version
ledger that keeps ICS SEQUENCE/DTSTAMP stable so calendar clients don't re-notify
subscribers on every refresh.

Deleting it is recoverable -- the next refresh rebuilds it -- but every subscriber
sees the whole calendar as modified once.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from motorcal.config import StrictModel


def scope_key(series: str, season: str) -> str:
    """The `snapshots` key for one {series, season} scope."""
    return f"{series}|{season}"


class SnapshotState(StrictModel):
    """When one {series, season} scope was last fetched completely, and how much of it."""

    last_complete_at: str
    count: int


class VersionState(StrictModel):
    """The previously published version of one UID: what ICS clients already saw."""

    fingerprint: str
    sequence: int
    dtstamp: str
    last_modified: str
    status: str


class State(StrictModel):
    uid_domain: str | None = None
    snapshots: dict[str, SnapshotState] = {}
    versions: dict[str, VersionState] = {}


def load(path: Path) -> State:
    """Read the state file. A missing (or empty) file is a valid empty state."""
    path = Path(path)
    if not path.exists():
        return State()
    raw = yaml.safe_load(path.read_text()) or {}
    return State.model_validate(raw)


def save(path: Path, state: State) -> None:
    """Write the state file atomically: full write + fsync, then a single rename.

    A crash leaves either the previous file intact or the new one complete, never a
    truncated mix -- a partial ledger would silently re-notify every subscriber.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(state.model_dump(mode="json"), f, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)
