"""Public feed app (port 8000): one combined ICS feed, filterable by series.

The refresh and reload jobs each build a fresh `Publication` and swap it onto
`app.state.publication` in one assignment. A request reads that attribute
exactly once at the top of the handler, so it always sees one consistent
generation of config/feeds/published together -- never config from a rebuild
paired with feeds from the one before it (or a series that generation removed).

A request with no query parameters is served the prebuilt bytes. Anything else
is a `Selection`: the subscriber's own cut of the feed, filtered and re-rendered
per request. That costs a render, but nothing else -- the ETag is a hash of the
bytes actually served, so every variant revalidates correctly on its own, and
alarms and the title prefix are applied here at render time, never written back
to the version ledger. Nobody's calendar re-notifies because someone else asked
for `?emoji=true`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import QueryParams

from motorcal.config import COMBINED_SERIES_KEY, Config, ConfigError, parse_alarm_offset
from motorcal.ics import build_title, compute_content_hash, render_combined_bytes
from motorcal.models import PublishedEvent, SessionType

_access_logger = logging.getLogger("motorcal.access")

# Session types excluded by `?qualifying=false`. Different series name their
# pole-setting session differently (WEC's hyperpole, F1's sprint qualifying),
# but they're all "qualifying" from a subscriber's point of view.
_QUALIFYING_TYPES = {
    SessionType.QUALIFYING, SessionType.HYPERPOLE, SessionType.SPRINT_QUALIFYING,
}

# An alarm needs a confirmed start to hang off, and a test day has nothing worth
# alerting about. `resolve_alarms` skips it for configured alarms; a URL override
# must not be a way around that.
_NO_ALARM_TYPES = {SessionType.TESTING}

class EmojiOption(str, Enum):
    """What, if anything, to put in front of every published title."""

    NONE = "none"
    FLAG = "flag"
    CAR = "car"


_EMOJI_PREFIXES = {
    EmojiOption.NONE: "",
    EmojiOption.FLAG: "\N{CHEQUERED FLAG} ",
    EmojiOption.CAR: "\N{RACING CAR} ",
}
# `emoji=true`/`emoji=false` predate the choice of emoji and are kept as
# aliases for the two options they used to mean.
_EMOJI_BOOL_ALIASES = {"true": EmojiOption.FLAG, "false": EmojiOption.NONE}

# The feed-builder page served at `/`. Read once at import: it ships inside the
# package, so it can only change with a new image.
_INDEX_HTML = (Path(__file__).parent / "index.html").read_text()

# `alarms_race=-1h` and friends: one per session type.
_ALARM_PARAMS = {f"alarms_{session_type.value}": session_type for session_type in SessionType}
# Settable for one series as `f1.sessions=race`, or for every series unprefixed.
_SERIES_PARAMS = {"sessions", "alarms", *_ALARM_PARAMS}
# `practices`/`qualifying` predate `sessions` and are kept for feeds already
# subscribed to in people's calendar apps.
_LEGACY_PARAMS = {"practices", "qualifying"}
_GLOBAL_ONLY_PARAMS = {"series", "emoji", "name", *_LEGACY_PARAMS}
_GLOBAL_PARAMS = _SERIES_PARAMS | _GLOBAL_ONLY_PARAMS

_BOOLEANS = {
    "true": True, "1": True, "on": True, "yes": True,
    "false": False, "0": False, "off": False, "no": False,
}


@dataclass(frozen=True)
class Publication:
    """One consistent generation of what the app serves.

    Always replaced wholesale (never mutated in place) so assigning it to
    `app.state.publication` is the one atomic handoff a concurrent request can
    observe -- either the whole old generation or the whole new one.
    """

    config: Config
    feeds: dict[str, bytes]
    published: dict[str, list[PublishedEvent]]


@dataclass(frozen=True)
class _Filters:
    """What one series' events are cut down to, for one request."""

    sessions: frozenset[SessionType] | None  # None = every session type
    # Per session type, already resolved through the whole precedence chain.
    # None for a type = leave that event's configured alarms alone.
    alarms: dict[SessionType, list[str] | None]


