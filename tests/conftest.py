import yaml

from motorcal.config import (
    Config,
    DefaultsConfig,
    EventConfig,
    GlobalConfig,
    RetentionConfig,
    SeriesConfig,
    SessionConfig,
    UnknownTimeConfig,
)
from motorcal.state import State

UID_DOMAIN = "racing.example.com"


def make_globals(
    *,
    uid_domain: str = UID_DOMAIN,
    durations: dict[str, str] | None = None,
    alerts: dict[str, list[str]] | None = None,
    retention: RetentionConfig | None = None,
    **kwargs,
) -> GlobalConfig:
    return GlobalConfig(
        uid_domain=uid_domain,
        retention=retention or RetentionConfig(),
        defaults=DefaultsConfig(
            durations=durations or {}, alerts=alerts if alerts is not None else {}
        ),
        unknown_time=UnknownTimeConfig(),
        **kwargs,
    )


def make_series(
    *,
    name: str = "WEC",
    schedule_url: str | None = None,
    durations=None,
    alerts=None,
    events=None,
    changes=None,
) -> SeriesConfig:
    return SeriesConfig(
        name=name,
        schedule_url=schedule_url,
        durations=durations,
        alerts=alerts,
        events=list(events or []),
        changes=list(changes or []),
    )


def make_config(*, series=None, **global_kwargs) -> Config:
    return Config(
        globals=make_globals(**global_kwargs),
        series=series or {"wec": make_series()},
    )


def make_session(
    uid: str = "my-session", *, type: str = "race", **kwargs
) -> SessionConfig:
    """One session. Defaults to an all-day race, so only what a test cares about is passed."""
    if not kwargs.get("start"):
        kwargs.setdefault("date", "2026-05-01")
    return SessionConfig(uid=uid, type=type, **kwargs)


def make_event(
    uid: str = "my-event",
    *,
    name: str = "6 Hours of Imola",
    url: str | None = None,
    location: str | None = None,
    round: int | None = None,
    **kwargs,
) -> EventConfig:
    """A race event of one session."""
    return EventConfig(
        name=name,
        url=url,
        location=location,
        round=round,
        sessions=[make_session(uid, **kwargs)],
    )


def make_state(*, uid_domain: str = UID_DOMAIN, **kwargs) -> State:
    return State(uid_domain=uid_domain, **kwargs)


def write_config_dir(tmp_path, config: Config):
    """Materialise a Config as a real directory, the way load_config expects it."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "defaults.yaml").write_text(
        yaml.safe_dump(
            config.globals.model_dump(mode="json", exclude={"uid_domain"}),
            sort_keys=False,
        )
    )
    for series, series_config in config.series.items():
        (config_dir / f"{series}.yaml").write_text(
            yaml.safe_dump(
                series_config.model_dump(mode="json", exclude_none=True),
                sort_keys=False,
            )
        )
    return config_dir
