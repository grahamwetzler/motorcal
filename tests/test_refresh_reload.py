from datetime import datetime, timezone

from tests.conftest import (
    make_config,
    make_series,
    make_state,
    source_event,
    write_config_dir,
)

from motorcal.refresh import check_and_reload_config, config_bundle_hash

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _dir(tmp_path, **kwargs):
    config = make_config(
        series={"wec": make_series(events=[source_event("1", time="13:00:00")])}, **kwargs
    )
    return write_config_dir(tmp_path, config), config


def test_bundle_hash_changes_when_a_series_file_changes(tmp_path):
    config_dir, _ = _dir(tmp_path)
    before = config_bundle_hash(config_dir)

    (config_dir / "wec.yaml").write_text((config_dir / "wec.yaml").read_text() + "\n")

    assert config_bundle_hash(config_dir) != before


def test_bundle_hash_is_stable_for_unchanged_content(tmp_path):
    config_dir, _ = _dir(tmp_path)

    assert config_bundle_hash(config_dir) == config_bundle_hash(config_dir)


def test_bundle_hash_notices_a_new_series_file(tmp_path):
    config_dir, config = _dir(tmp_path)
    before = config_bundle_hash(config_dir)

    from motorcal.config import save_series
    save_series(config_dir, "imsa", make_series(league_id=4488, name="IMSA"))

    assert config_bundle_hash(config_dir) != before


def test_reload_skips_when_nothing_changed(tmp_path):
    config_dir, config = _dir(tmp_path)
    previous_hash = config_bundle_hash(config_dir)

    result = check_and_reload_config(config_dir, make_state(), previous_hash, config, NOW)

    assert result.reloaded is False
    assert result.error is None
    assert result.config is config  # untouched, same object


def test_reload_succeeds_and_rebuilds(tmp_path):
    config_dir, config = _dir(tmp_path)

    result = check_and_reload_config(config_dir, make_state(), None, config, NOW)

    assert result.reloaded is True
    assert result.error is None
    assert result.bundle_hash == config_bundle_hash(config_dir)
    assert len(result.published["wec"]) == 1


def test_reload_picks_up_a_hand_edited_event(tmp_path):
    config_dir, config = _dir(tmp_path)
    text = (config_dir / "wec.yaml").read_text().replace(
        "summary: 6 Hours of Imola", "summary: 6 Hours of Imola (edited)"
    )
    (config_dir / "wec.yaml").write_text(text)

    result = check_and_reload_config(config_dir, make_state(), None, config, NOW)

    assert result.reloaded is True
    assert result.published["wec"][0].summary == "6 Hours of Imola (edited)"


def test_reload_picks_up_a_new_series_file(tmp_path):
    config_dir, config = _dir(tmp_path)
    from motorcal.config import save_series
    save_series(config_dir, "imsa", make_series(league_id=4488, name="IMSA"))

    result = check_and_reload_config(config_dir, make_state(), None, config, NOW)

    assert result.reloaded is True
    assert "imsa" in result.config.series


def test_reload_is_rejected_on_invalid_yaml(tmp_path):
    config_dir, config = _dir(tmp_path)
    (config_dir / "wec.yaml").write_text("not: valid: yaml: [[[")

    result = check_and_reload_config(config_dir, make_state(), None, config, NOW)

    assert result.reloaded is False
    assert result.error is not None
    assert result.config is config  # previous bundle stays active
    assert result.published is None


def test_reload_is_rejected_on_schema_validation_failure(tmp_path):
    config_dir, config = _dir(tmp_path)
    (config_dir / "wec.yaml").write_text("league_id: not_a_number\nname: WEC\nmax_round: 20\n")

    result = check_and_reload_config(config_dir, make_state(), None, config, NOW)

    assert result.reloaded is False
    assert result.error is not None
    assert result.config is config


def test_reload_is_rejected_when_an_event_is_invalid(tmp_path):
    config_dir, config = _dir(tmp_path)
    # An event with neither start nor date violates the "exactly one" rule.
    (config_dir / "wec.yaml").write_text(
        "league_id: 4413\nname: WEC\nmax_round: 20\nevents:\n- uid: broken\n  summary: Nope\n"
    )

    result = check_and_reload_config(config_dir, make_state(), None, config, NOW)

    assert result.reloaded is False
    assert result.error is not None


def test_reload_rejects_a_runtime_uid_domain_change(tmp_path):
    config_dir, config = _dir(tmp_path)
    text = (config_dir / "motorcal.yaml").read_text().replace(
        config.globals.uid_domain, "other.example.com"
    )
    (config_dir / "motorcal.yaml").write_text(text)

    result = check_and_reload_config(config_dir, make_state(), None, config, NOW)

    assert result.reloaded is False
    assert "uid_domain" in result.error
    assert result.config.globals.uid_domain != "other.example.com"
