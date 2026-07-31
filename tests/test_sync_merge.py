"""The 3-way merge: what happens to your edits when the provider refetches."""
from tests.conftest import (
    manual_session,
    provider_event,
    snapshot,
    source_event,
    source_session,
    source_snapshot,
)

from motorcal.config import SeriesConfig
from motorcal.models import SessionType
from motorcal.sync import (
    derive_event,
    derive_session,
    event_from_sources,
    event_name_from,
    merge_event,
    merge_session,
    sync_snapshot,
)


def _series(events=None):
    return SeriesConfig(league_id=4413, name="WEC", max_round=20, events=list(events or []))


def _sync(series, snap, *, season="2026", now="t1", is_current=True, previous_count=None):
    return sync_snapshot(
        series, snap, season=season, now=now,
        is_current_season=is_current, previous_count=previous_count,
    )


def _sessions(series):
    return {session.key: session for _, session in series.iter_sessions()}


# ----------------------------------------------------------------- derive


def test_event_name_is_the_session_name_that_prefixes_the_others():
    """The provider names the race after the weekend, and every other session after
    the race -- so the race's own name is the weekend's name."""
    assert event_name_from([
        "6 Hours of Imola Free Practice 3",
        "6 Hours of Imola Qualifying",
        "6 Hours of Imola",
    ]) == "6 Hours of Imola"


def test_event_name_of_a_two_race_weekend_drops_the_session_word_they_share():
    """Neither race name prefixes the other, so the weekend is what they share --
    without the "Race" that belongs to the sessions, not the weekend."""
    assert event_name_from([
        "Snap On Milwaukee 250 Race #1", "Snap On Milwaukee 250 Race #2",
    ]) == "Snap On Milwaukee 250"


def test_event_name_falls_back_to_the_shortest_when_nothing_is_shared():
    assert event_name_from([
        "Weekend Qualifying", "Milwaukee 250",
    ]) == "Milwaukee 250"


def test_derive_event_takes_the_most_complete_location_of_the_weekend():
    """The provider routinely drops the venue on some sessions of a weekend it gave
    in full on others; the event stores the best one once."""
    values = derive_event([
        source_snapshot(name="6 Hours of Imola Practice 3", venue=None),
        source_snapshot(name="6 Hours of Imola", venue="Imola", country="Italy"),
    ])

    assert values["name"] == "6 Hours of Imola"
    assert values["location"] == "Imola, Italy"
    assert values["round"] == 1


def test_derive_event_handles_a_partial_location():
    assert derive_event([source_snapshot(country=None)])["location"] == "Imola"
    assert derive_event([source_snapshot(venue=None)])["location"] == "Italy"
    assert derive_event([source_snapshot(venue=None, country=None)])["location"] is None


def test_derive_session_splits_the_label_off_the_provider_name():
    values = derive_session(
        source_snapshot(name="6 Hours of Imola Free Practice 3"), "6 Hours of Imola"
    )

    assert values["label"] == "Free Practice 3"
    assert values["type"] is SessionType.PRACTICE


def test_derive_session_labels_the_event_named_session_as_the_race():
    values = derive_session(source_snapshot(name="6 Hours of Imola"), "6 Hours of Imola")

    assert values["label"] == "Race"
    assert values["type"] is SessionType.RACE


def test_derive_session_keeps_the_whole_name_when_it_is_not_prefixed():
    values = derive_session(
        source_snapshot(name="Weekend Qualifying"), "6 Hours of Imola"
    )

    assert values["label"] == "Weekend Qualifying"
    assert values["type"] is SessionType.QUALIFYING


def test_derive_session_maps_the_time_onto_start_or_date():
    assert derive_session(source_snapshot(time="13:00:00"), "x")["start"] == (
        "2026-04-19T13:00:00+00:00"
    )
    assert derive_session(source_snapshot(time=None), "x")["date"] == "2026-04-19"
    # TheSportsDB uses 00:00:00 as "no time yet", not as actual midnight.
    assert derive_session(source_snapshot(time="00:00:00"), "x")["start"] is None


# ----------------------------------------------------------------- merge_session


def _merge(session, new_source, *, was="6 Hours of Imola", now="6 Hours of Imola"):
    return merge_session(session, new_source, was_event_name=was, now_event_name=now)


def test_provider_change_is_taken_when_you_have_not_touched_the_field():
    session = source_session("1", name="6 Hours of Imola Qualifying")

    taken = _merge(session, source_snapshot(name="6 Hours of Imola Qualifying 1"))

    assert taken == ["label"]
    assert session.label == "Qualifying 1"


