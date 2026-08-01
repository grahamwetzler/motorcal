"""The query grammar that lets a subscriber shape their own feed.

Every parameter is checked twice over: that it does what it says, and that a
malformed or misplaced one is a 400 rather than something quietly ignored.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_config, make_series

from motorcal.ics import render_combined_bytes
from motorcal.models import EventStatus, PublishedEvent, SessionType
from motorcal.web import Publication, create_app

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONFIG = make_config(
    series={"wec": make_series(), "f1": make_series(name="F1")}
)
PREBUILT = b"BEGIN:VCALENDAR\r\nSUMMARY:prebuilt\r\nEND:VCALENDAR\r\n"


def _event(uid, session_type=SessionType.RACE, *, series="wec", alarms=None, confirmed=True):
    return PublishedEvent(
        uid=uid, series=series, session_type=session_type, summary=uid,
        start=datetime(2026, 4, 19, 13, tzinfo=timezone.utc) if confirmed else None,
        all_day_date=None if confirmed else "2026-04-19", time_confirmed=confirmed,
        duration_seconds=3600, location=None, description="D",
        status=EventStatus.CONFIRMED, sequence=1, dtstamp=NOW, last_modified=NOW,
        fingerprint="fp", alarms=list(alarms or []), session_key=uid,
    )


PUBLISHED = {
    "wec": [_event("wec-race"), _event("wec-practice", SessionType.PRACTICE)],
    "f1": [_event("f1-race", series="f1"), _event("f1-practice", SessionType.PRACTICE, series="f1")],
}


def _client(published=None):
    app = create_app(CONFIG)
    app.state.publication = Publication(
        config=CONFIG,
        feeds={"events": PREBUILT},
        published=published if published is not None else PUBLISHED,
    )
    return TestClient(app)


def _get(query=""):
    path = "/events.ics"
    return _client().get(f"{path}?{query}" if query else path)


# ------------------------------------------------------------------- the fast path


def test_a_request_with_no_params_serves_the_prebuilt_bytes_untouched():
    assert _get().content == PREBUILT


def test_a_no_op_param_re_renders_the_exact_bytes_the_fast_path_would_serve():
    """Otherwise a subscriber's ETag flaps between the two paths on equivalent requests."""
    app = create_app(CONFIG)
    real_feed = render_combined_bytes(CONFIG, PUBLISHED)
    app.state.publication = Publication(
        config=CONFIG, feeds={"events": real_feed}, published=PUBLISHED
    )
    client = TestClient(app)

    assert client.get("/events.ics?emoji=false").content == real_feed


def test_a_global_and_a_per_series_setting_render_identically():
    plain = _client().get("/events.ics?series=wec&sessions=race,practice")
    prefixed = _client().get("/events.ics?series=wec&wec.sessions=race,practice")

    assert plain.content == prefixed.content


# ----------------------------------------------------------------------- series


def test_series_selects_only_the_named_series():
    body = _get(query="series=f1").content

    assert b"UID:f1-race" in body
    assert b"UID:wec-race" not in body


def test_unknown_series_is_rejected():
    assert _get(query="series=motogp").status_code == 400


# --------------------------------------------------------------------- sessions


def test_sessions_is_an_allow_list():
    body = _get(query="sessions=race").content

    assert b"UID:wec-race" in body
    assert b"UID:f1-race" in body
    assert b"UID:wec-practice" not in body


def test_a_per_series_sessions_override_beats_the_global_one():
    body = _get(query="sessions=race&wec.sessions=practice").content

    assert b"UID:wec-practice" in body
    assert b"UID:wec-race" not in body
    assert b"UID:f1-race" in body
    assert b"UID:f1-practice" not in body


def test_warmup_is_a_selectable_session_type():
    """The type IMSA and IndyCar actually run, added when TheSportsDB was dropped."""
    published = {"wec": [_event("wec-warmup", SessionType.WARMUP), _event("wec-race")], "f1": []}
    body = _client(published).get("/events.ics?series=wec&sessions=warmup").content

    assert b"UID:wec-warmup" in body
    assert b"UID:wec-race" not in body


def test_unknown_session_type_is_rejected():
    assert _get(query="sessions=lunch").status_code == 400
    # `unknown` was a real type until the provider went; it must not linger as one.
    assert _get(query="sessions=unknown").status_code == 400


def test_empty_member_in_a_list_is_rejected():
    assert _get(query="sessions=race,").status_code == 400


# ----------------------------------------------------------------------- alarms


def test_alarms_override_the_configured_ones():
    published = {"wec": [_event("wec-race", alarms=["-1d"])], "f1": []}
    body = _client(published).get("/events.ics?alarms=-2h").content

    assert b"TRIGGER:-PT2H" in body
    assert b"TRIGGER:-P1D" not in body


