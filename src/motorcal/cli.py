"""motorcal command-line interface."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from motorcal.store import backup_database, check_integrity, connect, init_schema


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