def test_your_edit_wins_over_a_provider_change():
    session = source_session("1", name="6 Hours of Imola Qualifying")
    session.label = "Quali"

    taken = _merge(session, source_snapshot(name="6 Hours of Imola Qualifying 1"))

    assert taken == []
    assert session.label == "Quali"


def test_an_unchanged_provider_value_never_overwrites_your_edit():
    """The common case: you set a time the provider still hasn't announced."""
    session = source_session("1", time=None)  # provider has no time -> all-day
    session.start, session.date = "2026-04-19T13:00:00+00:00", None

    taken = _merge(session, source_snapshot(time=None))  # provider still has none

    assert taken == []
    assert session.start == "2026-04-19T13:00:00+00:00"
    assert session.date is None


def test_the_provider_announcing_a_time_promotes_an_untouched_all_day_session():
    session = source_session("1", time=None)
    assert session.date == "2026-04-19" and session.start is None

    taken = _merge(session, source_snapshot(time="13:00:00"))

    assert taken == ["start"]
    assert session.start == "2026-04-19T13:00:00+00:00"
    assert session.date is None


def test_the_provider_announcing_a_time_does_not_override_your_own_time():
    session = source_session("1", time=None)
    session.start, session.date = "2026-04-19T12:00:00+00:00", None  # you guessed 12:00

    taken = _merge(session, source_snapshot(time="13:00:00"))  # provider says 13:00

    assert taken == []
    assert session.start == "2026-04-19T12:00:00+00:00"  # yours stands until you clear it


def test_merge_updates_the_source_baseline_even_when_your_edit_wins():
    """Otherwise your edit would be re-compared against a stale baseline forever."""
    session = source_session("1", name="6 Hours of Imola Practice 1")
    session.label = "Mine"

    _merge(session, source_snapshot(name="6 Hours of Imola Practice 2"))

    assert session.source.name == "6 Hours of Imola Practice 2"
    # A later provider change is now measured from that, and still loses to your edit.
    assert _merge(session, source_snapshot(name="6 Hours of Imola Practice 3")) == []
    assert session.label == "Mine"


def test_renaming_the_weekend_does_not_relabel_its_sessions():
    """The label is measured against the event name of the same generation, so a
    weekend the provider renames leaves every session's label untouched."""
    session = source_session("1", name="6 Hours of Imola Qualifying")

    taken = _merge(
        session, source_snapshot(name="6 Hours of Emilia Qualifying"),
        was="6 Hours of Imola", now="6 Hours of Emilia",
    )

    assert taken == []
    assert session.label == "Qualifying"


def test_merge_never_touches_your_own_fields():
    session = source_session("1")
    session.duration, session.note, session.status = "6h", "my note", "TENTATIVE"

    _merge(session, source_snapshot(name="Anything Else"))

    assert (session.duration, session.note, session.status) == ("6h", "my note", "TENTATIVE")


def test_merge_ignores_manual_sessions():
    session = manual_session("mine")

    assert _merge(session, source_snapshot()) == []
    assert session.source is None


# ----------------------------------------------------------------- merge_event


def test_event_level_provider_change_is_taken_when_untouched():
    event = source_event("1", name="6 Hours of Imola")
    old = [session.source for session in event.sessions]

    taken = merge_event(event, old, [source_snapshot(name="6 Hours of Imola", venue="Monza")])

    assert taken == ["location"]
    assert event.location == "Monza, Italy"


def test_your_event_level_edit_wins_over_a_provider_change():
    event = source_event("1", name="6 Hours of Imola")
    event.name, event.location = "6 Hours of Imola (mine)", "Autodromo Imola"
    old = [session.source for session in event.sessions]

    taken = merge_event(event, old, [source_snapshot(name="6 Hours of Monza", venue="Monza")])

    assert taken == []
    assert (event.name, event.location) == ("6 Hours of Imola (mine)", "Autodromo Imola")


def test_merge_event_with_no_baseline_changes_nothing():
    event = source_event("1")

    assert merge_event(event, [], [source_snapshot(name="Anything")]) == []


# ----------------------------------------------------------------- sync_snapshot


def test_sync_groups_a_round_into_one_event_with_its_sessions():
    series = _series()

    result = _sync(series, snapshot([
        provider_event("1", name="6 Hours of Imola", date="2026-04-19"),
        provider_event("2", name="6 Hours of Imola Qualifying", date="2026-04-18"),
        provider_event("3", name="6 Hours of Imola Free Practice 3", date="2026-04-17"),
    ]))

    assert result.committed is True
    assert result.events_added == 3
    assert len(series.events) == 1
    event = series.events[0]
    assert event.name == "6 Hours of Imola"
    assert event.location == "Imola, Italy"
    assert event.round == 1
    assert [(s.key, s.label, s.type) for s in event.sessions] == [
        ("3", "Free Practice 3", SessionType.PRACTICE),
        ("2", "Qualifying", SessionType.QUALIFYING),
        ("1", "Race", SessionType.RACE),
    ]


