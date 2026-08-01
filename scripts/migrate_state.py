#!/usr/bin/env python3
"""One-shot: bring a pre-TheSportsDB-removal state.yaml up to the current schema.

The old file carries a `snapshots:` block recording when each {series, season}
scope was last fetched, and `thesportsdb-*` version entries for provider-backed
sessions. Neither exists any more, and `State` forbids unknown keys -- so the app
will refuse to start against an untouched file.

Run once, with the container stopped:

    uv run python scripts/migrate_state.py data/state.yaml state/state.yaml

Then delete this script. The `local-*` version entries are what matter: they keep
the 16 hand-added IMSA and IndyCar sessions from being re-notified to every
subscriber.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


def migrate(raw: dict) -> dict:
    versions = raw.get("versions") or {}
    return {
        "uid_domain": raw.get("uid_domain"),
        "versions": {uid: v for uid, v in versions.items() if uid.startswith("local-")},
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 1

    source, destination = Path(argv[0]), Path(argv[1])
    raw = yaml.safe_load(source.read_text()) or {}
    migrated = migrate(raw)

    dropped_versions = len(raw.get("versions") or {}) - len(migrated["versions"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(migrated, sort_keys=False))

    print(
        f"Wrote {destination}: kept {len(migrated['versions'])} local version(s), "
        f"dropped {dropped_versions} provider version(s) and "
        f"{len(raw.get('snapshots') or {})} snapshot scope(s)."
    )
    print(f"{source} is untouched -- keep it until the new feeds look right.")
    return 0


def _self_check() -> None:
    before = {
        "uid_domain": "motorcal.example.com",
        "snapshots": {"f1|2026": {"last_complete_at": "t", "count": 110}},
        "versions": {
            "thesportsdb-2408107@motorcal.example.com": {"sequence": 1},
            "local-imsa-2026-sebring-qualifying@motorcal.example.com": {"sequence": 2},
        },
    }

    after = migrate(before)

    assert after == {
        "uid_domain": "motorcal.example.com",
        "versions": {"local-imsa-2026-sebring-qualifying@motorcal.example.com": {"sequence": 2}},
    }, after
    assert migrate({}) == {"uid_domain": None, "versions": {}}


if __name__ == "__main__":
    _self_check()
    sys.exit(main(sys.argv[1:]))