@dataclass(frozen=True)
class Selection:
    """One request's cut of the feed. Private to this module."""

    series: tuple[str, ...]
    filters: dict[str, _Filters]
    prefix: str
    calname: str | None
    is_default: bool


def create_app(config: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.publication = Publication(config=config, feeds={}, published={})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # The builder page.
    @app.get("/", response_class=HTMLResponse)
    def get_index():
        publication = app.state.publication

        series = [
            {
                "key": key,
                "name": series_config.name,
                # Only the session types this series actually runs -- e.g. WEC's
                # hyperpole has no business as a per-series override for IndyCar.
                "sessions": sorted({
                    session.type.value for _, session in series_config.iter_sessions()
                }),
            }
            for key, series_config in publication.config.series.items()
        ]
        upcoming = _example_events(
            publication.config, publication.published, datetime.now(timezone.utc)
        )
        page = _INDEX_HTML.replace("__SERIES_JSON__", _inline_json(series)).replace(
            "__UPCOMING_JSON__", _inline_json(upcoming)
        )
        # The page carries real event times, so it must revalidate like the feeds
        # do rather than sit in a browser cache until the next race has been run.
        return HTMLResponse(page, headers={"Cache-Control": "public, no-cache"})

    @app.get(f"/{COMBINED_SERIES_KEY}.ics")
    def get_combined_calendar(request: Request):
        publication = app.state.publication

        ics_bytes = publication.feeds.get(COMBINED_SERIES_KEY)
        if not ics_bytes:
            raise HTTPException(status_code=503, detail="no usable events")

        selection = _parse_selection(request.query_params, publication.config)
        if not selection.is_default:
            published = {
                series: _select(publication.published.get(series, []), selection.filters[series])
                for series in selection.series
            }
            ics_bytes = render_combined_bytes(
                publication.config, published,
                prefix=selection.prefix, calname=selection.calname,
            )

        return _conditional_response(ics_bytes, request, COMBINED_SERIES_KEY)

    return app


# ------------------------------------------------------------------- index page


def _inline_json(value: object) -> str:
    """Serialise for embedding in a <script> block.

    Escaping `<` is the whole point: a series named with a stray `</script>`
    would otherwise end the block and turn the rest of the page into markup.
    """
    return json.dumps(value).replace("<", "\\u003c")


def _starts_at(event: PublishedEvent) -> datetime:
    """Where the session sits in time. An all-day session sits at midnight UTC."""
    if event.start is not None:
        return event.start
    return datetime.fromisoformat(event.all_day_date).replace(tzinfo=timezone.utc)


def _is_upcoming(event: PublishedEvent, now: datetime) -> bool:
    """Whether the session is still ahead of `now`.

    An all-day session has no time, so it counts as upcoming for the whole of
    its day rather than vanishing from the page at midnight UTC. That is a
    question about when it *ends*, kept apart from `_starts_at` so ordering two
    sessions of the same day doesn't rank the all-day one last.
    """
    if event.start is not None:
        return event.start >= now
    return _starts_at(event) + timedelta(days=1) > now


def _example_events(
    config: Config, published: dict[str, list[PublishedEvent]], now: datetime
) -> list[dict[str, object]]:
    """The next upcoming session of every (series, session type), for `/`.

    One per pair rather than one overall: the page filters this list by whatever
    the visitor ticked, and the earliest survivor of that filter is the real next
    event of the feed they just built. Bounded by construction -- at most one
    entry per series per session type.
    """
    soonest: dict[tuple[str, SessionType], PublishedEvent] = {}
    for series, events in published.items():
        for event in events:
            if not _is_upcoming(event, now):
                continue
            key = (series, event.session_type)
            if key not in soonest or _starts_at(event) < _starts_at(soonest[key]):
                soonest[key] = event

    return [
        {
            "series": event.series,
            "type": event.session_type.value,
            # The title the calendar app will show, built by the one rule that
            # builds it for real (`build_vevent`), minus the emoji prefix the
            # page applies itself when that box is ticked.
            "title": build_title(
                config.series[event.series].name, event.summary, event.status.value
            ),
            "start": event.start.isoformat() if event.start is not None else None,
            "date": event.all_day_date,
            "duration": event.duration_seconds,
            "location": event.location,
            "time_confirmed": event.time_confirmed,
            "alarms": list(event.alarms),
        }
        for event in sorted(soonest.values(), key=_starts_at)
    ]


# ------------------------------------------------------------------ query parsing


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _split(value: str, key: str) -> list[str]:
    """Split a comma-separated parameter, rejecting empty members."""
    parts = [part.strip() for part in value.split(",")]
    if not all(parts):
        raise _bad_request(f"empty value in {key!r}")
    return parts


def _parse_bool(value: str, key: str) -> bool:
    try:
        return _BOOLEANS[value.strip().lower()]
    except KeyError:
        raise _bad_request(f"{key!r} must be true or false (got {value!r})") from None


def _parse_emoji(value: str, key: str) -> str:
    normalized = value.strip().lower()
    option = _EMOJI_BOOL_ALIASES.get(normalized)
    if option is None:
        try:
            option = EmojiOption(normalized)
        except ValueError:
            valid = ", ".join(o.value for o in EmojiOption)
            raise _bad_request(f"{key!r} must be one of: {valid} (got {value!r})") from None
    return _EMOJI_PREFIXES[option]


def _parse_sessions(value: str, key: str) -> frozenset[SessionType]:
    session_types = set()
    for name in _split(value, key):
        try:
            session_types.add(SessionType(name))
        except ValueError:
            valid = ", ".join(sorted(member.value for member in SessionType))
            raise _bad_request(
                f"unknown session type {name!r} in {key!r} (expected one of: {valid})"
            ) from None
    return frozenset(session_types)


_MAX_ALARMS = 10  # one VALARM per offset per event -- caps anonymous list-based amplification


def _parse_alarms(value: str, key: str) -> list[str]:
    """Parse an alarm override. An empty value is meaningful: silence this feed."""
    if not value.strip():
        return []
    offsets = _split(value, key)
    if len(offsets) > _MAX_ALARMS:
        raise _bad_request(f"{key!r} accepts at most {_MAX_ALARMS} alarms (got {len(offsets)})")
    for offset in offsets:
        try:
            parse_alarm_offset(offset)
        except ConfigError as exc:
            raise _bad_request(f"{exc} in {key!r}") from None
    return offsets


def _first_set(*candidates: list[str] | None) -> list[str] | None:
    """First candidate that was actually given. An empty list is 'given' -- it silences."""
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


def _parse_selection(query: QueryParams, config: Config) -> Selection:
    """Turn one request's query string into the cut of the feed it asks for.

    Strict on the way in: an unknown or misplaced parameter is a 400, never
    something quietly ignored. Ignoring a typo would hand a subscriber a feed
    that silently isn't the one they asked for, and they would have no way to
    tell -- an ICS feed reports nothing back.
    """
    items = query.multi_items()
    keys = [key for key, _ in items]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        raise _bad_request(
            f"repeated query parameter(s): {', '.join(repeated)}. Give one comma-separated "
            "value instead (e.g. sessions=race,qualifying)"
        )

    # Split each key on its first '.': a known series key on the left makes it a
    # per-series override, otherwise the whole key is a global parameter name. So
    # `alarms_race` can never be read as a prefix.
    global_raw: dict[str, str] = {}
    per_series_raw: dict[str, dict[str, str]] = {}
    for key, value in items:
        prefix, _, param = key.partition(".")
        if not param:
            global_raw[key] = value
            continue
        if prefix not in config.series:
            raise _bad_request(f"unknown series {prefix!r} in query parameter {key!r}")
        per_series_raw.setdefault(prefix, {})[param] = value

    for key in global_raw:
        if key not in _GLOBAL_PARAMS:
            raise _bad_request(f"unknown query parameter {key!r}")
    for owner, params in per_series_raw.items():
        for key in params:
            if key in _SERIES_PARAMS:
                continue
            if key in _GLOBAL_PARAMS:
                raise _bad_request(
                    f"{key!r} applies to the whole feed and cannot be set for one "
                    f"series (got {owner}.{key})"
                )
            raise _bad_request(f"unknown query parameter '{owner}.{key}'")

    # Rather than define how a legacy exclusion interacts with the allow-list,
    # refuse the combination. Each alias on its own keeps behaving exactly as it
    # always has, and nothing already subscribed sends both.
    legacy = sorted(_LEGACY_PARAMS & set(global_raw))
    any_sessions = "sessions" in global_raw or any(
        "sessions" in params for params in per_series_raw.values()
    )
    if legacy and any_sessions:
        raise _bad_request(
            f"{', '.join(legacy)} cannot be combined with 'sessions' -- use 'sessions' alone"
        )

    if "series" in global_raw:
        selected = tuple(dict.fromkeys(_split(global_raw["series"], "series")))
        for key in selected:
            if key not in config.series:
                raise _bad_request(f"unknown series {key!r}")
    else:
        selected = tuple(config.series)

    orphaned = sorted(set(per_series_raw) - set(selected))
    if orphaned:
        raise _bad_request(
            f"{', '.join(orphaned)} has settings but is not in this feed -- add it to 'series'"
        )

    global_sessions = (
        _parse_sessions(global_raw["sessions"], "sessions") if "sessions" in global_raw else None
    )
    if legacy:
        excluded: set[SessionType] = set()
        if not _parse_bool(global_raw.get("practices", "true"), "practices"):
            excluded.add(SessionType.PRACTICE)
        if not _parse_bool(global_raw.get("qualifying", "true"), "qualifying"):
            excluded |= _QUALIFYING_TYPES
        if excluded:
            global_sessions = frozenset(set(SessionType) - excluded)

    global_alarms = _parse_alarms(global_raw["alarms"], "alarms") if "alarms" in global_raw else None
    global_by_type = {
        session_type: _parse_alarms(global_raw[param], param)
        for param, session_type in _ALARM_PARAMS.items()
        if param in global_raw
    }

    filters: dict[str, _Filters] = {}
    for key in selected:
        raw = per_series_raw.get(key, {})
        own_sessions = (
            _parse_sessions(raw["sessions"], f"{key}.sessions") if "sessions" in raw else None
        )
        own_alarms = _parse_alarms(raw["alarms"], f"{key}.alarms") if "alarms" in raw else None
        own_by_type = {
            session_type: _parse_alarms(raw[param], f"{key}.{param}")
            for param, session_type in _ALARM_PARAMS.items()
            if param in raw
        }
        filters[key] = _Filters(
            sessions=own_sessions if own_sessions is not None else global_sessions,
            alarms={
                session_type: _first_set(
                    own_by_type.get(session_type),
                    own_alarms,
                    global_by_type.get(session_type),
                    global_alarms,
                )
                for session_type in SessionType
            },
        )

    return Selection(
        series=selected,
        filters=filters,
        prefix=_parse_emoji(global_raw.get("emoji", "none"), "emoji"),
        calname=global_raw.get("name") or None,
        is_default=not items,
    )


def _select(events: list[PublishedEvent], filters: _Filters) -> list[PublishedEvent]:
    """Apply one series' filters: drop the session types not asked for, override alarms.

    Returns copies where alarms changed, so the events held by the live
    `Publication` -- shared by every other request -- are never touched.
    """
    selected = []
    for event in events:
        if filters.sessions is not None and event.session_type not in filters.sessions:
            continue
        alarms = filters.alarms.get(event.session_type)
        if alarms is not None and event.time_confirmed and event.session_type not in _NO_ALARM_TYPES:
            event = replace(event, alarms=list(alarms))
        selected.append(event)
    return selected


def _conditional_response(ics_bytes: bytes, request: Request, label: str) -> Response:
    """Serve the feed bytes, answering a matching If-None-Match with a 304.

    ETag over the exact bytes served is the only revalidation signal this feed
    needs. A Last-Modified derived from the events would lie whenever retention
    prunes one, since that changes the feed without touching any remaining
    event's timestamp.
    """
    etag = f'"{compute_content_hash(ics_bytes)}"'
    headers = {"Cache-Control": "public, no-cache", "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    _access_logger.info("GET /%s.ics -> 200 (%d bytes)", label, len(ics_bytes))
    return Response(content=ics_bytes, media_type="text/calendar", headers=headers)
