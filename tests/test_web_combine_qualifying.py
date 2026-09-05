"""`?combine_qualifying=true` -- merging one weekend's qualifying-family
sessions (qualifying, hyperpole, sprint qualifying) into a single event.
"""

from datetime import UTC, datetime

from motorcal.models import EventStatus, PublishedEvent, SessionType
from motorcal.web import _combine_qualifying

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    uid,
    session_type,
    *,
    event_name="Lone Star Le Mans",
    event_key="wec-2026-lsl-fp1",  # the weekend's first session's uid, as merge.py assigns it
    series="wec",
    start=None,
    duration_seconds=1200,
    status=EventStatus.CONFIRMED,
    alarms=None,
    time_confirmed=True,
    sequence=1,
    dtstamp=NOW,
    last_modified=NOW,
):
    return PublishedEvent(
        uid=uid,
        series=series,
        session_type=session_type,
        event_name=event_name,
        event_key=event_key,
        summary=f"{event_name} {uid}",
        start=start if time_confirmed else None,
        all_day_date=None if time_confirmed else "2026-09-05",
        time_confirmed=time_confirmed,
        duration_seconds=duration_seconds if time_confirmed else None,
        location="Circuit of the Americas, United States",
        description="Round: 5",
        status=status,
        sequence=sequence,
        dtstamp=dtstamp,
        last_modified=last_modified,
        fingerprint="fp",
        alarms=list(alarms or []),
    )


def _lone_star_le_mans():
    """Four qualifying-family sessions plus a practice and a race -- the real shape of `data/wec.yaml`."""
    return [
        _event(
            "wec-2026-lsl-fp1",
            SessionType.PRACTICE,
            start=datetime(2026, 9, 4, 16, 30, tzinfo=UTC),
            duration_seconds=5400,
        ),
        _event(
            "wec-2026-lsl-qualifying-lmgt3",
            SessionType.QUALIFYING,
            start=datetime(2026, 9, 5, 20, 0, tzinfo=UTC),
            alarms=["-15m"],
        ),
        _event(
            "wec-2026-lsl-hyperpole-lmgt3",
            SessionType.HYPERPOLE,
            start=datetime(2026, 9, 5, 20, 20, tzinfo=UTC),
            alarms=["-15m"],
        ),
        _event(
            "wec-2026-lsl-qualifying-hypercar",
            SessionType.QUALIFYING,
            start=datetime(2026, 9, 5, 20, 40, tzinfo=UTC),
            alarms=["-15m"],
        ),
        _event(
            "wec-2026-lsl-hyperpole-hypercar",
            SessionType.HYPERPOLE,
            start=datetime(2026, 9, 5, 21, 0, tzinfo=UTC),
            alarms=["-15m"],
        ),
        _event(
            "wec-2026-lsl-race",
            SessionType.RACE,
            start=datetime(2026, 9, 6, 18, 0, tzinfo=UTC),
            duration_seconds=6 * 3600,
        ),
    ]


def _combined(events):
    return next(
        e
        for e in events
        if e.session_type == SessionType.QUALIFYING and "combined" in e.uid
    )


def test_four_qualifying_family_sessions_collapse_into_one_event():
    events = _lone_star_le_mans()

    result = _combine_qualifying(events)

    others = {e.uid for e in result if "combined" not in e.uid}
    assert others == {"wec-2026-lsl-fp1", "wec-2026-lsl-race"}

    combined = _combined(result)
    assert combined.start == datetime(2026, 9, 5, 20, 0, tzinfo=UTC)
    assert combined.duration_seconds == 80 * 60  # 20:00 to 21:20
    assert combined.summary == "Lone Star Le Mans Qualifying"
    assert combined.session_type == SessionType.QUALIFYING
    assert combined.alarms == ["-15m"]
    assert combined.time_confirmed is True
    assert "Combines:" in combined.description


def test_a_lone_qualifying_session_is_left_alone():
    """Most non-WEC rounds have exactly one qualifying session -- nothing to combine."""
    events = [
        _event(
            "q",
            SessionType.QUALIFYING,
            start=datetime(2026, 4, 18, 13, tzinfo=UTC),
        ),
        _event("r", SessionType.RACE, start=datetime(2026, 4, 19, 13, tzinfo=UTC)),
    ]

    result = _combine_qualifying(events)

    assert {e.uid for e in result} == {"q", "r"}


def test_an_unconfirmed_session_in_the_group_blocks_the_merge():
    """No real time to build a span from, so the group is left untouched."""
    events = [
        _event(
            "wec-2026-lsl-qualifying-lmgt3",
            SessionType.QUALIFYING,
            start=datetime(2026, 9, 5, 20, 0, tzinfo=UTC),
        ),
        _event(
            "wec-2026-lsl-hyperpole-lmgt3",
            SessionType.HYPERPOLE,
            time_confirmed=False,
        ),
    ]

    result = _combine_qualifying(events)

    assert {e.uid for e in result} == {
        "wec-2026-lsl-qualifying-lmgt3",
        "wec-2026-lsl-hyperpole-lmgt3",
    }


