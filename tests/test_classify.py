from motorcal.classify import classify_session
from motorcal.models import SessionType


def test_round_500_is_always_testing_regardless_of_label():
    assert classify_session("Day 1", 500) is SessionType.TESTING
    assert classify_session("Morning Session", 500) is SessionType.TESTING
    assert classify_session("", 500) is SessionType.TESTING
    assert classify_session("Qualifying", 500) is SessionType.TESTING


def test_empty_label_is_the_race():
    """A session named after the event itself leaves nothing behind, and that is
    always the race -- for every series, without a name rule per series."""
    assert classify_session("", 1) is SessionType.RACE
    assert classify_session("", 13) is SessionType.RACE


def test_practice_labels():
    assert classify_session("Practice 1", 1) is SessionType.PRACTICE
    assert classify_session("Free Practice 3", 1) is SessionType.PRACTICE


def test_qualifying_label():
    assert classify_session("Qualifying", 1) is SessionType.QUALIFYING


def test_sprint_qualifying_before_sprint_and_qualifying():
    assert classify_session("Sprint Qualifying", 2) is SessionType.SPRINT_QUALIFYING


def test_sprint_label():
    assert classify_session("Sprint", 2) is SessionType.SPRINT


def test_hyperpole_before_qualifying():
    assert classify_session("Hyperpole Qualifying – LMP2 & LMGT3", 3) is SessionType.HYPERPOLE
    assert classify_session("Hyperpole 1 - Hypercar", 3) is SessionType.HYPERPOLE


def test_class_split_qualifying_is_plain_qualifying():
    assert classify_session("Qualifying - LMGT3", 2) is SessionType.QUALIFYING
    assert classify_session("Qualifying - Hypercar", 2) is SessionType.QUALIFYING


def test_unrecognized_label_is_unknown_not_race():
    assert classify_session("Drivers Parade", 1) is SessionType.UNKNOWN


def test_a_whole_event_name_as_label_still_classifies():
    """A session the provider does not name after its weekend keeps its full name as
    the label; the session word inside it still decides the type."""
    assert (
        classify_session("Snap-on INDYCAR Weekend Qualifying", 15) is SessionType.QUALIFYING
    )
    assert classify_session("Firestone Grand Prix of St. Petersburg", 1) is SessionType.UNKNOWN