def test_a_double_header_weekend_is_one_event_with_both_races():
    """Two championship rounds at one venue on consecutive days are one weekend, and
    every session of it -- including the qualifying they share -- belongs together."""
    series = _series()

    _sync(series, snapshot([
        provider_event("r1", name="Snap On Milwaukee 250 Race #1", round=15,
                       date="2026-08-29", venue="Milwaukee Mile", country="United States"),
        provider_event("r2", name="Snap On Milwaukee 250 Race #2", round=16,
                       date="2026-08-30", venue="Milwaukee Mile", country="United States"),
    ]))

    assert len(series.events) == 1
    event = series.events[0]
    assert event.name == "Snap On Milwaukee 250"
    assert event.round == 15
    assert [(s.label, s.type, s.round) for s in event.sessions] == [
        ("Race #1", SessionType.RACE, None),   # the weekend's own round
        ("Race #2", SessionType.RACE, 16),     # runs for the next one
    ]


def test_a_double_header_groups_even_when_one_round_omits_the_venue():
    """The provider drops the venue on some sessions of a weekend it gives in full
    on others -- requiring identical locations would split the very weekends this
    grouping exists to join."""
    series = _series()

    _sync(series, snapshot([
        provider_event("r1", name="Milwaukee 250 Race #1", round=15, date="2026-08-29",
                       venue="Milwaukee Mile", country="United States"),
        provider_event("r2", name="Milwaukee 250 Race #2", round=16, date="2026-08-30",
                       venue=None, country="United States"),
    ]))

    assert len(series.events) == 1
    assert series.events[0].location == "Milwaukee Mile, United States"


def test_rounds_with_no_location_at_all_never_group():
    """An unknown location is unknown, not equal to another unknown one."""
    series = _series()

    _sync(series, snapshot([
        provider_event("1", name="Somewhere", round=1, date="2026-08-29",
                       venue=None, country=None),
        provider_event("2", name="Elsewhere", round=2, date="2026-08-30",
                       venue=None, country=None),
    ]))

    assert len(series.events) == 2


def test_the_same_venue_a_week_later_is_a_separate_weekend():
    series = _series()

    _sync(series, snapshot([
        provider_event("1", name="Austrian Grand Prix", round=1, date="2026-07-05",
                       venue="Red Bull Ring", country="Austria"),
        provider_event("2", name="Styrian Grand Prix", round=2, date="2026-07-12",
                       venue="Red Bull Ring", country="Austria"),
    ]))

    assert [e.name for e in series.events] == ["Austrian Grand Prix", "Styrian Grand Prix"]


def _milwaukee(first: int, second: int):
    return [
        provider_event("r1", name="Milwaukee 250 Race #1", round=first, date="2026-08-29",
                       venue="Milwaukee Mile", country="United States"),
        provider_event("r2", name="Milwaukee 250 Race #2", round=second, date="2026-08-30",
                       venue="Milwaukee Mile", country="United States"),
    ]


def test_renumbering_a_double_header_upstream_moves_both_of_its_rounds():
    """The second race's `round` overrides the weekend's, so it has to follow the
    provider too -- left behind it would silently claim the first race's round."""
    series = _series()
    _sync(series, snapshot(_milwaukee(15, 16)))

    _sync(series, snapshot(_milwaukee(16, 17)))

    event = series.events[0]
    assert event.round == 16
    assert [s.round for s in event.sessions] == [None, 17]


def test_your_own_round_survives_the_weekend_being_renumbered():
    series = _series()
    _sync(series, snapshot(_milwaukee(15, 16)))
    series.events[0].sessions[1].round = 99

    _sync(series, snapshot(_milwaukee(16, 17)))

    assert series.events[0].sessions[1].round == 99


def test_a_weekend_stored_as_two_events_is_folded_into_one():
    """Files written before a round was recognised as a double-header converge on
    the next refresh instead of staying split -- hand-added sessions included."""
    series = _series()
    _sync(series, snapshot([
        provider_event("r1", name="Milwaukee 250 Race #1", round=15, date="2026-08-29",
                       venue="Milwaukee Mile", country="United States"),
    ]))
    series.events[0].sessions.append(manual_session("quali", label="Qualifying"))
    # The second race turns up only now, making it a double-header.
    _sync(series, snapshot([
        provider_event("r1", name="Milwaukee 250 Race #1", round=15, date="2026-08-29",
                       venue="Milwaukee Mile", country="United States"),
        provider_event("r2", name="Milwaukee 250 Race #2", round=16, date="2026-08-30",
                       venue="Milwaukee Mile", country="United States"),
    ]))

    assert len(series.events) == 1
    assert {s.key for s in series.events[0].sessions} == {"r1", "r2", "quali"}


