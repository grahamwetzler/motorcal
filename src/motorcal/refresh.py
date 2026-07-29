"""Scheduled refresh orchestration and runtime config reload."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from motorcal.config import ConfigError, OverridesConfig, RootConfig, load_config, load_overrides
from motorcal.ics import render_calendar_bytes, sync_feed_revision
from motorcal.merge import (
    PatchMatchError,
    PublicationBlockedError,
    RebuildReport,
    rebuild_publication,
    reconcile_synthetic_events,
)
from motorcal.providers.thesportsdb import RateLimiter, build_client, scan_series_season
from motorcal.store import (
    acquire_lease,
    current_lease_holder,
    ingest_snapshot,
    list_published_events,
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
    lease_lost: bool = False


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


def _record_blocked_diagnostics(
    conn: sqlite3.Connection, now_iso: str, exc: PublicationBlockedError
) -> None:
    """Record why a rebuild was blocked, in a standalone commit that never touches
    source/synthetic/published state -- only diagnostics, so /status can explain the
    block without reopening the atomic guarantee around the rest of the rebuild."""
    with transaction(conn):
        current = list_published_events(conn)
        events_cancelled = sum(1 for row in current if row["status"] == "CANCELLED")
        upsert_refresh_diagnostics(
            conn,
            now_iso,
            json.dumps([_serialize_patch_error(e) for e in exc.patch_errors]),
            json.dumps(exc.unknown_events),
            len(current),
            events_cancelled,
            0,
        )


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

    # A real monotonic clock reading, independent of the (possibly fixed/injected)
    # `now` used for business-date logic: it's what lets the mid-cycle lease
    # renewal check below detect real elapsed wall-clock time during potentially
    # slow network scans, without perturbing deterministic tests that pass a
    # fixed `now` (where elapsed real time stays effectively zero).
    cycle_started_at = time.monotonic()

    try:
        client = build_client()
        rate_limiter = RateLimiter(rate_per_minute=root_config.source.rate_limit_per_min)
        series_season_outcomes: dict[str, dict[str, str]] = {}
        report: RebuildReport | None = None
        lease_lost = False

        try:
            for series_key, series_config in root_config.series.items():
                series_season_outcomes[series_key] = {}
                for season, is_current in seasons_to_fetch(now, root_config.source.next_season_from):
                    snapshot = scan_series_season(
                        client, api_key, series_config.league_id, season, series_config.max_round,
                        series=series_key, include_non_championship=root_config.include_non_championship,
                        rate_limiter=rate_limiter,
                    )

                    # A scan can run long enough (retries/backoff) to outlive the lease's
                    # TTL. Re-check ownership immediately before writing, advancing the
                    # cycle's logical `now` by real elapsed wall-clock time, so losing the
                    # lease actually prevents a commit rather than just being checked once
                    # at the very start of the cycle.
                    elapsed = time.monotonic() - cycle_started_at
                    if current_lease_holder(conn, now=now.timestamp() + elapsed) != lease_holder:
                        lease_lost = True
                        break

                    # Replacing the source snapshot and rebuilding the published events it
                    # affects happen in the same transaction, so a crash can never expose
                    # new source state alongside an old, unrebuilt publication.
                    try:
                        with transaction(conn):
                            ingest_result = ingest_snapshot(
                                conn, snapshot, provider="thesportsdb", series=series_key, season=season,
                                now=now.isoformat(), is_current_season=is_current,
                            )
                            if ingest_result.committed:
                                reconcile_synthetic_events(conn, overrides.events, now.isoformat())
                                report = rebuild_publication(
                                    conn, root_config=root_config, overrides=overrides,
                                    uid_domain=uid_domain, now=now,
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
                    except PublicationBlockedError as exc:
                        # The whole ingest+rebuild transaction rolled back, including this
                        # series' own ingest -- the previously valid published state (and
                        # source state) remains active in full. A later series/season in
                        # this same cycle may still resolve the failing patch (e.g. it
                        # targets an event that hasn't been scanned yet), so keep going.
                        _record_blocked_diagnostics(conn, now.isoformat(), exc)
                        series_season_outcomes[series_key][season] = "patch_error_blocked"
                        continue

                    series_season_outcomes[series_key][season] = ingest_result.reason or "committed"
                if lease_lost:
                    break
        finally:
            client.close()

        if not lease_lost:
            for series_key, series_config in root_config.series.items():
                ics_bytes = render_calendar_bytes(conn, series_key, series_config)
                sync_feed_revision(conn, series_key, ics_bytes, now.isoformat())

        return RefreshCycleResult(
            lease_acquired=True,
            series_season_outcomes=series_season_outcomes,
            rebuild_report=report,
            lease_lost=lease_lost,
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
    now: datetime,
) -> ReloadResult:
    """Detect a config-file change, validate the whole bundle, and rebuild atomically.

    On any failure the previous config/overrides/published state remain
    completely untouched -- validation happens before any database write, and
    reconciliation + rebuild happen inside one transaction so a mid-way
    failure can never leave a half-applied config active. `uid_domain` is
    explicit immutable configuration: a runtime change is rejected outright,
    since applying it would silently republish every event under a new UID.
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

    if new_root_config.server.uid_domain != previous_root_config.server.uid_domain:
        return ReloadResult(
            reloaded=False, root_config=previous_root_config, overrides=previous_overrides,
            bundle_hash=previous_hash,
            error=(
                "server.uid_domain cannot be changed at runtime "
                f"({previous_root_config.server.uid_domain!r} -> "
                f"{new_root_config.server.uid_domain!r}); restart the service to apply this change"
            ),
        )

    try:
        with transaction(conn):
            reconcile_synthetic_events(conn, new_overrides.events, now.isoformat())
            report = rebuild_publication(
                conn, root_config=new_root_config, overrides=new_overrides,
                uid_domain=new_root_config.server.uid_domain, now=now,
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
    except PublicationBlockedError as exc:
        # The whole reconcile+rebuild transaction rolled back -- record why separately
        # so /status still explains it, then reject the reload outright: the new
        # bundle never becomes active, exactly like a schema validation failure.
        _record_blocked_diagnostics(conn, now.isoformat(), exc)
        return ReloadResult(
            reloaded=False, root_config=previous_root_config, overrides=previous_overrides,
            bundle_hash=previous_hash, error=str(exc),
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


REFRESH_JOB_ID = "refresh_job"


def build_scheduler(
    refresh_job, refresh_cron: str, reload_job, reload_interval_seconds: float = 30
) -> BackgroundScheduler:
    """Build (but do not start) a scheduler running refresh_job on refresh_cron
    and reload_job on a fixed interval."""
    scheduler = BackgroundScheduler()
    scheduler.add_job(refresh_job, CronTrigger.from_crontab(refresh_cron), id=REFRESH_JOB_ID)
    scheduler.add_job(reload_job, IntervalTrigger(seconds=reload_interval_seconds))
    return scheduler


def reschedule_refresh_job(scheduler: BackgroundScheduler, refresh_cron: str) -> None:
    """Re-point the refresh job at a new cron expression picked up by a hot config reload."""
    scheduler.reschedule_job(REFRESH_JOB_ID, trigger=CronTrigger.from_crontab(refresh_cron))
