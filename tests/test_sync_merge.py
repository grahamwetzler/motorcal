"""The 3-way merge: what happens to your edits when the provider refetches."""
from tests.conftest import manual_event, provider_event, snapshot, source_event, source_snapshot

from motorcal.config import SeriesConfig
from motorcal.sync import derive, event_from_source, merge_event, sync_snapshot


def _series(events=None):
    return SeriesConfig(league_id=4413, name="WEC", max_round=20, events=list(events or []))


def _sync(series, snap, *, season="2026", now="t1", is_current=True, previous_count=None):
    return sync_snapshot(
        series, snap, season=season, now=now,
        is_current_season=is_current, previous_count=previous_count,
    )


# ----------------------------------------------------------------- derive


def test_derive_maps_provider_fields_onto_published_ones():
    values = derive(source_snapshot(name="6 Hours of Imola", time="13:00:00"))

    assert values["summary"] == "6 Hours of Imola"
    assert values["start"] == "2026-04-19T13:00:00+00:00"
    assert values["date"] is None
    assert values["location"] == "Imola, Italy"


def test_derive_treats_a_missing_time_as_all_day():
    values = derive(source_snapshot(time=None))

    assert values["start"] is None
    assert values["date"] == "2026-04-19"


def test_derive_treats_midnight_as_an_unannounced_time():
    # TheSportsDB uses 00:00:00 as "no time yet", not as actual midnight.
    assert derive(source_snapshot(time="00:00:00"))["start"] is None


def test_derive_handles_a_partial_location():
    assert derive(source_snapshot(country=None))["location"] == "Imola"
    assert derive(source_snapshot(venue=None))["location"] == "Italy"
    assert derive(source_snapshot(venue=None, country=None))["location"] is None


# ----------------------------------------------------------------- merge_event


def test_provider_change_is_taken_when_you_have_not_touched_the_field():
    event = source_event("1", name="6 Hours of Imola")

    taken = merge_event(event, source_snapshot(name="6 Hours of Imola (Revised)"))

    assert taken == ["summary"]
    assert event.summary == "6 Hours of Imola (Revised)"


def test_your_edit_wins_over_a_provider_change():
    event = source_event("1", name="6 Hours of Imola")
    event.summary = "6 Hours of Imola (my title)"

    taken = merge_event(event, source_snapshot(name="6 Hours of Imola (Revised)"))

    assert taken == []
    assert event.summary == "6 Hours of Imola (my title)"


def test_an_unchanged_provider_value_never_overwrites_your_edit():
    """The common case: you set a time the provider still hasn't announced."""
    event = source_event("1", time=None)  # provider has no time -> all-day
    event.start, event.date = "2026-04-19T13:00:00+00:00", None

    taken = merge_event(event, source_snapshot(time=None))  # provider still has none

    assert taken == []
    assert event.start == "2026-04-19T13:00:00+00:00"
    assert event.date is None


def test_the_provider_announcing_a_time_promotes_an_untouched_all_day_event():
    event = source_event("1", time=None)
    assert event.date == "2026-04-19" and event.start is None

    taken = merge_event(event, source_snapshot(time="13:00:00"))

    assert taken == ["start"]
    assert event.start == "2026-04-19T13:00:00+00:00"
    assert event.date is None


def test_the_provider_announcing_a_time_does_not_override_your_own_time():
    event = source_event("1", time=None)
    event.start, event.date = "2026-04-19T12:00:00+00:00", None  # you guessed 12:00

    taken = merge_event(event, source_snapshot(time="13:00:00"))  # provider says 13:00

    assert taken == []
    assert event.start == "2026-04-19T12:00:00+00:00"  # yours stands until you clear it


def test_merge_updates_the_source_baseline_even_when_your_edit_wins():
    """Otherwise your edit would be re-compared against a stale baseline forever."""
    event = source_event("1", name="Original")
    event.summary = "Mine"

    merge_event(event, source_snapshot(name="Provider v2"))

    assert event.source.name == "Provider v2"
    # A later provider change is now measured from v2, and still loses to your edit.
    assert merge_event(event, source_snapshot(name="Provider v3")) == []
    assert event.summary == "Mine"


