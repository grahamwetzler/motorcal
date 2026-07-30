"""motorcal command-line interface."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from motorcal import state as state_module
from motorcal.admin import create_admin_app
from motorcal.config import Config, ConfigError, load_config, save_series
from motorcal.ics import render_calendar_bytes
from motorcal.merge import rebuild_publication
from motorcal.refresh import (
    build_scheduler,
    check_and_reload_config,
    config_bundle_hash,
    diagnostics_from_report,
    reschedule_refresh_job,
    run_refresh_cycle,
)
from motorcal.web import create_app

_logger = logging.getLogger("motorcal.serve")


def _render_feeds(config: Config, published) -> dict[str, bytes]:
    return {
        series: render_calendar_bytes(series_config, published.get(series, []))
        for series, series_config in config.series.items()
    }


def _cmd_serve(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    config_dir = Path(args.config)

    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1

    api_key = os.environ.get("THESPORTSDB_API_KEY")
    if not api_key:
        print("THESPORTSDB_API_KEY must be set", file=sys.stderr)
        return 1

    state = state_module.load(state_path)

    # uid_domain is baked into every event's stable ICS UID. A hot reload already
    # rejects changing it at runtime; bind it to the state file on first use so a
    # full restart with an edited motorcal.yaml can't sail through and silently
    # duplicate every event under new UIDs.
    if state.uid_domain is None:
        state.uid_domain = config.globals.uid_domain
    elif state.uid_domain != config.globals.uid_domain:
        print(
            f"uid_domain has changed ({state.uid_domain!r} -> "
            f"{config.globals.uid_domain!r}) since this state file was created. "
            "Changing it would duplicate every published event under new UIDs. "
            "Revert motorcal.yaml, or perform an explicit migration (see "
            "docs/operations.md) before restarting with the new domain.",
            file=sys.stderr,
        )
        return 1

    now = datetime.now(timezone.utc)
    published, report = rebuild_publication(config, state, now=now)
    state_module.save(state_path, state)

    # app.state is the single source of truth for everything the HTTP layer and the
    # scheduler jobs read. Jobs rebuild against copies and swap all of these at once
    # on success, so a failed cycle leaves the app exactly as it was.
    app = create_app(config)
    app.state.data = state
    app.state.published = published
    app.state.feeds = _render_feeds(config, published)
    app.state.diagnostics = diagnostics_from_report(report, now)
    app.state.bundle_hash = config_bundle_hash(config_dir)
    app.state.config_dir = config_dir

    def refresh_job():
        now = datetime.now(timezone.utc)

        # Re-read from disk rather than copying app.state.config. This job writes
        # series files back, so merging into an in-memory snapshot would silently
        # revert any edit made since the last successful reload -- and if reloads
        # are being rejected (a typo in one series file), that snapshot is stale
        # indefinitely and the revert would span every series.
        try:
            working_config = load_config(config_dir)
        except ConfigError as exc:
            _logger.warning("Refresh skipped, config is currently invalid: %s", exc)
            return
        if working_config.globals.uid_domain != app.state.data.uid_domain:
            _logger.warning(
                "Refresh skipped: uid_domain on disk (%r) does not match the one this "
                "state file is bound to (%r)",
                working_config.globals.uid_domain, app.state.data.uid_domain,
            )
            return

        working_state = app.state.data.model_copy(deep=True)

        result = run_refresh_cycle(working_config, working_state, api_key=api_key, now=now)
        for error in result.scan_errors:
            _logger.warning("Provider scan: %s", error)
        if result.diagnostics is not None:
            app.state.diagnostics = result.diagnostics
        if result.published is None:
            _logger.warning("Refresh published nothing: %s", result.series_season_outcomes)
            return  # the working copies are discarded unpersisted

        # Series files first: if writing one fails, the exception propagates before
        # app.state is touched, so the app keeps serving a publication that matches
        # what is actually on disk.
        for series in sorted(result.synced_series):
            save_series(config_dir, series, working_config.series[series])
        state_module.save(state_path, working_state)

        app.state.config = working_config
        app.state.data = working_state
        app.state.published = result.published
        app.state.feeds = _render_feeds(working_config, result.published)
        # We just rewrote the config ourselves; adopt the new hash so the reload job
        # doesn't mistake our own writes for a hand edit and rebuild all over again.
        app.state.bundle_hash = config_bundle_hash(config_dir)

    def reload_job():
        now = datetime.now(timezone.utc)
        working_state = app.state.data.model_copy(deep=True)
        result = check_and_reload_config(
            config_dir, working_state, app.state.bundle_hash, app.state.config, now
        )
        app.state.bundle_hash = result.bundle_hash
        if result.diagnostics is not None:
            app.state.diagnostics = result.diagnostics
        if not result.reloaded:
            if result.error is not None:
                _logger.warning("Config reload rejected: %s", result.error)
            return

        # Reschedule before swapping in the new config: if this raised, the old
        # (still-active) config and schedule must stay consistent with each other.
        if result.config.globals.source.refresh_cron != app.state.config.globals.source.refresh_cron:
            reschedule_refresh_job(scheduler, result.config.globals.source.refresh_cron)

        state_module.save(state_path, working_state)
        app.state.config = result.config
        app.state.data = working_state
        app.state.published = result.published
        app.state.feeds = _render_feeds(result.config, result.published)

    scheduler = build_scheduler(refresh_job, config.globals.source.refresh_cron, reload_job)
    scheduler.start()

    admin_app = create_admin_app(config_dir, app)
    threading.Thread(
        target=uvicorn.run, args=(admin_app,), kwargs={"host": "0.0.0.0", "port": 8001}, daemon=True,
    ).start()

    uvicorn.run(app, host="0.0.0.0", port=8000)
    return 0


def _cmd_validate_config(args: argparse.Namespace) -> int:
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1
    total = sum(len(s.events) for s in config.series.values())
    print(f"Configuration is valid: {len(config.series)} series, {total} events.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motorcal")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser(
        "serve", help="Run the scheduler and HTTP servers (feeds on :8000, admin on :8001)"
    )
    serve_parser.add_argument("--config", required=True, help="Path to the config directory")
    serve_parser.add_argument("--state", required=True, help="Path to state.yaml")
    serve_parser.set_defaults(func=_cmd_serve)

    validate_config_parser = subparsers.add_parser(
        "validate-config", help="Validate the config directory without activating it"
    )
    validate_config_parser.add_argument(
        "--config", required=True, help="Path to the config directory"
    )
    validate_config_parser.set_defaults(func=_cmd_validate_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_usage(sys.stderr)
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
