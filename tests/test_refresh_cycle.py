import json
from datetime import datetime, timezone

import httpx

from motorcal.config import (
    DefaultsConfig,
    DurationDefaults,
    OverridesConfig,
    PatchConfig,
    RetentionConfig,
    RootConfig,
    SeriesConfig,
    UnknownTimeConfig,
)
from motorcal.refresh import run_refresh_cycle
from motorcal.store import (
    connect,
    get_feed_revision,
    get_published_event,
    get_refresh_diagnostics,
    get_source_event,
    init_schema,
)
from motorcal.models import source_uid

UID_DOMAIN = "x.example.com"


def _root_config(series=None, next_season_from="10-01"):
    return RootConfig(
        server={"base_url": f"https://{UID_DOMAIN}", "uid_domain": UID_DOMAIN},
        source={"refresh_cron": "0 * * * *", "next_season_from": next_season_from,
                "rate_limit_per_min": 6000},
        retention=RetentionConfig(),
        defaults=DefaultsConfig(
            durations=DurationDefaults(), alerts={"race": ["-1d"]}, include_sessions=["race"],
        ),
        unknown_time=UnknownTimeConfig(),
        series=series or {"wec": SeriesConfig(league_id=4413, name="WEC", max_round=1)},
    )


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _single_race_handler(request: httpx.Request) -> httpx.Response:
    round_number = int(request.url.params["r"])
    if round_number == 1:
        body = {
            "events": [
                {
                    "idEvent": "2421035", "idLeague": "4413", "strSeason": request.url.params["s"],
                    "dateEvent": "2026-04-19", "strTime": "13:00:00", "strEvent": "6 Hours of Imola",
                    "strVenue": "Imola", "strCountry": "Italy",
                }
            ]
        }
    else:
        body = {"events": None}
    return httpx.Response(200, text=json.dumps(body))


def _patched_client(monkeypatch):
    def fake_build_client():
        return httpx.Client(transport=httpx.MockTransport(_single_race_handler))

    monkeypatch.setattr("motorcal.refresh.build_client", fake_build_client)


def test_refresh_cycle_ingests_and_publishes(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = run_refresh_cycle(
        conn, root_config=_root_config(), overrides=OverridesConfig(), api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    assert result.lease_acquired is True
    assert result.series_season_outcomes["wec"]["2026"] == "committed"
    assert get_source_event(conn, "thesportsdb", "2421035") is not None
    assert get_published_event(conn, source_uid("2421035", UID_DOMAIN)) is not None
    assert result.rebuild_report.events_published == 1


def test_refresh_cycle_syncs_feed_revision_for_every_series(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    run_refresh_cycle(
        conn, root_config=_root_config(), overrides=OverridesConfig(), api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    revision = get_feed_revision(conn, "wec")
    assert revision is not None
    assert revision["revision"] != ""


def test_refresh_cycle_persists_diagnostics(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    run_refresh_cycle(
        conn, root_config=_root_config(), overrides=OverridesConfig(), api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    diagnostics = get_refresh_diagnostics(conn)
    assert diagnostics is not None
    assert diagnostics["events_published"] == 1
    assert json.loads(diagnostics["unknown_events_json"]) == []


def test_refresh_cycle_skips_entirely_when_lease_already_held(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    from motorcal.store import acquire_lease
    acquire_lease(conn, "other-worker", ttl_seconds=300, now=now.timestamp())

    result = run_refresh_cycle(
        conn, root_config=_root_config(), overrides=OverridesConfig(), api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    assert result.lease_acquired is False
    assert get_source_event(conn, "thesportsdb", "2421035") is None  # nothing was fetched


def test_refresh_cycle_reconciles_synthetic_events(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    from motorcal.config import SyntheticEventConfig
    from motorcal.models import synthetic_event_uid

    synthetic_cfg = SyntheticEventConfig(
        uid="imsa-2026-rolex-24", series="imsa", summary="Rolex 24 at Daytona",
        start="2026-01-25T18:40:00Z", duration="24h",
    )
    overrides = OverridesConfig(events=[synthetic_cfg])

    run_refresh_cycle(
        conn, root_config=_root_config(), overrides=overrides, api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    row = get_published_event(conn, synthetic_event_uid("imsa-2026-rolex-24", UID_DOMAIN))
    assert row is not None


def test_refresh_cycle_fetches_next_season_after_cutoff(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 12, 15, tzinfo=timezone.utc)  # after the "10-01" cutoff

    result = run_refresh_cycle(
        conn, root_config=_root_config(next_season_from="10-01"), overrides=OverridesConfig(),
        api_key="3", uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    assert set(result.series_season_outcomes["wec"].keys()) == {"2026", "2027"}


def test_refresh_cycle_rolls_back_ingest_when_a_patch_fails_to_match(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    overrides = OverridesConfig(patches=[PatchConfig(id_event="does-not-exist")])

    result = run_refresh_cycle(
        conn, root_config=_root_config(), overrides=overrides, api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=300, now=now,
    )

    assert result.series_season_outcomes["wec"]["2026"] == "patch_error_blocked"
    # The whole transaction rolled back -- the freshly scanned source event too, not
    # just the publication -- so a crash can never leave new source paired with old
    # publication just because an unrelated patch is broken.
    assert get_source_event(conn, "thesportsdb", "2421035") is None
    assert get_published_event(conn, source_uid("2421035", UID_DOMAIN)) is None

    diagnostics = get_refresh_diagnostics(conn)
    assert diagnostics is not None
    assert json.loads(diagnostics["patch_errors_json"])[0]["reason"] == "no_match"


def test_refresh_cycle_stops_committing_once_the_lease_is_lost_mid_cycle(tmp_path, monkeypatch):
    _patched_client(monkeypatch)
    conn = _fresh_conn(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # Simulate a scan that ran long enough (retries/backoff) to blow past a 1-second
    # lease TTL: the cycle's first time.monotonic() call anchors the start, the
    # second (checked right before the first write) reports 1000s of elapsed time.
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 1000.0

    monkeypatch.setattr("time.monotonic", fake_monotonic)

    result = run_refresh_cycle(
        conn, root_config=_root_config(), overrides=OverridesConfig(), api_key="3",
        uid_domain=UID_DOMAIN, lease_holder="worker-a", lease_ttl_seconds=1, now=now,
    )

    assert result.lease_acquired is True
    assert result.lease_lost is True
    assert result.series_season_outcomes["wec"] == {}  # broke out before committing anything
    assert get_source_event(conn, "thesportsdb", "2421035") is None