def test_omitting_alarms_keeps_the_configured_ones():
    published = {"wec": [_event("wec-race", alarms=["-1d"])], "f1": []}
    body = _client(published).get("/events.ics?sessions=race").content

    assert b"TRIGGER:-P1D" in body


def test_an_empty_alarms_value_silences_the_feed():
    published = {"wec": [_event("wec-race", alarms=["-1d"])], "f1": []}
    body = _client(published).get("/events.ics?alarms=").content

    assert b"BEGIN:VALARM" not in body


def test_an_oversized_offset_is_a_400_not_an_overflow():
    assert _get(query="alarms=-1000000000d").status_code == 400


def test_too_many_alarms_in_one_list_is_rejected():
    offsets = ",".join(f"-{n}d" for n in range(1, 12))
    assert _get(query=f"alarms={offsets}").status_code == 400


def test_per_type_alarms_apply_only_to_that_session_type():
    published = {
        "wec": [_event("wec-race"), _event("wec-practice", SessionType.PRACTICE)], "f1": [],
    }
    body = _client(published).get("/events.ics?alarms_race=-1h").content

    assert body.count(b"BEGIN:VALARM") == 1
    assert b"TRIGGER:-PT1H" in body


def test_alarm_precedence_runs_most_specific_first():
    """series+type > series > global type > global."""
    published = {"wec": [_event("wec-race")], "f1": [_event("f1-race", series="f1")]}
    body = _client(published).get(
        "/events.ics?alarms=-1d&alarms_race=-2d&wec.alarms=-3d&wec.alarms_race=-4d"
    ).content

    assert b"TRIGGER:-P4D" in body  # wec: the series+type override wins
    assert b"TRIGGER:-P2D" in body  # f1: falls back to the global per-type one
    assert b"TRIGGER:-P1D" not in body
    assert b"TRIGGER:-P3D" not in body


def test_an_unconfirmed_time_gets_no_alarms_even_when_the_url_asks():
    """Same guard `resolve_alarms` applies: there is no real start to hang it off."""
    published = {"wec": [_event("tbc", confirmed=False)], "f1": []}
    body = _client(published).get("/events.ics?alarms=-1h").content

    assert b"BEGIN:VALARM" not in body


def test_testing_sessions_get_no_alarms_even_when_the_url_asks():
    published = {"wec": [_event("test", SessionType.TESTING)], "f1": []}
    body = _client(published).get("/events.ics?alarms=-1h").content

    assert b"BEGIN:VALARM" not in body


def test_a_malformed_alarm_offset_is_rejected():
    assert _get(query="alarms=soon").status_code == 400
    assert _get(query="alarms=1h").status_code == 400  # no sign: not a valid offset


# ------------------------------------------------------------------ emoji, name


def test_emoji_prefixes_every_title():
    body = _get(query="emoji=true").content

    assert "SUMMARY:\N{CHEQUERED FLAG} WEC: wec-race".encode() in body


def test_emoji_defaults_to_off():
    assert "\N{CHEQUERED FLAG}".encode() not in _get(query="sessions=race").content


def test_emoji_cannot_be_set_for_one_series():
    assert _get(query="wec.emoji=true").status_code == 400


def test_a_non_boolean_emoji_value_is_rejected():
    assert _get(query="emoji=maybe").status_code == 400


def test_name_sets_the_calendar_name():
    assert b"X-WR-CALNAME:My Racing" in _get(query="name=My Racing").content


# ------------------------------------------------------------- malformed input


def test_a_repeated_key_is_rejected_rather_than_merged():
    response = _client().get("/events.ics?sessions=race&sessions=practice")

    assert response.status_code == 400
    assert "repeated" in response.json()["detail"]


def test_an_unknown_param_is_rejected():
    assert _get(query="rounds=1-5").status_code == 400


def test_an_unknown_prefix_is_reported_as_an_unknown_series():
    response = _get(query="alarms.race=-1h")

    assert response.status_code == 400
    assert "unknown series" in response.json()["detail"]


def test_settings_for_a_series_outside_the_selection_are_rejected():
    assert _get(query="series=f1&wec.sessions=race").status_code == 400


# ------------------------------------------------------- legacy alias handling


@pytest.mark.parametrize("query", ["practices=false", "qualifying=false"])
def test_the_legacy_aliases_still_work_on_their_own(query):
    assert _get(query=query).status_code == 200


def test_practices_false_still_drops_practices():
    body = _get(query="practices=false").content

    assert b"UID:wec-practice" not in body
    assert b"UID:wec-race" in body


def test_a_legacy_alias_combined_with_sessions_is_rejected():
    response = _get(query="practices=false&sessions=race")

    assert response.status_code == 400
    assert "sessions" in response.json()["detail"]


def test_a_prefixed_legacy_alias_is_rejected():
    assert _get(query="wec.practices=false").status_code == 400
