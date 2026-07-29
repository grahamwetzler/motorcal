from motorcal.classify import classify_event
from motorcal.models import SessionType


def test_round_500_is_always_testing_regardless_of_series_or_name():
    assert classify_event("f1", "Bahrain Testing 1 Day 1", 500) is SessionType.TESTING
    assert classify_event("wec", "Imola Prologue Morning Session", 500) is SessionType.TESTING
    assert classify_event("imsa", "Roar Before The Rolex 24", 500) is SessionType.TESTING
    assert classify_event("indycar", "Anything At All", 500) is SessionType.TESTING


def test_f1_practice_sessions():
    assert classify_event("f1", "Australian Grand Prix Practice 1", 1) is SessionType.PRACTICE
    assert classify_event("f1", "Australian Grand Prix Practice 2", 1) is SessionType.PRACTICE
    assert classify_event("f1", "Chinese Grand Prix Practice 1", 2) is SessionType.PRACTICE


def test_f1_qualifying():
    assert classify_event("f1", "Australian Grand Prix Qualifying", 1) is SessionType.QUALIFYING


def test_f1_sprint_qualifying_before_sprint_and_qualifying():
    assert classify_event("f1", "Chinese Grand Prix Sprint Qualifying", 2) is SessionType.SPRINT_QUALIFYING


def test_f1_sprint():
    assert classify_event("f1", "Chinese Grand Prix Sprint", 2) is SessionType.SPRINT


def test_f1_bare_name_is_race_via_positive_rule():
    assert classify_event("f1", "Australian Grand Prix", 1) is SessionType.RACE
    assert classify_event("f1", "Chinese Grand Prix", 2) is SessionType.RACE


def test_f1_unrecognized_name_is_unknown_not_race():
    assert classify_event("f1", "Drivers Parade", 1) is SessionType.UNKNOWN


def test_wec_hyperpole_before_qualifying():
    assert (
        classify_event("wec", "24 Hours of Le Mans Hyperpole Qualifying – LMP2 & LMGT3", 3)
        is SessionType.HYPERPOLE
    )
    assert (
        classify_event("wec", "24 Hours of Le Mans Hyperpole Qualifying – Hypercar", 3)
        is SessionType.HYPERPOLE
    )


def test_wec_class_split_qualifying_is_plain_qualifying():
    assert (
        classify_event("wec", "6 Hours of Spa Francorchamps Qualifying - LMGT3", 2)
        is SessionType.QUALIFYING
    )
    assert (
        classify_event("wec", "6 Hours of Spa Francorchamps Qualifying - Hypercar", 2)
        is SessionType.QUALIFYING
    )
    assert classify_event("wec", "6 Hours of Imola Qualifying", 1) is SessionType.QUALIFYING


def test_wec_practice():
    assert classify_event("wec", "6 Hours of Imola Free Practice 3", 1) is SessionType.PRACTICE
    assert (
        classify_event("wec", "24 Hours of Le Mans Free Practice 1", 3) is SessionType.PRACTICE
    )


def test_wec_bare_name_is_race_via_positive_rule():
    assert classify_event("wec", "6 Hours of Imola", 1) is SessionType.RACE
    assert classify_event("wec", "24 Hours of Le Mans", 3) is SessionType.RACE


def test_wec_unrecognized_name_is_unknown_not_race():
    assert classify_event("wec", "Drivers Parade", 1) is SessionType.UNKNOWN


def test_indycar_is_race_only_series():
    assert (
        classify_event("indycar", "Firestone Grand Prix of St. Petersburg", 1)
        is SessionType.RACE
    )


def test_imsa_is_race_only_series():
    assert classify_event("imsa", "Rolex 24 At DAYTONA", 1) is SessionType.RACE
    assert classify_event("imsa", "Mobil 1 Twelve Hours of Sebring", 2) is SessionType.RACE
    assert classify_event("imsa", "Acura Grand Prix of Long Beach", 3) is SessionType.RACE


def test_unconfigured_series_is_always_unknown():
    assert classify_event("some_future_series", "Anything", 1) is SessionType.UNKNOWN
