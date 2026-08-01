"""Runtime config reload.

The data directory is edited by hand and by the scheduled agent that reads the
official timetables; this module notices those edits and rebuilds the feeds from
them. The `Config`/`State` pair handed in may be mutated freely, and the caller
persists only on success -- so a failed reload leaves the state file and the
served feeds exactly as they were.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from motorcal.config import Config, ConfigError, load_config
from motorcal.merge import rebuild_publication
from motorcal.models import PublishedEvent
from motorcal.state import State


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


def check_and_reload_config(
    config_dir: Path,
    state: State,
    previous_hash: str | None,
    previous_config: Config,
    uid_domain: str,
    now: datetime,
) -> ReloadResult:
    """Detect a config change, validate the whole directory, and rebuild.

    `state` must be a throwaway copy: it is mutated during the rebuild, and on any
    failure the caller discards it, leaving the previous config, state, and served
    feeds untouched. `uid_domain` comes from the caller's environment, not a file,
    so it cannot change between calls -- there is nothing here to reject.
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
        new_config = load_config(config_dir, uid_domain=uid_domain)
    except ConfigError as exc:
        return rejected(str(exc))

    try:
        published, _report = rebuild_publication(new_config, state, now=now)
    except Exception as exc:  # noqa: BLE001 -- any rebuild failure must be rejected, not crash the job
        return rejected(str(exc))

    return ReloadResult(
        reloaded=True, config=new_config, published=published,
        bundle_hash=new_hash, error=None,
    )


def build_scheduler(reload_job, reload_interval_seconds: float = 30) -> BackgroundScheduler:
    """Build (but do not start) the scheduler for the config-reload job.

    Deliberately single-threaded: the job swaps state onto the running app, and one
    worker is all that is needed to keep a slow reload from overlapping the next.
    """
    scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1)})
    scheduler.add_job(reload_job, IntervalTrigger(seconds=reload_interval_seconds))
    return scheduler
