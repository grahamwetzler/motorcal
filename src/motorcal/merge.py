"""Publication rebuild logic: patch matching (this phase) and, in a later phase,
fingerprinting, sequencing, duration/alarm resolution, and cancellation lifecycle."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from motorcal.config import PatchConfig, SyntheticEventConfig, parse_duration
from motorcal.models import SourceEvent
from motorcal.store import (
    list_synthetic_events,
    mark_synthetic_event_removed,
    transaction,
    upsert_synthetic_event,
)


@dataclass
class PatchMatchError:
    """A patch that did not match exactly one source event."""

    patch: PatchConfig
    reason: str  # "no_match" or "multiple_matches"
    candidate_count: int


@dataclass
class MatchedPatch:
    """A patch successfully paired with the single source event it modifies."""

    patch: PatchConfig
    source_event: SourceEvent


def _find_candidates(patch: PatchConfig, source_events: list[SourceEvent]) -> list[SourceEvent]:
    if patch.id_event is not None:
        return [e for e in source_events if e.key.id_event == patch.id_event]

    matcher = patch.match
    assert matcher is not None  # config-schema validation (Phase 1) guarantees exactly one is set
    needle = matcher.contains.lower()
    return [
        e
        for e in source_events
        if e.series == matcher.series and e.date == matcher.date and needle in e.name.lower()
    ]


def match_all_patches(
    patches: list[PatchConfig], source_events: list[SourceEvent]
) -> tuple[list[MatchedPatch], list[PatchMatchError]]:
    """Match every patch against source_events, requiring exactly one candidate each."""
    matches: list[MatchedPatch] = []
    errors: list[PatchMatchError] = []

    for patch in patches:
        candidates = _find_candidates(patch, source_events)
        if len(candidates) == 1:
            matches.append(MatchedPatch(patch=patch, source_event=candidates[0]))
        elif len(candidates) == 0:
            errors.append(PatchMatchError(patch=patch, reason="no_match", candidate_count=0))
        else:
            errors.append(
                PatchMatchError(
                    patch=patch, reason="multiple_matches", candidate_count=len(candidates)
                )
            )

    return matches, errors


def reconcile_synthetic_events(
    conn: sqlite3.Connection, synthetic_configs: list[SyntheticEventConfig], now: str
) -> None:
    """Sync configured synthetic events into storage, marking removed ones.

    Every event currently in synthetic_configs is upserted (reactivating it if it
    was previously removed). Every stored synthetic event whose uid is NOT in
    synthetic_configs, and that is not already cancelled, is marked removed at `now`.
    """
    configured_uids = {cfg.uid for cfg in synthetic_configs}

    with transaction(conn):
        for cfg in synthetic_configs:
            upsert_synthetic_event(
                conn,
                uid=cfg.uid,
                series=cfg.series,
                summary=cfg.summary,
                start=cfg.start,
                date=cfg.date,
                duration_seconds=parse_duration(cfg.duration) if cfg.duration else None,
                location=cfg.location,
                status=cfg.status or "CONFIRMED",
                note=cfg.note,
                alarms_json=json.dumps(cfg.alarms),
            )

        for row in list_synthetic_events(conn):
            if row["uid"] not in configured_uids and row["cancelled_at"] is None:
                mark_synthetic_event_removed(conn, row["uid"], now)
