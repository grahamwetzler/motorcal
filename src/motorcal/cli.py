"""motorcal command-line interface."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from motorcal import state as state_module
from motorcal.config import COMBINED_SERIES_KEY, Config, ConfigError, load_config
from motorcal.ics import render_combined_bytes
from motorcal.merge import rebuild_publication
from motorcal.refresh import build_scheduler, check_and_reload_config, config_bundle_hash
from motorcal.web import Publication, create_app

_logger = logging.getLogger("motorcal.serve")


def _render_feeds(config: Config, published) -> dict[str, bytes]:
    return {COMBINED_SERIES_KEY: render_combined_bytes(config, published)}


def _cmd_serve(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    config_dir = Path(args.config)

    uid_domain = os.environ.get("UID_DOMAIN")
    if not uid_domain:
        print("UID_DOMAIN must be set", file=sys.stderr)
        return 1

    try:
        config = load_config(config_dir, uid_domain=uid_domain)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1

    state = state_module.load(state_path)

    # uid_domain is baked into every event's stable ICS UID. Bind it to the state
    # file on first use so a restart with a changed UID_DOMAIN can't sail through
    # and silently duplicate every event under new UIDs.
    if state.uid_domain is None:
        state.uid_domain = uid_domain
    elif state.uid_domain != uid_domain:
        print(
            f"uid_domain has changed ({state.uid_domain!r} -> {uid_domain!r}) since "
            "this state file was created. Changing it would duplicate every "
            "published event under new UIDs. Revert UID_DOMAIN, or perform an "
            "explicit migration (see docs/operations.md) before restarting with "
            "the new domain.",
            file=sys.stderr,
        )
        return 1

    now = datetime.now(timezone.utc)
    published, _report = rebuild_publication(config, state, now=now)
    state_module.save(state_path, state)

    # app.state is the single source of truth for everything the HTTP layer and the
    # reload job read. The job rebuilds against copies and swaps the whole Publication
    # at once on success, so a failed reload leaves the app exactly as it was, and a
    # request mid-swap never sees config from one generation paired with another's feeds.
    app = create_app(config)
    app.state.data = state
    app.state.publication = Publication(
        config=config, published=published, feeds=_render_feeds(config, published)
    )
    app.state.bundle_hash = config_bundle_hash(config_dir)
    app.state.config_dir = config_dir

    def reload_job():
        now = datetime.now(timezone.utc)
        working_state = app.state.data.model_copy(deep=True)
        result = check_and_reload_config(
            config_dir, working_state, app.state.bundle_hash,
            app.state.publication.config, uid_domain, now,
        )
        app.state.bundle_hash = result.bundle_hash
        if not result.reloaded:
            if result.error is not None:
                _logger.warning("Config reload rejected: %s", result.error)
            return

        state_module.save(state_path, working_state)
        app.state.data = working_state
        app.state.publication = Publication(
            config=result.config, published=result.published,
            feeds=_render_feeds(result.config, result.published),
        )

    scheduler = build_scheduler(reload_job)
    scheduler.start()

    uvicorn.run(app, host="0.0.0.0", port=8000)
    return 0


def _cmd_validate_config(args: argparse.Namespace) -> int:
    uid_domain = os.environ.get("UID_DOMAIN")
    if not uid_domain:
        print("UID_DOMAIN must be set", file=sys.stderr)
        return 1

    try:
        config = load_config(Path(args.config), uid_domain=uid_domain)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1
    events = sum(len(s.events) for s in config.series.values())
    sessions = sum(len(s.iter_sessions()) for s in config.series.values())
    print(
        f"Configuration is valid: {len(config.series)} series, "
        f"{events} events, {sessions} sessions."
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motorcal")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser(
        "serve", help="Run the scheduler and HTTP server (feeds on :8000)"
    )
    serve_parser.add_argument("--config", required=True, help="Path to the data directory")
    serve_parser.add_argument("--state", required=True, help="Path to state.yaml")
    serve_parser.set_defaults(func=_cmd_serve)

    validate_config_parser = subparsers.add_parser(
        "validate-config", help="Validate the data directory without activating it"
    )
    validate_config_parser.add_argument(
        "--config", required=True, help="Path to the data directory"
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
