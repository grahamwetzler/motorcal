from datetime import datetime, timezone

from motorcal.refresh import seasons_to_fetch


def test_only_current_season_before_the_cutoff():
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    assert seasons_to_fetch(now, "10-01") == [("2026", True)]


def test_next_season_included_on_the_cutoff_date():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert seasons_to_fetch(now, "10-01") == [("2026", True), ("2027", False)]


def test_next_season_included_after_the_cutoff():
    now = datetime(2026, 12, 31, tzinfo=timezone.utc)
    assert seasons_to_fetch(now, "10-01") == [("2026", True), ("2027", False)]


def test_only_current_season_early_in_the_year():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert seasons_to_fetch(now, "10-01") == [("2026", True)]


def test_current_season_is_always_first_in_the_list():
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    result = seasons_to_fetch(now, "10-01")
    assert result[0] == ("2026", True)