def test_fields_merge_independently():
    event = source_event("1", name="Original", venue="Imola", country="Italy")
    event.summary = "Mine"  # only the summary is yours

    merge_event(event, source_snapshot(name="Provider v2", venue="Monza", country="Italy"))

    assert event.summary == "Mine"
    assert event.location == "Monza, Italy"


def test_merge_never_touches_your_own_fields():
    event = source_event("1")
    event.duration, event.note, event.status = "6h", "my note", "TENTATIVE"

    merge_event(event, source_snapshot(name="Anything Else"))

    assert (event.duration, event.note, event.status) == ("6h", "my note", "TENTATIVE")


def test_merge_ignores_manual_events():
    event = manual_event("mine")

    assert merge_event(event, source_snapshot()) == []
    assert event.source is None


# ----------------------------------------------------------------- sync_snapshot


def test_sync_adds_new_events():
    series = _series()

    result = _sync(series, snapshot([provider_event("1"), provider_event("2")]))

    assert result.committed is True
    assert result.events_added == 2
    assert {e.id_event for e in series.events} == {"1", "2"}


def test_sync_leaves_manual_events_alone():
    series = _series([manual_event("mine")])

    _sync(series, snapshot([provider_event("1")]))

    assert {e.key for e in series.events} == {"mine", "1"}
    assert next(e for e in series.events if e.key == "mine").disappeared_at is None


def test_incomplete_snapshot_is_discarded_in_full():
    series = _series([source_event("1", name="Original")])

    result = _sync(series, snapshot([provider_event("1", name="Changed")], complete=False,
                                    diagnostics=["round 2: boom"]))

    assert result.committed is False
    assert result.reason == "incomplete_snapshot"
    assert series.events[0].summary == "Original"


def test_disappearance_is_marked_not_deleted():
    series = _series()
    _sync(series, snapshot([provider_event("1"), provider_event("2")]), now="t1")

    result = _sync(series, snapshot([provider_event("1")]), now="t2")

    assert result.events_disappeared == 1
    by_id = {e.id_event: e for e in series.events}
    assert by_id["1"].disappeared_at is None
    assert by_id["2"].disappeared_at == "t2"


def test_reappearance_clears_the_disappearance_mark():
    series = _series()
    _sync(series, snapshot([provider_event("1"), provider_event("2")]), now="t1")
    _sync(series, snapshot([provider_event("1")]), now="t2")

    _sync(series, snapshot([provider_event("1"), provider_event("2")]), now="t3")

    assert all(e.disappeared_at is None for e in series.events)


def test_disappearance_is_scoped_to_the_season_being_synced():
    series = _series()
    _sync(series, snapshot([provider_event("1", season="2026")]), season="2026", now="t1")
    _sync(series, snapshot([provider_event("9", season="2027")]), season="2027",
          now="t1", is_current=False)

    _sync(series, snapshot([provider_event("2", season="2026")]), season="2026", now="t2")

    by_id = {e.id_event: e for e in series.events}
    assert by_id["1"].disappeared_at == "t2"
    assert by_id["9"].disappeared_at is None  # different season, untouched


def test_empty_snapshot_for_current_season_is_always_suspicious():
    series = _series()

    result = _sync(series, snapshot([]), is_current=True)

    assert result.committed is False
    assert result.reason == "suspicious_empty_current_season"


def test_empty_snapshot_for_a_brand_new_future_season_is_accepted():
    result = _sync(_series(), snapshot([]), season="2027", is_current=False, previous_count=None)

    assert result.committed is True


def test_empty_snapshot_for_a_previously_populated_future_season_is_suspicious():
    series = _series([source_event("1", season="2027")])

    result = _sync(series, snapshot([]), season="2027", is_current=False, previous_count=5)

    assert result.committed is False
    assert result.reason == "suspicious_empty_future_season"
    assert series.events[0].disappeared_at is None  # untouched


def test_sync_sorts_events_by_when_they_happen():
    series = _series()

    _sync(series, snapshot([
        provider_event("late", date="2026-09-01"),
        provider_event("early", date="2026-03-01"),
    ]))

    assert [e.id_event for e in series.events] == ["early", "late"]


def test_event_from_source_carries_the_snapshot():
    event = event_from_source(source_snapshot(), "1")

    assert event.id_event == "1"
    assert event.uid is None
    assert event.source is not None
    assert merge_event(event, source_snapshot()) == []  # already in sync
