"""motorcal command-line interface."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from motorcal.config import ConfigError, load_config, load_overrides
from motorcal.refresh import build_scheduler, check_and_reload_config, run_refresh_cycle
from motorcal.store import backup_database, check_integrity, connect, force_advance_all_sequences, init_schema
from motorcal.web import create_app


def _cmd_init_db(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    conn = connect(db_path)
    init_schema(conn)
    conn.close()
    print(f"Initialized database at {db_path}")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    dest_path = Path(args.dest)

    if not db_path.exists():
        print(f"Source database not found: {db_path}", file=sys.stderr)
        return 1

    try:
        conn = connect(db_path)
    except sqlite3.DatabaseError as err:
        print(f"Refusing to back up {db_path}: could not open database ({err})", file=sys.stderr)
        return 1

    integrity_ok = check_integrity(conn)
    conn.close()
    if not integrity_ok:
        print(f"Refusing to back up {db_path}: integrity check failed", file=sys.stderr)
        return 1

    backup_database(db_path, dest_path)
    print(f"Backed up {db_path} to {dest_path}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    config_path = Path(args.config)
    overrides_path = Path(args.overrides)

    try:
        root_config = load_config(config_path)
        overrides = load_overrides(overrides_path)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1

    api_key = os.environ.get("THESPORTSDB_API_KEY")
    tokens_env = os.environ.get("MOTORCAL_TOKENS", "")
    tokens = [t for t in tokens_env.split(",") if t]
    if not api_key or not tokens:
        print(
            "THESPORTSDB_API_KEY and MOTORCAL_TOKENS must both be set", file=sys.stderr
        )
        return 1

    conn = connect(db_path)
    init_schema(conn)
    conn.close()

    state = {
        "root_config": root_config,
        "overrides": overrides,
        "bundle_hash": None,
    }

    def refresh_job():
        conn = connect(db_path)
        try:
            run_refresh_cycle(
                conn, root_config=state["root_config"], overrides=state["overrides"],
                api_key=api_key, uid_domain=state["root_config"].server.uid_domain,
                lease_holder=f"scheduler-{os.getpid()}", lease_ttl_seconds=1800,
                now=datetime.now(timezone.utc),
            )
        finally:
            conn.close()

    def reload_job():
        conn = connect(db_path)
        try:
            result = check_and_reload_config(
                conn, config_path, overrides_path, state["bundle_hash"],
                state["root_config"], state["overrides"],
                state["root_config"].server.uid_domain, datetime.now(timezone.utc),
            )
            if result.reloaded:
                state["root_config"] = result.root_config
                state["overrides"] = result.overrides
            state["bundle_hash"] = result.bundle_hash
        finally:
            conn.close()

    scheduler = build_scheduler(refresh_job, root_config.source.refresh_cron, reload_job)
    scheduler.start()

    app = create_app(db_path, root_config, tokens)
    uvicorn.run(app, host="0.0.0.0", port=8000)
    return 0


def _cmd_validate_config(args: argparse.Namespace) -> int:
    try:
        load_config(Path(args.config))
        load_overrides(Path(args.overrides))
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1
    print("Configuration is valid.")
    return 0


def _cmd_republish(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    conn = connect(db_path)
    now = datetime.now(timezone.utc)
    now_unix_minute = int(now.timestamp() // 60)
    count = force_advance_all_sequences(conn, now_unix_minute, now.isoformat())
    conn.close()
    print(f"Advanced sequence for {count} published event(s) to at least {now_unix_minute}.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motorcal")
    subparsers = parser.add_subparsers(dest="command")

    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument("--db", required=True, help="Path to the SQLite database file")

    init_db_parser = subparsers.add_parser(
        "init-db", parents=[db_parent], help="Create/upgrade the database schema"
    )
    init_db_parser.set_defaults(func=_cmd_init_db)

    backup_parser = subparsers.add_parser(
        "backup", parents=[db_parent], help="Back up the database to another file"
    )
    backup_parser.add_argument("--dest", required=True, help="Path to write the backup to")
    backup_parser.set_defaults(func=_cmd_backup)

    serve_parser = subparsers.add_parser(
        "serve", parents=[db_parent], help="Run the scheduler and HTTP server"
    )
    serve_parser.add_argument("--config", required=True, help="Path to config.yaml")
    serve_parser.add_argument("--overrides", required=True, help="Path to overrides.yaml")
    serve_parser.set_defaults(func=_cmd_serve)

    validate_config_parser = subparsers.add_parser(
        "validate-config", help="Validate config.yaml + overrides.yaml without activating them"
    )
    validate_config_parser.add_argument("--config", required=True, help="Path to config.yaml")
    validate_config_parser.add_argument("--overrides", required=True, help="Path to overrides.yaml")
    validate_config_parser.set_defaults(func=_cmd_validate_config)

    republish_parser = subparsers.add_parser(
        "republish", parents=[db_parent], help="Recovery: force-advance published event sequences"
    )
    republish_parser.add_argument(
        "--force-version", action="store_true", required=True,
        help="Advance every retained event's sequence to at least the current UTC Unix minute",
    )
    republish_parser.set_defaults(func=_cmd_republish)

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
