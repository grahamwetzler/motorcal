"""Scheduled refresh orchestration and runtime config reload."""
from __future__ import annotations

from datetime import datetime


def seasons_to_fetch(now: datetime, next_season_from: str) -> list[tuple[str, bool]]:
    """Which {season, is_current_season} pairs to fetch on this refresh cycle.

    The current calendar-year season is always included. Once `now` has passed
    `next_season_from` (an "MM-DD" string) for this year, next year's season is
    also included, marked as NOT current -- Phase 4's ingest_snapshot uses this
    flag to decide whether an empty snapshot is suspicious.
    """
    month, day = (int(part) for part in next_season_from.split("-"))
    current_year = now.year
    seasons = [(str(current_year), True)]
    cutoff = now.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
    if now >= cutoff:
        seasons.append((str(current_year + 1), False))
    return seasons
