from motorcal.config import PatchConfig, PatchMatcher
from motorcal.merge import MatchedPatch, PatchMatchError, match_all_patches
from motorcal.models import SourceEvent, SourceEventKey

WEC_RACE = SourceEvent(
    key=SourceEventKey(provider="thesportsdb", id_event="2421035"),
    series="wec",
    season="2026",
    round=1,
    name="6 Hours of Imola",
    date="2026-04-19",
    time="00:00:00",
    venue="Imola",
    country="Italy",
    raw={},
)
WEC_QUALIFYING = SourceEvent(
    key=SourceEventKey(provider="thesportsdb", id_event="2421036"),
    series="wec",
    season="2026",
    round=1,
    name="6 Hours of Imola Qualifying",
    date="2026-04-18",
    time="12:30:00",
    venue="Imola",
    country="Italy",
    raw={},
)
F1_RACE = SourceEvent(
    key=SourceEventKey(provider="thesportsdb", id_event="9999"),
    series="f1",
    season="2026",
    round=1,
    name="Australian Grand Prix",
    date="2026-03-08",
    time="04:00:00",
    venue="Melbourne",
    country="Australia",
    raw={},
)

ALL_EVENTS = [WEC_RACE, WEC_QUALIFYING, F1_RACE]


def test_id_event_patch_matches_exactly_one_event():
    patch = PatchConfig(id_event="2421035", start="2026-04-19T13:00:00Z", duration="6h", note="official WEC timetable")
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert errors == []
    assert len(matches) == 1
    assert matches[0].patch is patch
    assert matches[0].source_event is WEC_RACE


def test_fallback_matcher_matches_exactly_one_event():
    patch = PatchConfig(match=PatchMatcher(series="wec", date="2026-04-19", contains="Imola"))
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert errors == []
    assert len(matches) == 1
    assert matches[0].source_event is WEC_RACE


def test_fallback_matcher_contains_is_case_insensitive():
    patch = PatchConfig(match=PatchMatcher(series="wec", date="2026-04-19", contains="imola"))
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert errors == []
    assert len(matches) == 1


def test_id_event_patch_with_no_match_is_a_validation_error():
    patch = PatchConfig(id_event="does-not-exist")
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert matches == []
    assert len(errors) == 1
    assert errors[0].reason == "no_match"
    assert errors[0].candidate_count == 0


def test_fallback_matcher_with_no_match_is_a_validation_error():
    patch = PatchConfig(match=PatchMatcher(series="wec", date="2099-01-01", contains="Imola"))
    matches, errors = match_all_patches([patch], ALL_EVENTS)

    assert matches == []
    assert len(errors) == 1
    assert errors[0].reason == "no_match"


def test_fallback_matcher_with_multiple_matches_is_a_validation_error():
    duplicate = SourceEvent(
        key=SourceEventKey(provider="thesportsdb", id_event="2421035-dup"),
        series="wec",
        season="2026",
        round=1,
        name="6 Hours of Imola (Rescheduled)",
        date="2026-04-19",
        time="00:00:00",
        venue="Imola",
        country="Italy",
        raw={},
    )
    patch = PatchConfig(match=PatchMatcher(series="wec", date="2026-04-19", contains="Imola"))
    matches, errors = match_all_patches([patch], [WEC_RACE, duplicate])

    assert matches == []
    assert len(errors) == 1
    assert errors[0].reason == "multiple_matches"
    assert errors[0].candidate_count == 2


def test_multiple_valid_patches_all_match_independently():
    patch_a = PatchConfig(id_event="2421035", note="a")
    patch_b = PatchConfig(match=PatchMatcher(series="f1", date="2026-03-08", contains="Grand Prix"))
    matches, errors = match_all_patches([patch_a, patch_b], ALL_EVENTS)

    assert errors == []
    assert len(matches) == 2
    matched_ids = {m.source_event.key.id_event for m in matches}
    assert matched_ids == {"2421035", "9999"}


def test_one_bad_patch_does_not_prevent_others_from_matching():
    good_patch = PatchConfig(id_event="2421035")
    bad_patch = PatchConfig(id_event="does-not-exist")
    matches, errors = match_all_patches([good_patch, bad_patch], ALL_EVENTS)

    assert len(matches) == 1
    assert matches[0].patch is good_patch
    assert len(errors) == 1
    assert errors[0].patch is bad_patch


def test_no_patches_returns_empty_results():
    matches, errors = match_all_patches([], ALL_EVENTS)
    assert matches == []
    assert errors == []
