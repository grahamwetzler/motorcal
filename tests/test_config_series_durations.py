# tests/test_config_series_durations.py
from pathlib import Path

from motorcal.config import load_config

EXAMPLE_CONFIG = Path("config/config.example.yaml")


def test_series_config_durations_defaults_to_none():
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.series["f1"].durations is None


def test_series_config_accepts_per_series_duration_overrides(tmp_path):
    overridden = tmp_path / "config.yaml"
    overridden.write_text(
        EXAMPLE_CONFIG.read_text().replace(
            '  f1:\n    league_id: 4370\n    name: "Formula 1"\n    max_round: 30\n',
            '  f1:\n    league_id: 4370\n    name: "Formula 1"\n    max_round: 30\n'
            '    durations:\n      race: "2h"\n',
        )
    )
    cfg = load_config(overridden)
    assert cfg.series["f1"].durations is not None
    assert cfg.series["f1"].durations.race == "2h"
    assert cfg.series["f1"].durations.practice is None
