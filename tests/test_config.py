import pytest
import yaml
from tests.conftest import UID_DOMAIN, make_config, make_event, make_series, write_config_dir

from motorcal.config import (
    ConfigError,
    EventConfig,
    SessionConfig,
    load_config,
    parse_alarm_offset,
    parse_duration,
    series_path,
)

EXAMPLE_DIR = "data.example"


def _dir(tmp_path):
    return write_config_dir(
        tmp_path,
        make_config(series={"wec": make_series(events=[make_event("wec-2026-imola-race")])}),
    )


# --------------------------------------------------------------- parsing helpers


def test_parse_duration_hours_and_minutes():
    assert parse_duration("6h") == 21600
    assert parse_duration("45m") == 2700


def test_parse_duration_rejects_garbage():
    with pytest.raises(ConfigError):
        parse_duration("banana")


def test_parse_alarm_offset_is_negative_seconds():
    assert parse_alarm_offset("-1d") == -86400
    assert parse_alarm_offset("-30m") == -1800


def test_parse_alarm_offset_rejects_a_positive_offset():
    with pytest.raises(ConfigError):
        parse_alarm_offset("30m")


# --------------------------------------------------------------- directory loading


def test_the_shipped_example_directory_is_valid():
    config = load_config(EXAMPLE_DIR, uid_domain=UID_DOMAIN)

    assert config.globals.uid_domain == "racing.example.com"
    assert "f1" in config.series
    assert len(config.series["f1"].events) == 2
    assert config.series["f1"].schedule_url is not None


def test_the_series_key_comes_from_the_filename(tmp_path):
    config_dir = _dir(tmp_path)
    (config_dir / "wec.yaml").rename(config_dir / "endurance.yaml")

    config = load_config(config_dir, uid_domain=UID_DOMAIN)

    assert set(config.series) == {"endurance"}


def test_a_missing_directory_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope", uid_domain=UID_DOMAIN)


def test_a_directory_with_no_series_files_is_an_error(tmp_path):
    config_dir = _dir(tmp_path)
    (config_dir / "wec.yaml").unlink()

    with pytest.raises(ConfigError, match="No series files"):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_a_missing_globals_file_is_an_error(tmp_path):
    config_dir = _dir(tmp_path)
    (config_dir / "defaults.yaml").unlink()

    with pytest.raises(ConfigError):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_an_unknown_top_level_key_is_rejected(tmp_path):
    config_dir = _dir(tmp_path)
    (config_dir / "defaults.yaml").write_text(
        (config_dir / "defaults.yaml").read_text() + "surprise: true\n"
    )

    with pytest.raises(ConfigError):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_a_bad_duration_default_is_rejected(tmp_path):
    config_dir = _dir(tmp_path)
    raw = yaml.safe_load((config_dir / "defaults.yaml").read_text())
    raw["defaults"]["durations"] = {"race": "ages"}
    (config_dir / "defaults.yaml").write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_a_duration_default_for_an_unknown_session_type_is_rejected(tmp_path):
    config_dir = _dir(tmp_path)
    raw = yaml.safe_load((config_dir / "defaults.yaml").read_text())
    raw["defaults"]["durations"] = {"parade_lap": "10m"}
    (config_dir / "defaults.yaml").write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError, match="Unknown session type"):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_a_bad_alarm_offset_is_rejected(tmp_path):
    config_dir = _dir(tmp_path)
    raw = yaml.safe_load((config_dir / "defaults.yaml").read_text())
    raw["defaults"]["alerts"] = {"race": ["1 day"]}
    (config_dir / "defaults.yaml").write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_a_series_filename_that_is_not_a_valid_key_is_rejected(tmp_path):
    config_dir = _dir(tmp_path)
    (config_dir / "wec.yaml").rename(config_dir / "Formula One.yaml")

    with pytest.raises(ConfigError, match="series key"):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_a_series_named_after_the_combined_feed_is_rejected(tmp_path):
    """/events.ics is the combined feed, so that series could never be served."""
    config_dir = _dir(tmp_path)
    (config_dir / "wec.yaml").rename(config_dir / "events.yaml")

    with pytest.raises(ConfigError, match="reserved"):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_dotfiles_are_ignored(tmp_path):
    """A crashed atomic write leaves a .tmp file behind; it must not become a series."""
    config_dir = _dir(tmp_path)
    (config_dir / ".wec-abc.tmp.yaml").write_text("garbage: true\n")

    assert set(load_config(config_dir, uid_domain=UID_DOMAIN).series) == {"wec"}