def test_combines_only_whatever_survived_an_earlier_session_type_filter():
    """`sessions=qualifying` already dropped hyperpole before this step runs."""
    events = [
        e for e in _lone_star_le_mans() if e.session_type == SessionType.QUALIFYING
    ]

    result = _combine_qualifying(events)

    combined = _combined(result)
    assert combined.start == datetime(2026, 9, 5, 20, 0, tzinfo=UTC)
    # Hyperpole was never in the input, so it can't stretch the combined span:
    # 20:00 (qualifying-lmgt3 start) to 21:00 (qualifying-hypercar's own end).
    assert combined.duration_seconds == 60 * 60
    assert len(result) == 1


def test_two_seasons_of_the_same_named_event_are_not_merged_together():
    """A series reuses an event name across seasons (WEC's "Lone Star Le Mans"
    every year) -- the name alone must not be the grouping key, or an
    unconfirmed future season blocks merging this year's confirmed one.
    """
    events = [
        _event(
            "wec-2026-lsl-qualifying-lmgt3",
            SessionType.QUALIFYING,
            event_key="wec-2026-lsl-fp1",
            start=datetime(2026, 9, 5, 20, 0, tzinfo=UTC),
        ),
        _event(
            "wec-2026-lsl-hyperpole-lmgt3",
            SessionType.HYPERPOLE,
            event_key="wec-2026-lsl-fp1",
            start=datetime(2026, 9, 5, 20, 20, tzinfo=UTC),
        ),
        # Next year's edition -- same name, still TBC.
        _event(
            "wec-2027-lsl-qualifying-lmgt3",
            SessionType.QUALIFYING,
            event_key="wec-2027-lsl-fp1",
            time_confirmed=False,
        ),
        _event(
            "wec-2027-lsl-hyperpole-lmgt3",
            SessionType.HYPERPOLE,
            event_key="wec-2027-lsl-fp1",
            time_confirmed=False,
        ),
    ]

    result = _combine_qualifying(events)

    combined_uids = [e.uid for e in result if "combined" in e.uid]
    assert len(combined_uids) == 1  # this year's edition merged...
    uncombined_uids = {e.uid for e in result if "combined" not in e.uid}
    assert uncombined_uids == {  # ...next year's TBC edition is untouched
        "wec-2027-lsl-qualifying-lmgt3",
        "wec-2027-lsl-hyperpole-lmgt3",
    }


def test_two_seasons_sharing_both_name_and_round_are_still_kept_apart():
    """Round numbers are seasonal ordinals, not identities -- two different
    seasons can coincidentally share one (e.g. both happen to be round 5).
    `event_key` (derived from the weekend's own first session uid, globally
    unique) must still tell them apart even then.
    """
    events = [
        _event(
            "wec-2026-lsl-qualifying-lmgt3",
            SessionType.QUALIFYING,
            event_key="wec-2026-lsl-fp1",
            start=datetime(2026, 9, 5, 20, 0, tzinfo=UTC),
        ),
        _event(
            "wec-2026-lsl-hyperpole-lmgt3",
            SessionType.HYPERPOLE,
            event_key="wec-2026-lsl-fp1",
            start=datetime(2026, 9, 5, 20, 20, tzinfo=UTC),
        ),
        # A different season, same event name, coincidentally also "round 5" --
        # but a genuinely different weekend, confirmed with its own real times.
        _event(
            "wec-2027-lsl-qualifying-lmgt3",
            SessionType.QUALIFYING,
            event_key="wec-2027-lsl-fp1",
            start=datetime(2027, 9, 11, 20, 0, tzinfo=UTC),
        ),
        _event(
            "wec-2027-lsl-hyperpole-lmgt3",
            SessionType.HYPERPOLE,
            event_key="wec-2027-lsl-fp1",
            start=datetime(2027, 9, 11, 20, 20, tzinfo=UTC),
        ),
    ]

    result = _combine_qualifying(events)

    combined = [e for e in result if "combined" in e.uid]
    assert len(combined) == 2  # each season merges on its own...
    assert {e.start.year for e in combined} == {2026, 2027}  # ...never together


def test_combining_is_deterministic_across_repeated_requests():
    """A subscriber polls the same feed URL repeatedly -- the merged event must be stable."""
    first = _combine_qualifying(_lone_star_le_mans())
    second = _combine_qualifying(_lone_star_le_mans())

    assert _combined(first).uid == _combined(second).uid
    assert _combined(first).sequence == _combined(second).sequence
    assert _combined(first).fingerprint == _combined(second).fingerprint
