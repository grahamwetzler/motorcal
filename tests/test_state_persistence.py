from datetime import datetime, timezone

import pytest
import yaml
from tests.conftest import UID_DOMAIN, make_config, make_event, make_series, make_state

from motorcal import state as state_module
from motorcal.merge import rebuild_publication
from motorcal.state import State, VersionState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_load_returns_an_empty_state_when_the_file_is_missing(tmp_path):
    assert state_module.load(tmp_path / "nope.yaml") == State()


def test_load_returns_an_empty_state_for_an_empty_file(tmp_path):
    path = tmp_path / "state.yaml"
    path.write_text("")

    assert state_module.load(path) == State()


def test_save_then_load_round_trips_every_field(tmp_path):
    path = tmp_path / "state.yaml"
    original = State(
        uid_domain=UID_DOMAIN,
        versions={
            "u1": VersionState(
                fingerprint="fp", sequence=42, dtstamp="t3", last_modified="t4"
            )
        },
    )

    state_module.save(path, original)

    assert state_module.load(path) == original


def test_load_ignores_the_removed_status_field(tmp_path):
    path = tmp_path / "state.yaml"
    path.write_text(
        "versions:\n  u1:\n    fingerprint: fp\n    sequence: 42\n    dtstamp: t3\n"
        "    last_modified: t4\n    status: CONFIRMED\n"
    )

    assert state_module.load(path).versions["u1"] == VersionState(
        fingerprint="fp", sequence=42, dtstamp="t3", last_modified="t4"
    )


def test_save_creates_the_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.yaml"

    state_module.save(path, make_state())

    assert state_module.load(path).uid_domain == UID_DOMAIN


def test_save_leaves_no_temporary_files_behind(tmp_path):
    state_module.save(tmp_path / "state.yaml", make_state())

    assert [p.name for p in tmp_path.iterdir()] == ["state.yaml"]


def test_saved_state_is_plain_readable_yaml(tmp_path):
    path = tmp_path / "state.yaml"
    state_module.save(path, make_state())

    assert yaml.safe_load(path.read_text())["uid_domain"] == UID_DOMAIN


def test_load_rejects_a_structurally_invalid_state_file(tmp_path):
    path = tmp_path / "state.yaml"
    path.write_text("versions:\n  u1:\n    fingerprint: fp\n")  # missing required keys

    with pytest.raises(Exception):
        state_module.load(path)


def test_sequence_and_dtstamp_survive_a_save_load_cycle(tmp_path):
    """The whole reason the ledger is persisted: a restart must not re-notify clients."""
    path = tmp_path / "state.yaml"
    config = make_config(series={"wec": make_series(events=[make_event("wec-2026-imola-race", start="2026-04-19T13:00:00+00:00")])})
    state = make_state()
    first, _ = rebuild_publication(config, state, now=NOW)
    state_module.save(path, state)

    reloaded = state_module.load(path)
    second, _ = rebuild_publication(config, reloaded, now=datetime(2026, 6, 1, tzinfo=timezone.utc))

    assert second["wec"][0].sequence == first["wec"][0].sequence
    assert second["wec"][0].dtstamp == first["wec"][0].dtstamp
    assert second["wec"][0].fingerprint == first["wec"][0].fingerprint


def test_a_failed_rebuild_never_reaches_disk(tmp_path):
    """Copy-on-write: the caller discards the working copy, so the file is untouched."""
    path = tmp_path / "state.yaml"
    config = make_config(series={"wec": make_series(events=[make_event("wec-2026-imola-race", start="2026-04-19T13:00:00+00:00")])})
    live = make_state()
    rebuild_publication(config, live, now=NOW)
    state_module.save(path, live)
    on_disk_before = path.read_text()

    working = live.model_copy(deep=True)
    broken = config.model_copy(deep=True)
    broken.series["wec"].events[0].sessions[0].start = "not-a-timestamp"
    with pytest.raises(ValueError):
        rebuild_publication(broken, working, now=NOW)
    # The caller does NOT save on failure -- that is the entire guarantee.

    assert path.read_text() == on_disk_before
    assert state_module.load(path) == live


def test_an_all_day_event_keeps_its_version_across_a_reload(tmp_path):
    path = tmp_path / "state.yaml"
    config = make_config(series={"wec": make_series(events=[make_event("mine")])})
    state = make_state()
    first, _ = rebuild_publication(config, state, now=NOW)
    state_module.save(path, state)

    second, _ = rebuild_publication(
        config, state_module.load(path), now=datetime(2026, 6, 1, tzinfo=timezone.utc)
    )

    assert second["wec"][0].sequence == first["wec"][0].sequence
