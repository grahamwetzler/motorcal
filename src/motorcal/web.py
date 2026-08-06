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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote_plus

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.datastructures import QueryParams

from motorcal.config import (
    COMBINED_SERIES_KEY,
    Config,
    ConfigError,
    SessionConfig,
    parse_alarm_offset,
)
from motorcal.ics import build_title, compute_content_hash, render_combined_bytes
from motorcal.merge import resolve_duration
from motorcal.models import PublishedEvent, SessionType

_access_logger = logging.getLogger("motorcal.access")

# Session types excluded by `?qualifying=false`. Different series name their
# pole-setting session differently (WEC's hyperpole, F1's sprint qualifying),
# but they're all "qualifying" from a subscriber's point of view.
_QUALIFYING_TYPES = {
    SessionType.QUALIFYING,
    SessionType.HYPERPOLE,
    SessionType.SPRINT_QUALIFYING,
}

# An alarm needs a confirmed start to hang off, and a test day has nothing worth
# alerting about. `resolve_alarms` skips it for configured alarms; a URL override
# must not be a way around that.
_NO_ALARM_TYPES = {SessionType.TESTING}

_EMOJI_PREFIXES = {
    "none": "",
    "flag": "\N{CHEQUERED FLAG} ",
    "car": "\N{RACING CAR} ",
}
# `emoji=true`/`emoji=false` predate the choice of emoji and are kept as
# aliases for the two options they used to mean.
_EMOJI_BOOL_ALIASES = {"true": "flag", "false": "none"}

# The feed-builder page served at `/`. Read per-request rather than cached at
# import: it's a static file next to this module, and re-reading it costs
# nothing a request-scale server would notice, but it lets a dev edit it and
# reload the browser instead of restarting the process.
_INDEX_HTML_PATH = Path(__file__).parent / "index.html"
# The schedule page and the stylesheet both pages share, read the same way and
# for the same reason. The schedule page needs no substitution at all -- it
# fetches `/sessions.json` itself.
_SCHEDULE_HTML_PATH = Path(__file__).parent / "schedule.html"
_CSS_PATH = Path(__file__).parent / "motorcal.css"

# `alarms_race=-1h` and friends: one per session type.
_ALARM_PARAMS = {
    f"alarms_{session_type.value}": session_type for session_type in SessionType
}
# Settable for one series as `f1.sessions=race`, or for every series unprefixed.
_SERIES_PARAMS = {"sessions", "alarms", *_ALARM_PARAMS}
# `practices`/`qualifying` predate `sessions` and are kept for feeds already
# subscribed to in people's calendar apps.
_LEGACY_PARAMS = {"practices", "qualifying"}
_GLOBAL_ONLY_PARAMS = {"series", "emoji", "name", *_LEGACY_PARAMS}
_GLOBAL_PARAMS = _SERIES_PARAMS | _GLOBAL_ONLY_PARAMS

_BOOLEANS = {
    "true": True,
    "1": True,
    "on": True,
    "yes": True,
    "false": False,
    "0": False,
    "off": False,
    "no": False,
}

# The feed's filter URL is public input. These bounds are deliberately well
# above every supported combination of options but keep one request from tying
# up the parser or producing an oversized rendered calendar name.
_MAX_QUERY_STRING_BYTES = 8 * 1024
_MAX_QUERY_PARAMETERS = 128
_MAX_CALENDAR_NAME_LENGTH = 128
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")
_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True)
class Publication:
    """One consistent generation of what the app serves.

    Always replaced wholesale (never mutated in place) so assigning it to
    `app.state.publication` is the one atomic handoff a concurrent request can
    observe -- either the whole old generation or the whole new one.
    """

    config: Config
    feed: bytes
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
    app.state.publication = Publication(config=config, feed=b"", published={})

    @app.middleware("http")
    async def harden_request(request: Request, call_next):
        # Every route is read-only and takes all supported input in the URL.
        # Rejecting bodies avoids silently accepting an input this app never uses.
        if (
            request.headers.get("content-length") not in (None, "0")
            or "transfer-encoding" in request.headers
        ):
            return JSONResponse(
                status_code=400,
                content={"detail": "request bodies are not supported"},
                headers=_SECURITY_HEADERS,
            )

        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    @app.get("/healthz")
    def healthz(request: Request):
        _reject_query_parameters(request)
        return {"ok": True}

    # The builder page.
    @app.get("/", response_class=HTMLResponse)
    def get_index(request: Request):
        _reject_query_parameters(request)
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
            publication.config, publication.published, datetime.now(UTC)
        )
        page = (
            _INDEX_HTML_PATH
            .read_text()
            .replace("__SERIES_JSON__", _inline_json(series))
            .replace("__UPCOMING_JSON__", _inline_json(upcoming))
        )
        # The page carries real event times, so it must revalidate like the feeds
        # do rather than sit in a browser cache until the next race has been run.
        return HTMLResponse(page, headers={"Cache-Control": "public, no-cache"})

    # The full-season schedule page, and the data behind it.
    @app.get("/schedule", response_class=HTMLResponse)
    def get_schedule(request: Request):
        _reject_query_parameters(request)
        return HTMLResponse(
            _SCHEDULE_HTML_PATH.read_text(),
            headers={"Cache-Control": "public, no-cache"},
        )

    @app.get("/sessions.json")
    def get_sessions(request: Request):
        _reject_query_parameters(request)
        return JSONResponse(
            _schedule(app.state.publication.config),
            headers={"Cache-Control": "public, no-cache"},
        )

    @app.get("/motorcal.css")
    def get_stylesheet(request: Request):
        _reject_query_parameters(request)
        return Response(
            _CSS_PATH.read_text(),
            media_type="text/css",
            headers={"Cache-Control": "public, no-cache"},
        )

    @app.get(f"/{COMBINED_SERIES_KEY}.ics")
    def get_combined_calendar(request: Request):
        publication = app.state.publication

        ics_bytes = publication.feed
        if not ics_bytes:
            raise HTTPException(status_code=503, detail="no usable events")

        selection = _parse_selection(
            _validated_query_params(request), publication.config
        )
        if not selection.is_default:
            published = {
                series: _select(
                    publication.published.get(series, []), selection.filters[series]
                )
                for series in selection.series
            }
            ics_bytes = render_combined_bytes(
                publication.config,
                published,
                prefix=selection.prefix,
                calname=selection.calname,
            )

        return _conditional_response(
            ics_bytes, request, COMBINED_SERIES_KEY, series=",".join(selection.series)
        )

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
    return datetime.fromisoformat(event.all_day_date).replace(tzinfo=UTC)


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


