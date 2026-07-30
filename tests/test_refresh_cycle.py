import json
from datetime import datetime, timezone

import httpx
from tests.conftest import UID_DOMAIN, make_config, make_series, make_state, manual_event

from motorcal.refresh import run_refresh_cycle
from motorcal.state import scope_key

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _config(**kwargs):
    return make_config(
        series={
            "wec": make_series(league_id=4413, name="WEC", max_round=1),
            "imsa": make_series(league_id=4488, name="IMSA", max_round=1, race_only=True),
        },
        alerts={"race": ["-1d"]},
        **kwargs,
    )


def _wec_handler(request: httpx.Request) -> httpx.Response:
    if int(request.url.params["r"]) == 1 and request.url.params["id"] == "4413":
        body = {"events": [{
            "idEvent": "2421035", "idLeague": "4413", "strSeason": request.url.params["s"],
            "dateEvent": "2026-04-19", "strTime": "13:00:00", "strEvent": "6 Hours of Imola",
            "strVenue": "Imola", "strCountry": "Italy",
        }]}
    else:
        body = {"events": None}
    return httpx.Response(200, text=json.dumps(body))


def _patched_client(monkeypatch, handler=_wec_handler):
    monkeypatch.setattr(
        "motorcal.refresh.build_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _run(config, state=None, now=NOW):
    return run_refresh_cycle(config, state or make_state(), api_key="3", now=now)


def test_refresh_cycle_fetches_and_publishes(monkeypatch):
    _patched_client(monkeypatch)
    config = _config()

    result = _run(config)

    assert result.series_season_outcomes["wec"]["2026"] == "committed"
    assert [e.id_event for e in config.series["wec"].events] == ["2421035"]
    assert result.published["wec"][0].uid == f"thesportsdb-2421035@{UID_DOMAIN}"
    assert result.diagnostics["events_published"] == 1


def test_refresh_cycle_records_the_snapshot_for_freshness(monkeypatch):
    _patched_client(monkeypatch)
    state = make_state()

    _run(_config(), state)

    assert state.snapshots[scope_key("wec", "2026")].count == 1
    assert state.snapshots[scope_key("wec", "2026")].last_complete_at == NOW.isoformat()


def test_refresh_cycle_reports_which_series_it_touched(monkeypatch):
    _patched_client(monkeypatch)

    result = _run(_config())

    # imsa returned nothing for the current season, which is suspicious and rejected,
    # so only wec's file needs rewriting.
    assert result.synced_series == {"wec"}
    assert result.series_season_outcomes["imsa"]["2026"] == "suspicious_empty_current_season"


def test_refresh_cycle_preserves_a_hand_edited_field(monkeypatch):
    _patched_client(monkeypatch)
    config = _config()
    state = make_state()
    _run(config, state)

    event = config.series["wec"].events[0]
    event.summary = "6 Hours of Imola (my title)"
    event.duration = "6h"

    _run(config, state, now=datetime(2026, 1, 2, tzinfo=timezone.utc))

    event = config.series["wec"].events[0]
    assert event.summary == "6 Hours of Imola (my title)"
    assert event.duration == "6h"


def test_refresh_cycle_leaves_manual_events_alone(monkeypatch):
    _patched_client(monkeypatch)
    config = _config()
    config.series["wec"].events.append(manual_event("mine"))

    _run(config)

    mine = next(e for e in config.series["wec"].events if e.key == "mine")
    assert mine.summary == "Test Day"
    assert mine.disappeared_at is None


def test_refresh_cycle_fetches_next_season_after_the_cutoff(monkeypatch):
    _patched_client(monkeypatch)

    result = _run(_config(), now=datetime(2026, 12, 15, tzinfo=timezone.utc))  # after "10-01"

    assert set(result.series_season_outcomes["wec"].keys()) == {"2026", "2027"}


def test_refresh_cycle_publishes_nothing_when_every_scan_is_rejected(monkeypatch):
    _patched_client(monkeypatch, lambda request: httpx.Response(200, text='{"events": null}'))

    result = _run(_config())

    assert result.published is None
    assert result.synced_series == set()
    assert result.series_season_outcomes["wec"]["2026"] == "suspicious_empty_current_season"


def test_refresh_cycle_surfaces_scan_errors(monkeypatch):
    # A malformed body fails without retrying, so this doesn't sit through backoff.
    _patched_client(monkeypatch, lambda request: httpx.Response(200, text="not json"))

    result = _run(_config())

    assert result.scan_errors
    assert result.published is None


def test_a_refresh_merges_into_what_is_on_disk_not_a_stale_copy(monkeypatch, tmp_path):
    """Regression: the refresh rewrites series files, so it must start from the
    file. Merging into an in-memory snapshot silently reverts any edit made since
    that snapshot -- and a rejected reload can keep one stale indefinitely."""
    from tests.conftest import write_config_dir

    from motorcal.config import load_config, save_series

    _patched_client(monkeypatch)
    config_dir = write_config_dir(tmp_path, _config())
    state = make_state()

    # Cycle one populates the file, and we keep the resulting config object around
    # to stand in for the stale `app.state.config` the bug used to reuse.
    stale = load_config(config_dir)
    run_refresh_cycle(stale, state, api_key="3", now=NOW)
    save_series(config_dir, "wec", stale.series["wec"])

    # Someone edits the file directly, the way a person would.
    on_disk = load_config(config_dir)
    on_disk.series["wec"].events[0].summary = "Edited by hand"
    save_series(config_dir, "wec", on_disk.series["wec"])

    # Cycle two, done correctly: re-read, merge, write back.
    fresh = load_config(config_dir)
    run_refresh_cycle(fresh, state, api_key="3", now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    save_series(config_dir, "wec", fresh.series["wec"])

    assert load_config(config_dir).series["wec"].events[0].summary == "Edited by hand"
    # And the stale copy, had it been used, would indeed have clobbered it.
    assert stale.series["wec"].events[0].summary == "6 Hours of Imola"
