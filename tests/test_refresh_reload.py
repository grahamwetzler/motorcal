from datetime import datetime, timezone
from pathlib import Path

from motorcal.config import OverridesConfig, load_config
from motorcal.refresh import check_and_reload_config, config_bundle_hash
from motorcal.store import connect, get_published_event, init_schema

EXAMPLE_CONFIG = Path("config/config.example.yaml")
EXAMPLE_OVERRIDES = Path("config/overrides.example.yaml")
UID_DOMAIN = "racing.example.com"  # matches config.example.yaml's uid_domain


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def test_config_bundle_hash_changes_when_content_changes(tmp_path):
    config_a = tmp_path / "a.yaml"
    config_a.write_text("hello")
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("patches: []\nevents: []\n")

    hash1 = config_bundle_hash(config_a, overrides)
    config_a.write_text("goodbye")
    hash2 = config_bundle_hash(config_a, overrides)

    assert hash1 != hash2


def test_config_bundle_hash_is_stable_for_unchanged_content(tmp_path):
    config_a = tmp_path / "a.yaml"
    config_a.write_text("hello")
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("patches: []\nevents: []\n")

    assert config_bundle_hash(config_a, overrides) == config_bundle_hash(config_a, overrides)


def test_reload_skips_when_bundle_unchanged(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    overrides = OverridesConfig()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    previous_hash = config_bundle_hash(EXAMPLE_CONFIG, EXAMPLE_OVERRIDES)

    result = check_and_reload_config(
        conn, EXAMPLE_CONFIG, EXAMPLE_OVERRIDES, previous_hash, root_config, overrides,
        UID_DOMAIN, now,
    )

    assert result.reloaded is False
    assert result.error is None
    assert result.root_config is root_config  # untouched, same object


def test_reload_succeeds_on_first_load_and_rebuilds(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    overrides = OverridesConfig()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = check_and_reload_config(
        conn, EXAMPLE_CONFIG, EXAMPLE_OVERRIDES, None, root_config, overrides, UID_DOMAIN, now,
    )

    assert result.reloaded is True
    assert result.error is None
    assert result.bundle_hash == config_bundle_hash(EXAMPLE_CONFIG, EXAMPLE_OVERRIDES)


def test_reload_applies_a_new_synthetic_event_from_overrides(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = check_and_reload_config(
        conn, EXAMPLE_CONFIG, EXAMPLE_OVERRIDES, None, root_config, OverridesConfig(), UID_DOMAIN, now,
    )

    from motorcal.models import synthetic_event_uid
    row = get_published_event(conn, synthetic_event_uid("imsa-2026-rolex-24", UID_DOMAIN))
    assert row is not None
    assert row["summary"] == "Rolex 24 at Daytona"


def test_reload_leaves_previous_state_untouched_on_invalid_yaml(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    overrides = OverridesConfig()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    bad_config = tmp_path / "bad_config.yaml"
    bad_config.write_text("not: valid: yaml: [[[")

    result = check_and_reload_config(
        conn, bad_config, EXAMPLE_OVERRIDES, None, root_config, overrides, UID_DOMAIN, now,
    )

    assert result.reloaded is False
    assert result.error is not None
    assert result.root_config is root_config  # previous config still active


def test_reload_leaves_previous_state_untouched_on_schema_validation_failure(tmp_path):
    conn = _fresh_conn(tmp_path)
    root_config = load_config(EXAMPLE_CONFIG)
    overrides = OverridesConfig()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    bad_config = tmp_path / "bad_config.yaml"
    bad_config.write_text(
        EXAMPLE_CONFIG.read_text().replace("league_id: 4370", "league_id: not_a_number")
    )

    result = check_and_reload_config(
        conn, bad_config, EXAMPLE_OVERRIDES, None, root_config, overrides, UID_DOMAIN, now,
    )

    assert result.reloaded is False
    assert result.error is not None
    assert result.root_config is root_config