# -------------------------------------------------------------- schedule page


def _sorts_at(session: SessionConfig) -> str:
    """Where a session sits in the running order.

    `start` is an ISO 8601 string with an offset and `date` is "YYYY-MM-DD", so
    neither sorts against the other as text. Both become an aware datetime; an
    all-day session sits at midnight UTC of its day, the same place
    `_starts_at` puts one.
    """
    if session.start is not None:
        return datetime.fromisoformat(session.start).astimezone(UTC).isoformat()
    return datetime.fromisoformat(session.date).replace(tzinfo=UTC).isoformat()


def _schedule(config: Config) -> dict[str, object]:
    """The whole season, weekend by weekend, for `/sessions.json`.

    Built from the config rather than from `Publication.published`: a race
    weekend is the unit this page shows, and publishing flattens it away --
    a `PublishedEvent` carries "{event name} {label}" as one string, with no
    round or event URL of its own, and retention has already dropped the older
    end of the season. `SeriesConfig.events` is the data directory as written,
    which is what "the full schedule" means.
    """
    # A weekend sits where its first session does. Weekends from different
    # series interleave, so they are ordered together rather than per series.
    weekends = sorted(
        (
            (min(_sorts_at(session) for session in event.sessions), key, series, event)
            for key, series in config.series.items()
            for event in series.events
        ),
        key=lambda weekend: weekend[0],
    )
    events = [
        {
            "series": key,
            "name": event.name,
            "round": event.round,
            "location": event.location,
            "url": str(event.url) if event.url is not None else None,
            "sessions": [
                {
                    "label": session.label,
                    "type": session.type.value,
                    "start": session.start,
                    "date": session.date,
                    "tbc": session.tbc,
                    "round": session.round,
                    "duration": resolve_duration(
                        session.type,
                        own_duration=session.duration,
                        series_config=series,
                        globals_=config.globals,
                    ),
                }
                for session in sorted(event.sessions, key=_sorts_at)
            ],
        }
        for _, key, series, event in weekends
    ]
    return {
        "series": [
            {"key": key, "name": series.name} for key, series in config.series.items()
        ],
        "events": events,
    }


# ------------------------------------------------------------------ query parsing


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _validated_query_params(request: Request) -> QueryParams:
    """Return bounded, valid UTF-8 query parameters before interpreting them."""
    raw_query = request.scope["query_string"]
    if len(raw_query) > _MAX_QUERY_STRING_BYTES:
        raise _bad_request(f"query string exceeds {_MAX_QUERY_STRING_BYTES} bytes")
    if any(
        byte == ord("%")
        and (
            index + 2 >= len(raw_query)
            or raw_query[index + 1] not in _HEX_DIGITS
            or raw_query[index + 2] not in _HEX_DIGITS
        )
        for index, byte in enumerate(raw_query)
    ):
        raise _bad_request("query string must use valid percent-encoding")
    try:
        unquote_plus(raw_query.decode("ascii"), encoding="utf-8", errors="strict")
    except UnicodeError:
        raise _bad_request(
            "query string must be valid UTF-8 percent-encoding"
        ) from None

    query = request.query_params
    if len(query.multi_items()) > _MAX_QUERY_PARAMETERS:
        raise _bad_request(
            f"query string accepts at most {_MAX_QUERY_PARAMETERS} parameters"
        )
    return query


