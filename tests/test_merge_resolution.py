from tests.conftest import make_globals, make_series

from motorcal.config import DurationDefaults
from motorcal.merge import compute_fingerprint, next_sequence, resolve_alarms, resolve_duration
from motorcal.models import SessionType


def test_compute_fingerprint_is_deterministic_for_identical_inputs():
    fp1 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600,
        alarms=["-1d", "-30m"],
    )
    fp2 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600,
        alarms=["-1d", "-30m"],
    )
    assert fp1 == fp2


def test_compute_fingerprint_alarm_order_does_not_matter():
    fp1 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600,
        alarms=["-1d", "-30m"],
    )
    fp2 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600,
        alarms=["-30m", "-1d"],
    )
    assert fp1 == fp2


def test_compute_fingerprint_changes_when_status_changes():
    fp1 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600, alarms=[],
    )
    fp2 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CANCELLED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600, alarms=[],
    )
    assert fp1 != fp2


def test_compute_fingerprint_changes_when_alarm_set_changes():
    fp1 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600, alarms=["-1d"],
    )
    fp2 = compute_fingerprint(
        summary="Race", description="desc", location="Imola", status="CONFIRMED",
        start="2026-04-19T13:00:00+00:00", all_day_date=None, duration_seconds=21600, alarms=["-30m"],
    )
    assert fp1 != fp2


def test_next_sequence_for_a_brand_new_event():
    assert next_sequence(None, now_unix_minute=12345678) == 12345678


def test_next_sequence_increments_when_greater_than_current_minute():
    assert next_sequence(previous_sequence=100, now_unix_minute=50) == 101


def test_next_sequence_jumps_to_current_minute_when_ahead_of_previous():
    assert next_sequence(previous_sequence=100, now_unix_minute=999999) == 999999


def test_next_sequence_restored_backup_never_goes_backwards():
    # A restored-from-backup previous_sequence that's already far in the future of
    # "now" must still advance forward, never reset down to now_unix_minute.
    assert next_sequence(previous_sequence=999999, now_unix_minute=100) == 1000000


def _globals(global_race_duration="1h", alerts=None):
    return make_globals(
        durations=DurationDefaults(race=global_race_duration),
        alerts=alerts if alerts is not None else {"race": ["-1d"]},
    )


def test_resolve_duration_prefers_own_duration_over_everything():
    globals_ = _globals()
    series = make_series(durations=DurationDefaults(race="3h"))
    result = resolve_duration(
        SessionType.RACE, own_duration="6h", series_config=series, globals_=globals_
    )
    assert result == 21600  # own duration (6h), not the 3h series override or 1h global default


def test_resolve_duration_falls_back_to_series_override():
    globals_ = _globals(global_race_duration="1h")
    series = make_series(durations=DurationDefaults(race="3h"))
    result = resolve_duration(
        SessionType.RACE, own_duration=None, series_config=series, globals_=globals_
    )
    assert result == 3 * 3600


def test_resolve_duration_falls_back_to_global_default():
    globals_ = _globals(global_race_duration="1h")
    series = make_series()  # no series-level override
    result = resolve_duration(
        SessionType.RACE, own_duration=None, series_config=series, globals_=globals_
    )
    assert result == 3600


def test_resolve_duration_returns_none_when_nothing_configured():
    globals_ = _globals(global_race_duration=None)
    series = make_series()
    result = resolve_duration(
        SessionType.HYPERPOLE, own_duration=None, series_config=series, globals_=globals_
    )
    assert result is None


def test_resolve_alarms_returns_empty_for_unconfirmed_time():
    globals_ = _globals(alerts={"race": ["-1d"]})
    result = resolve_alarms(
        SessionType.RACE, own_alarms=None, time_confirmed=False, globals_=globals_
    )
    assert result == []


def test_resolve_alarms_returns_empty_for_unknown_session_type():
    globals_ = _globals(alerts={"race": ["-1d"]})
    result = resolve_alarms(
        SessionType.UNKNOWN, own_alarms=None, time_confirmed=True, globals_=globals_
    )
    assert result == []


def test_resolve_alarms_returns_empty_for_testing_session_type():
    globals_ = _globals(alerts={"race": ["-1d"]})
    result = resolve_alarms(
        SessionType.TESTING, own_alarms=None, time_confirmed=True, globals_=globals_
    )
    assert result == []


def test_resolve_alarms_uses_synthetic_own_alarms_when_synthetic():
    globals_ = _globals(alerts={"race": ["-1d"]})
    result = resolve_alarms(
        SessionType.RACE, own_alarms=["-2d", "-1h"],
        time_confirmed=True, globals_=globals_,
    )
    assert result == ["-2d", "-1h"]


def test_resolve_alarms_uses_global_defaults_for_source_backed_event():
    globals_ = _globals(alerts={"race": ["-1d", "-30m"]})
    result = resolve_alarms(
        SessionType.RACE, own_alarms=None, time_confirmed=True, globals_=globals_
    )
    assert result == ["-1d", "-30m"]


def test_resolve_alarms_empty_list_is_a_valid_deliberate_configuration():
    globals_ = _globals(alerts={"race": ["-1d"], "practice": []})
    result = resolve_alarms(
        SessionType.PRACTICE, own_alarms=None, time_confirmed=True, globals_=globals_
    )
    assert result == []
