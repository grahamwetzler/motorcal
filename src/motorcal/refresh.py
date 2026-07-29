"""Scheduled refresh orchestration and runtime config reload."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from motorcal.config import ConfigError, OverridesConfig, RootConfig, load_config, load_overrides
from motorcal.ics import render_calendar_bytes, sync_feed_revision
from motorcal.merge import PatchMatchError, RebuildReport, rebuild_publication, reconcile_synthetic_events
from motorcal.providers.thesportsdb import RateLimiter, build_client, scan_series_season
from motorcal.store import (
    acquire_lease,
    ingest_snapshot,
    release_lease,
    transaction,
    upsert_refresh_diagnostics,
)


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


@dataclass
class RefreshCycleResult:
    lease_acquired: bool
    series_season_outcomes: dict[str, dict[str, str]]
    rebuild_report: RebuildReport | None


def _serialize_patch_error(error: PatchMatchError) -> dict:
    return {
        "reason": error.reason,
        "candidate_count": error.candidate_count,
        "id_event": error.patch.id_event,
        "match": (
            {
                "series": error.patch.match.series,
                "date": error.patch.match.date,
                "contains": error.patch.match.contains,
            }
            if error.patch.match
            else None
        ),
    }


def run_refresh_cycle(
    conn: sqlite3.Connection,
    *,
    root_config: RootConfig,
    overrides: OverridesConfig,
    api_key: str,
    uid_domain: str,
    lease_holder: str,
    lease_ttl_seconds: float,
    now: datetime,
) -> RefreshCycleResult:
    """Run one complete refresh: scan every series/season, ingest, rebuild, render.

    The lease wraps the whole cycle. If it can't be acquired, the cycle is
    skipped entirely (another tick/worker already holds it) -- this is not an
    error condition.
    """
    if not acquire_lease(conn, lease_holder, lease_ttl_seconds, now=now.timestamp()):
        return RefreshCycleResult(
            lease_acquired=False, series_season_outcomes={}, rebuild_report=None
        )

    try:
        client = build_client()
        rate_limiter = RateLimiter(rate_per_minute=root_config.source.rate_limit_per_min)
        series_season_outcomes: dict[str, dict[str, str]] = {}

        try:
            for series_key, series_config in root_config.series.items():
                series_season_outcomes[series_key] = {}
                for season, is_current in seasons_to_fetch(now, root_config.source.next_season_from):
                    snapshot = scan_series_season(
                        client, api_key, series_config.league_id, season, series_config.max_round,
                        series=series_key, include_non_championship=root_config.include_non_championship,
                        rate_limiter=rate_limiter,
                    )
                    ingest_result = ingest_snapshot(
                        conn, snapshot, provider="thesportsdb", series=series_key, season=season,
                        now=now.isoformat(), is_current_season=is_current,
                    )
                    series_season_outcomes[series_key][season] = ingest_result.reason or "committed"
        finally:
            client.close()

        with transaction(conn):
            reconcile_synthetic_events(conn, overrides.events, now.isoformat())
            report = rebuild_publication(
                conn, root_config=root_config, overrides=overrides, uid_domain=uid_domain, now=now
            )
            upsert_refresh_diagnostics(
                conn,
                now.isoformat(),
                json.dumps([_serialize_patch_error(e) for e in report.patch_errors]),
                json.dumps(report.unknown_events),
                report.events_published,
                report.events_cancelled,
                report.events_pruned,
            )

        for series_key, series_config in root_config.series.items():
            ics_bytes = render_calendar_bytes(conn, series_key, series_config)
            sync_feed_revision(conn, series_key, ics_bytes, now.isoformat())

        return RefreshCycleResult(
            lease_acquired=True,
            series_season_outcomes=series_season_outcomes,
            rebuild_report=report,
        )
    finally:
        release_lease(conn, lease_holder)


def config_bundle_hash(config_path: Path, overrides_path: Path) -> str:
    """A content-based hash of both config files, used to detect a real change."""
    hasher = hashlib.sha256()
    for path in (config_path, overrides_path):
        hasher.update(Path(path).read_bytes())
    return hasher.hexdigest()


@dataclass
class ReloadResult:
    reloaded: bool
    root_config: RootConfig
    overrides: OverridesConfig
    bundle_hash: str | None
    error: str | None


def check_and_reload_config(
    conn: sqlite3.Connection,
    config_path: Path,
    overrides_path: Path,
    previous_hash: str | None,
    previous_root_config: RootConfig,
    previous_overrides: OverridesConfig,
    uid_domain: str,
    now: datetime,
) -> ReloadResult:
    """Detect a config-file change, validate the whole bundle, and rebuild atomically.

    On any failure the previous config/overrides/published state remain
    completely untouched -- validation happens before any database write, and
    reconciliation + rebuild happen inside one transaction so a mid-way
    failure can never leave a half-applied config active.
    """
    new_hash = config_bundle_hash(config_path, overrides_path)
    if new_hash == previous_hash:
        return ReloadResult(
            reloaded=False, root_config=previous_root_config, overrides=previous_overrides,
            bundle_hash=previous_hash, error=None,
        )

    try:
        new_root_config = load_config(config_path)
        new_overrides = load_overrides(overrides_path)
    except ConfigError as exc:
        return ReloadResult(
            reloaded=False, root_config=previous_root_config, overrides=previous_overrides,
            bundle_hash=previous_hash, error=str(exc),
        )

    try:
        with transaction(conn):
            reconcile_synthetic_events(conn, new_overrides.events, now.isoformat())
            rebuild_publication(
                conn, root_config=new_root_config, overrides=new_overrides,
                uid_domain=uid_domain, now=now,
            )
    except Exception as exc:  # noqa: BLE001 -- any rebuild failure must roll back and be reported
        return ReloadResult(
            reloaded=False, root_config=previous_root_config, overrides=previous_overrides,
            bundle_hash=previous_hash, error=str(exc),
        )

    return ReloadResult(
        reloaded=True, root_config=new_root_config, overrides=new_overrides,
        bundle_hash=new_hash, error=None,
    )
