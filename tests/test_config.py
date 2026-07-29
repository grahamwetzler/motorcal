from pathlib import Path

import pytest

from motorcal.config import ConfigError, load_config, parse_duration

EXAMPLE_CONFIG = Path("config/config.example.yaml")


def test_parse_duration_hours():
    assert parse_duration("1h") == 3600


def test_parse_duration_minutes():
    assert parse_duration("45m") == 45 * 60


def test_parse_duration_rejects_bad_format():
    with pytest.raises(ConfigError):
        parse_duration("banana")


def test_load_example_config_succeeds():
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.server.base_url == "https://racing.example.com"
    assert cfg.server.uid_domain == "racing.example.com"
    assert cfg.source.rate_limit_per_min == 28
    assert cfg.retention.historical_days == 180
    assert cfg.retention.cancelled_after_event_days == 90
    assert cfg.series["f1"].league_id == 4370
    assert cfg.series["wec"].league_id == 4413
    assert cfg.series["indycar"].league_id == 4373
    assert cfg.series["indycar"].race_only is True
    assert cfg.series["imsa"].league_id == 4488
    assert cfg.series["imsa"].race_only is True
    assert "race" in cfg.defaults.include_sessions
    assert cfg.unknown_time.mode == "all_day"


def test_load_config_missing_file_raises_config_error():
    with pytest.raises(ConfigError):
        load_config(Path("config/does-not-exist.yaml"))


def test_load_config_rejects_unknown_session_in_include_sessions(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        EXAMPLE_CONFIG.read_text().replace(
            "include_sessions:\n    - practice",
            "include_sessions:\n    - not_a_real_session\n    - practice",
        )
    )
    with pytest.raises(ConfigError):
        load_config(bad)


def test_load_config_rejects_missing_required_field(tmp_path):
    bad = tmp_path / "bad.yaml"
    text = EXAMPLE_CONFIG.read_text().replace('uid_domain: "racing.example.com"\n', "")
    bad.write_text(text)
    with pytest.raises(ConfigError):
        load_config(bad)


def test_load_config_rejects_bad_alarm_offset(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        EXAMPLE_CONFIG.read_text().replace(
            'race: ["-1d", "-30m"]', 'race: ["-1d", "thirty minutes"]'
        )
    )
    with pytest.raises(ConfigError):
        load_config(bad)