def test_sync_keeps_separate_rounds_as_separate_events():
    series = _series()

    _sync(series, snapshot([
        provider_event("1", name="6 Hours of Imola", round=1),
        provider_event("2", name="6 Hours of Monza", round=2, date="2026-05-19"),
    ]))

    assert [e.name for e in series.events] == ["6 Hours of Imola", "6 Hours of Monza"]
    assert all(len(e.sessions) == 1 for e in series.events)


def test_a_new_session_joins_the_weekend_it_belongs_to():
    series = _series()
    _sync(series, snapshot([provider_event("1", name="6 Hours of Imola")]))

    _sync(series, snapshot([
        provider_event("1", name="6 Hours of Imola"),
        provider_event("2", name="6 Hours of Imola Qualifying"),
    ]))

    assert len(series.events) == 1
    assert [s.label for s in series.events[0].sessions] == ["Race", "Qualifying"]


def test_sync_leaves_manual_sessions_alone():
    series = _series([source_event("1")])
    series.events[0].sessions.append(manual_session("mine"))

    _sync(series, snapshot([provider_event("1")]))

    assert _sessions(series)["mine"].disappeared_at is None


def test_incomplete_snapshot_is_discarded_in_full():
    series = _series([source_event("1", name="6 Hours of Imola")])

    result = _sync(series, snapshot([provider_event("1", name="Changed")], complete=False,
                                    diagnostics=["round 2: boom"]))

    assert result.committed is False
    assert result.reason == "incomplete_snapshot"
    assert series.events[0].name == "6 Hours of Imola"


def test_disappearance_is_marked_not_deleted():
    series = _series()
    _sync(series, snapshot([provider_event("1"), provider_event("2")]), now="t1")

    result = _sync(series, snapshot([provider_event("1")]), now="t2")

    assert result.events_disappeared == 1
    sessions = _sessions(series)
    assert sessions["1"].disappeared_at is None
    assert sessions["2"].disappeared_at == "t2"


def test_reappearance_clears_the_disappearance_mark():
    series = _series()
    _sync(series, snapshot([provider_event("1"), provider_event("2")]), now="t1")
    _sync(series, snapshot([provider_event("1")]), now="t2")

    _sync(series, snapshot([provider_event("1"), provider_event("2")]), now="t3")

    assert all(s.disappeared_at is None for s in _sessions(series).values())


def test_disappearance_is_scoped_to_the_season_being_synced():
    series = _series()
    _sync(series, snapshot([provider_event("1", season="2026")]), season="2026", now="t1")
    _sync(series, snapshot([provider_event("9", season="2027")]), season="2027",
          now="t1", is_current=False)

    _sync(series, snapshot([provider_event("2", season="2026")]), season="2026", now="t2")

    sessions = _sessions(series)
    assert sessions["1"].disappeared_at == "t2"
    assert sessions["9"].disappeared_at is None  # different season, untouched


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
    assert series.events[0].sessions[0].disappeared_at is None  # untouched


def test_sync_sorts_events_and_their_sessions_by_when_they_happen():
    series = _series()

    _sync(series, snapshot([
        provider_event("late", round=2, date="2026-09-01"),
        provider_event("early-race", round=1, date="2026-03-02"),
        provider_event("early-quali", round=1, date="2026-03-01"),
    ]))

    assert [s.key for _, s in series.iter_sessions()] == ["early-quali", "early-race", "late"]


def test_resyncing_the_same_snapshot_changes_nothing():
    """The invariant that keeps a refresh from rewriting files it has nothing new for
    -- and that a hand edit survives every cycle, not just the first."""
    series = _series()
    events = [
        provider_event("1", name="6 Hours of Imola", date="2026-04-19"),
        provider_event("2", name="6 Hours of Imola Qualifying", date="2026-04-18"),
    ]
    _sync(series, snapshot(events))
    series.events[0].name = "6 Hours of Imola (mine)"
    before = series.model_dump()

    result = _sync(series, snapshot(events), now="t2")

    assert (result.events_added, result.events_updated, result.events_disappeared) == (0, 0, 0)
    assert series.model_dump() == before


def test_event_from_sources_carries_the_snapshots():
    event = event_from_sources({"1": source_snapshot()})

    assert event.sessions[0].id_event == "1"
    assert event.sessions[0].uid is None
    assert event.sessions[0].source is not None
    # already in sync: a refetch of the same snapshot changes nothing
    assert merge_event(event, [source_snapshot()], [source_snapshot()]) == []