def _reject_query_parameters(request: Request) -> None:
    if _validated_query_params(request):
        raise _bad_request("this endpoint does not accept query parameters")


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
    option = _EMOJI_BOOL_ALIASES.get(normalized, normalized)
    try:
        return _EMOJI_PREFIXES[option]
    except KeyError:
        valid = ", ".join(_EMOJI_PREFIXES)
        raise _bad_request(f"{key!r} must be one of: {valid} (got {value!r})") from None


def _parse_calendar_name(value: str) -> str:
    if not value.strip():
        raise _bad_request("'name' must not be empty")
    if len(value) > _MAX_CALENDAR_NAME_LENGTH:
        raise _bad_request(
            f"'name' accepts at most {_MAX_CALENDAR_NAME_LENGTH} characters"
        )
    if any(ord(character) < 32 or 127 <= ord(character) < 160 for character in value):
        raise _bad_request("'name' must not contain control characters")
    return value


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


_MAX_ALARMS = (
    10  # one VALARM per offset per event -- caps anonymous list-based amplification
)


def _parse_alarms(value: str, key: str) -> list[str]:
    """Parse an alarm override. An empty value is meaningful: silence this feed."""
    if not value.strip():
        return []
    offsets = _split(value, key)
    if len(offsets) > _MAX_ALARMS:
        raise _bad_request(
            f"{key!r} accepts at most {_MAX_ALARMS} alarms (got {len(offsets)})"
        )
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
    seen: set[str] = set()
    repeated: set[str] = set()
    for key, _ in items:
        if key in seen:
            repeated.add(key)
        seen.add(key)
    repeated = sorted(repeated)
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
        _parse_sessions(global_raw["sessions"], "sessions")
        if "sessions" in global_raw
        else None
    )
    if legacy:
        excluded: set[SessionType] = set()
        if not _parse_bool(global_raw.get("practices", "true"), "practices"):
            excluded.add(SessionType.PRACTICE)
        if not _parse_bool(global_raw.get("qualifying", "true"), "qualifying"):
            excluded |= _QUALIFYING_TYPES
        if excluded:
            global_sessions = frozenset(set(SessionType) - excluded)

    global_alarms = (
        _parse_alarms(global_raw["alarms"], "alarms")
        if "alarms" in global_raw
        else None
    )
    global_by_type = {
        session_type: _parse_alarms(global_raw[param], param)
        for param, session_type in _ALARM_PARAMS.items()
        if param in global_raw
    }

    filters: dict[str, _Filters] = {}
    for key in selected:
        raw = per_series_raw.get(key, {})
        own_sessions = (
            _parse_sessions(raw["sessions"], f"{key}.sessions")
            if "sessions" in raw
            else None
        )
        own_alarms = (
            _parse_alarms(raw["alarms"], f"{key}.alarms") if "alarms" in raw else None
        )
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
        calname=_parse_calendar_name(global_raw["name"])
        if "name" in global_raw
        else None,
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
        if (
            alarms is not None
            and event.time_confirmed
            and event.session_type not in _NO_ALARM_TYPES
        ):
            event = replace(event, alarms=list(alarms))
        selected.append(event)
    return selected


def _client_ip(request: Request) -> str:
    """Best-effort caller identity for the access log.

    Compose binds this port to 127.0.0.1, so only a local reverse proxy can
    reach it directly -- an `X-Forwarded-For` here comes from that trusted
    hop, not an arbitrary Internet client spoofing it.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "-"


def _conditional_response(
    ics_bytes: bytes, request: Request, label: str, *, series: str
) -> Response:
    """Serve the feed bytes, answering a matching If-None-Match with a 304.

    ETag over the exact bytes served is the only revalidation signal this feed
    needs. A Last-Modified derived from the events would lie whenever retention
    prunes one, since that changes the feed without touching any remaining
    event's timestamp.

    Every hit is logged, 304s included -- a subscriber's calendar app polls
    far more often than its feed content actually changes, so the 304s are
    most of what a "how many people use this" count would miss.
    """
    etag = f'"{compute_content_hash(ics_bytes)}"'
    headers = {"Cache-Control": "public, no-cache", "ETag": etag}
    client = _client_ip(request)
    user_agent = request.headers.get("user-agent", "-")

    if request.headers.get("if-none-match") == etag:
        _access_logger.info(
            "GET /%s.ics client=%r ua=%r series=%r status=304 bytes=0",
            label,
            client,
            user_agent,
            series,
        )
        return Response(status_code=304, headers=headers)

    _access_logger.info(
        "GET /%s.ics client=%r ua=%r series=%r status=200 bytes=%d",
        label,
        client,
        user_agent,
        series,
        len(ics_bytes),
    )
    return Response(content=ics_bytes, media_type="text/calendar", headers=headers)