def test_uid_domain_in_motorcal_yaml_is_rejected(tmp_path):
    """uid_domain comes from the UID_DOMAIN env var, never the file."""
    config_dir = _dir(tmp_path)
    (config_dir / "defaults.yaml").write_text(
        (config_dir / "defaults.yaml").read_text() + "uid_domain: sneaky.example.com\n"
    )

    with pytest.raises(ConfigError, match="uid_domain"):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_state_yaml_sharing_the_directory_is_ignored(tmp_path):
    """state.yaml (and dated backups) live alongside series files; not series data."""
    config_dir = _dir(tmp_path)
    (config_dir / "state.yaml").write_text("uid_domain: x\n")
    (config_dir / "state-2026-07-31.yaml").write_text("uid_domain: x\n")

    assert set(load_config(config_dir, uid_domain=UID_DOMAIN).series) == {"wec"}


# --------------------------------------------------------------- event schema


def test_a_session_needs_a_uid_and_a_type():
    with pytest.raises(ValueError):
        SessionConfig(type="race", date="2026-01-01")  # no uid
    with pytest.raises(ValueError):
        SessionConfig(uid="mine", date="2026-01-01")  # no type


def test_a_session_uid_cannot_be_blank():
    """A blank one type-checks, and would publish every such session as one event."""
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="uid must not be empty"):
            SessionConfig(uid=blank, type="race", date="2026-01-01")


def test_a_session_rejects_an_unknown_type():
    with pytest.raises(ValueError):
        SessionConfig(uid="mine", type="parade_lap", date="2026-01-01")


def test_a_session_needs_exactly_one_of_start_or_date():
    with pytest.raises(ValueError, match="exactly one of start or date"):
        SessionConfig(uid="mine", type="race")
    with pytest.raises(ValueError, match="exactly one of start or date"):
        SessionConfig(uid="mine", type="race", start="2026-01-01T00:00:00Z", date="2026-01-01")


def test_a_session_with_a_confirmed_start_cannot_be_tbc():
    """tbc means "no time announced yet"; a start time is exactly that announcement."""
    with pytest.raises(ValueError, match="cannot also be tbc"):
        SessionConfig(uid="mine", type="race", start="2026-01-01T13:00:00Z", tbc=True)


def test_an_event_needs_at_least_one_session():
    with pytest.raises(ValueError, match="has no sessions"):
        EventConfig(name="Empty Weekend")


def test_a_session_rejects_a_bad_duration():
    with pytest.raises(ValueError):
        SessionConfig(uid="mine", type="race", date="2026-01-01", duration="ages")


def test_a_session_rejects_a_bad_status():
    with pytest.raises(ValueError):
        SessionConfig(uid="mine", type="race", date="2026-01-01", status="MAYBE")


def test_duplicate_session_keys_within_a_series_are_rejected(tmp_path):
    config_dir = _dir(tmp_path)
    raw = yaml.safe_load((config_dir / "wec.yaml").read_text())
    raw["events"].append(dict(raw["events"][0]))

    (config_dir / "wec.yaml").write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(config_dir, uid_domain=UID_DOMAIN)


def test_duplicate_session_uids_across_two_series_are_rejected(tmp_path):
    """A UID keys the version ledger and identifies the event in /events.ics, so it has
    to be unique across the whole directory, not just within one series file."""
    config_dir = _dir(tmp_path)
    series_path(config_dir, "f1").write_text(
        (config_dir / "wec.yaml").read_text().replace("name: WEC", "name: F1")
    )

    with pytest.raises(ConfigError, match="Duplicate session uid"):
        load_config(config_dir, uid_domain=UID_DOMAIN)
