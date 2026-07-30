import yaml

from motorcal.config import (
    Config,
    DefaultsConfig,
    DurationDefaults,
    EventConfig,
    GlobalConfig,
    RetentionConfig,
    SeriesConfig,
    SourceSettings,
    SourceSnapshot,
    UnknownTimeConfig,
    save_series,
)
from motorcal.providers.thesportsdb import ProviderEvent, SnapshotResult
from motorcal.state import State

UID_DOMAIN = "racing.example.com"


def make_globals(
    *,
    uid_domain: str = UID_DOMAIN,
    durations: DurationDefaults | None = None,
    alerts: dict[str, list[str]] | None = None,
    retention: RetentionConfig | None = None,
    refresh_cron: str = "0 */6 * * *",
    next_season_from: str = "10-01",
    rate_limit_per_min: int = 6000,
    **kwargs,
) -> GlobalConfig:
    return GlobalConfig(
        uid_domain=uid_domain,
        source=SourceSettings(
            refresh_cron=refresh_cron,
            next_season_from=next_season_from,
            rate_limit_per_min=rate_limit_per_min,
        ),
        retention=retention or RetentionConfig(),
        defaults=DefaultsConfig(
            durations=durations or DurationDefaults(), alerts=alerts if alerts is not None else {}
        ),
        unknown_time=UnknownTimeConfig(),
        **kwargs,
    )


def make_series(
    *, league_id: int = 4413, name: str = "WEC", max_round: int = 20,
    race_only: bool = False, durations=None, alerts=None, events=None,
) -> SeriesConfig:
    return SeriesConfig(
        league_id=league_id, name=name, max_round=max_round, race_only=race_only,
        durations=durations, alerts=alerts, events=list(events or []),
    )


def make_config(*, series=None, **global_kwargs) -> Config:
    return Config(
        globals=make_globals(**global_kwargs),
        series=series or {"wec": make_series()},
    )


def source_snapshot(
    *, name: str = "6 Hours of Imola", date: str = "2026-04-19", time: str | None = "13:00:00",
    venue: str | None = "Imola", country: str | None = "Italy", round: int = 1,
    season: str = "2026",
) -> SourceSnapshot:
    return SourceSnapshot(
        name=name, date=date, time=time, venue=venue, country=country,
        round=round, season=season,
    )


def source_event(id_event: str = "1", *, disappeared_at: str | None = None, **snapshot_kwargs):
    """A provider-backed event whose fields still match its source snapshot."""
    from motorcal.sync import event_from_source

    event = event_from_source(source_snapshot(**snapshot_kwargs), id_event)
    event.disappeared_at = disappeared_at
    return event


def manual_event(uid: str = "my-event", *, summary: str = "Test Day", **kwargs) -> EventConfig:
    kwargs.setdefault("date", "2026-05-01")
    return EventConfig(uid=uid, summary=summary, **kwargs)


def provider_event(
    id_event: str = "1", *, name: str = "6 Hours of Imola", round: int = 1,
    season: str = "2026", series: str = "wec", date: str = "2026-04-19",
    time: str | None = "13:00:00", venue: str | None = "Imola", country: str | None = "Italy",
) -> ProviderEvent:
    return ProviderEvent(
        id_event=id_event, name=name, date=date, time=time, round=round, season=season,
        series=series, venue=venue, country=country, raw={"idEvent": id_event},
    )


def snapshot(events, *, complete: bool = True, diagnostics=None) -> SnapshotResult:
    return SnapshotResult(
        complete=complete, events=list(events), diagnostics=diagnostics or [],
        rounds_attempted=max(len(events), 1), rounds_failed=len(diagnostics or []),
    )


def make_state(*, uid_domain: str = UID_DOMAIN, **kwargs) -> State:
    return State(uid_domain=uid_domain, **kwargs)


def write_config_dir(tmp_path, config: Config):
    """Materialise a Config as a real directory, the way load_config expects it."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "motorcal.yaml").write_text(
        yaml.safe_dump(config.globals.model_dump(mode="json"), sort_keys=False)
    )
    for series, series_config in config.series.items():
        save_series(config_dir, series, series_config)
    return config_dir
