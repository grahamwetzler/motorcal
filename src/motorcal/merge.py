"""Publication rebuild logic: patch matching (this phase) and, in a later phase,
fingerprinting, sequencing, duration/alarm resolution, and cancellation lifecycle."""
from __future__ import annotations

from dataclasses import dataclass

from motorcal.config import PatchConfig
from motorcal.models import SourceEvent


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
