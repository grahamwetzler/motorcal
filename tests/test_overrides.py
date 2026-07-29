from pathlib import Path

import pytest

from motorcal.config import ConfigError, load_overrides

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_OVERRIDES = REPO_ROOT / "config" / "overrides.example.yaml"


def test_load_example_overrides_succeeds():
    overrides = load_overrides(EXAMPLE_OVERRIDES)
    assert len(overrides.patches) == 1
    patch = overrides.patches[0]
    assert patch.id_event == "2421035"
    assert patch.start == "2026-04-19T13:00:00Z"
    assert patch.duration == "6h"
    assert patch.note == "official WEC timetable"

    assert len(overrides.events) == 1
    ev = overrides.events[0]
    assert ev.uid == "imsa-2026-rolex-24"
    assert ev.series == "imsa"
    assert ev.summary == "Rolex 24 at Daytona"
    assert ev.start == "2026-01-25T18:40:00Z"
    assert ev.duration == "24h"


def test_patch_requires_id_event_or_match(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches:
  - start: "2026-04-19T13:00:00Z"
events: []
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_patch_rejects_both_id_event_and_match(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches:
  - id_event: "123"
    match:
      series: "wec"
      date: "2026-04-19"
      contains: "Imola"
    start: "2026-04-19T13:00:00Z"
events: []
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_synthetic_event_requires_uid(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches: []
events:
  - series: imsa
    summary: "Rolex 24 at Daytona"
    start: "2026-01-25T18:40:00Z"
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_synthetic_event_requires_start_or_date(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches: []
events:
  - uid: "imsa-2026-rolex-24"
    series: imsa
    summary: "Rolex 24 at Daytona"
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_synthetic_event_rejects_both_start_and_date(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches: []
events:
  - uid: "imsa-2026-rolex-24"
    series: imsa
    summary: "Rolex 24 at Daytona"
    start: "2026-01-25T18:40:00Z"
    date: "2026-01-25"
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_load_overrides_missing_file_raises_config_error():
    with pytest.raises(ConfigError):
        load_overrides(REPO_ROOT / "config" / "does-not-exist-overrides.yaml")


def test_patch_rejects_unknown_key(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches:
  - id_event: "123"
    not_a_real_key: "oops"
events: []
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_patch_rejects_invalid_status(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches:
  - id_event: "123"
    status: cancelled
events: []
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)


def test_synthetic_event_rejects_invalid_status(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
patches: []
events:
  - uid: "imsa-2026-rolex-24"
    series: imsa
    summary: "Rolex 24 at Daytona"
    start: "2026-01-25T18:40:00Z"
    status: not_a_real_status
"""
    )
    with pytest.raises(ConfigError):
        load_overrides(bad)
