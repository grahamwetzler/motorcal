"""Scheduled refresh orchestration and runtime config reload.

Both entry points are handed a `Config`/`State` pair they may mutate freely, and
the caller persists only on success -- so a failed cycle leaves the config
directory, the state file, and the served feeds exactly as they were.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from motorcal.config import Config, ConfigError, load_config
from motorcal.merge import RebuildReport, rebuild_publication
from motorcal.models import PublishedEvent
from motorcal.providers.thesportsdb import RateLimiter, build_client, scan_series_season
from motorcal.state import SnapshotState, State, scope_key
from motorcal.sync import sync_snapshot


def seasons_to_fetch(now: datetime, next_season_from: str) -> list[tuple[str, bool]]:
    """Which {season, is_current_season} pairs to fetch on this refresh cycle.

    The current calendar-year season is always included. Once `now` has passed
    `next_season_from` (an "MM-DD" string) for this year, next year's season is
    also included, marked as NOT current -- sync_snapshot uses this flag to decide
    whether an empty snapshot is suspicious.
    """
    month, day = (int(part) for part in next_season_from.split("-"))
    seasons = [(str(now.year), True)]
    cutoff = now.replace(month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
    if now >= cutoff:
        seasons.append((str(now.year + 1), False))
    return seasons


def diagnostics_from_report(report: RebuildReport, now: datetime) -> dict:
    return {
        "updated_at": now.isoformat(),
        "unknown_events": report.unknown_events,
        "events_published": report.events_published,
        "events_cancelled": report.events_cancelled,
        "events_pruned": report.events_pruned,
    }


@dataclass
class RefreshCycleResult:
    series_season_outcomes: dict[str, dict[str, str]]
    published: dict[str, list[PublishedEvent]] | None
    diagnostics: dict | None
    synced_series: set[str] = field(default_factory=set)
    scan_errors: list[str] = field(default_factory=list)


def run_refresh_cycle(
    config: Config, state: State, *, api_key: str, now: datetime
) -> RefreshCycleResult:
    """Scan every series/season, merge what is trustworthy, and rebuild once.

    Mutates `config` (the series event lists) and `state`. Returns the rebuilt
    publication, or `published=None` if nothing was accepted, in which case the
    caller keeps serving what it already has.
    """
    globals_ = config.globals
    client = build_client()
    rate_limiter = RateLimiter(rate_per_minute=globals_.source.rate_limit_per_min)
    outcomes: dict[str, dict[str, str]] = {}
    scan_errors: list[str] = []
    synced: set[str] = set()

    try:
        for series, series_config in config.series.items():
            outcomes[series] = {}
            for season, is_current in seasons_to_fetch(now, globals_.source.next_season_from):
                snapshot = scan_series_season(
                    client, api_key, series_config.league_id, season, series_config.max_round,
                    series=series, include_non_championship=globals_.include_non_championship,
                    rate_limiter=rate_limiter,
                )
                scan_errors.extend(snapshot.diagnostics)

                key = scope_key(series, season)
                previous = state.snapshots.get(key)
                result = sync_snapshot(
                    series_config, snapshot, season=season, now=now.isoformat(),
                    is_current_season=is_current,
                    previous_count=previous.count if previous else None,
                )
                outcomes[series][season] = result.reason or "committed"
                if result.committed:
                    state.snapshots[key] = SnapshotState(
                        last_complete_at=now.isoformat(), count=len(snapshot.events)
                    )
                    synced.add(series)
    finally:
        client.close()

    if not synced:
        return RefreshCycleResult(
            series_season_outcomes=outcomes, published=None, diagnostics=None,
            scan_errors=scan_errors,
        )

    published, report = rebuild_publication(config, state, now=now)
    return RefreshCycleResult(
        series_season_outcomes=outcomes,
        published=published,
        diagnostics=diagnostics_from_report(report, now),
        synced_series=synced,
        scan_errors=scan_errors,
    )


def config_bundle_hash(config_dir: Path) -> str:
    """A content hash over every config file, used to detect a real change."""
    hasher = hashlib.sha256()
    for path in sorted(Path(config_dir).glob("*.yaml")):
        hasher.update(path.name.encode())
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


@dataclass
class ReloadResult:
    reloaded: bool
    config: Config
    published: dict[str, list[PublishedEvent]] | None
    bundle_hash: str | None
    error: str | None
    diagnostics: dict | None = None


def check_and_reload_config(
    config_dir: Path,
    state: State,
    previous_hash: str | None,
    previous_config: Config,
    now: datetime,
) -> ReloadResult:
    """Detect a config change, validate the whole directory, and rebuild.

    `state` must be a throwaway copy: it is mutated during the rebuild, and on any
    failure the caller discards it, leaving the previous config, state, and served
    feeds untouched. `uid_domain` is immutable configuration -- a runtime change is
    rejected outright, since applying it would republish every event under a new UID.
    """
    new_hash = config_bundle_hash(config_dir)
    if new_hash == previous_hash:
        return ReloadResult(
            reloaded=False, config=previous_config, published=None,
            bundle_hash=previous_hash, error=None,
        )

    def rejected(error: str) -> ReloadResult:
        return ReloadResult(
            reloaded=False, config=previous_config, published=None,
            bundle_hash=previous_hash, error=error,
        )

    try:
        new_config = load_config(config_dir)
    except ConfigError as exc:
        return rejected(str(exc))

    if new_config.globals.uid_domain != previous_config.globals.uid_domain:
        return rejected(
            "uid_domain cannot be changed at runtime "
            f"({previous_config.globals.uid_domain!r} -> "
            f"{new_config.globals.uid_domain!r}); restart the service to apply this change"
        )

    try:
        published, report = rebuild_publication(new_config, state, now=now)
    except Exception as exc:  # noqa: BLE001 -- any rebuild failure must be rejected, not crash the job
        return rejected(str(exc))

    return ReloadResult(
        reloaded=True, config=new_config, published=published, bundle_hash=new_hash,
        error=None, diagnostics=diagnostics_from_report(report, now),
    )


REFRESH_JOB_ID = "refresh_job"


def build_scheduler(
    refresh_job, refresh_cron: str, reload_job, reload_interval_seconds: float = 30
) -> BackgroundScheduler:
    """Build (but do not start) the scheduler for the refresh and reload jobs.

    Deliberately single-threaded: the two jobs both swap state onto the running
    app, and `max_instances=1` would only stop each from overlapping *itself*.
    One worker serializes them against each other too.
    """
    scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1)})
    scheduler.add_job(refresh_job, CronTrigger.from_crontab(refresh_cron), id=REFRESH_JOB_ID)
    scheduler.add_job(reload_job, IntervalTrigger(seconds=reload_interval_seconds))
    return scheduler


def reschedule_refresh_job(scheduler: BackgroundScheduler, refresh_cron: str) -> None:
    """Re-point the refresh job at a new cron expression picked up by a hot config reload."""
    scheduler.reschedule_job(REFRESH_JOB_ID, trigger=CronTrigger.from_crontab(refresh_cron))
