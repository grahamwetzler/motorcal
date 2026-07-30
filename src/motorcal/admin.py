"""Admin app (port 8001): the operator UI plus /status.

Deliberately a separate FastAPI app/port from motorcal.web.create_app so it is
never reachable through the Cloudflare-tunnel-forwarded port -- the tunnel only
ever forwards 8000.

Edits write straight into the series file. There is no patch or override layer:
the file is the event, so saving a form is a field assignment plus a rewrite.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from motorcal.config import EventConfig, load_config, save_series
from motorcal.models import PublishedEvent
from motorcal.state import scope_key

_EDITABLE = ("summary", "start", "date", "duration", "location", "status", "note")
_STATUS_OPTIONS = ("CONFIRMED", "TENTATIVE", "CANCELLED")

STALE_AFTER_HOURS = 12


def _str_or_none(value: str) -> str | None:
    return value.strip() or None


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return f"{seconds // 3600}h" if seconds % 3600 == 0 else f"{seconds // 60}m"


async def _parse_form(request: Request) -> dict[str, str]:
    body = await request.body()
    return dict(parse_qsl(body.decode(), keep_blank_values=True))


def _prefill(event: EventConfig | None) -> dict:
    if event is None:
        return {k: "" for k in ("uid", "series", *_EDITABLE)}
    return {
        "uid": event.uid or "",
        "series": "",
        "summary": event.summary,
        "start": event.start or "",
        "date": event.date or "",
        "duration": event.duration or "",
        "location": event.location or "",
        "status": event.status,
        "note": event.note or "",
    }


def _apply(event: EventConfig | None, form: dict) -> EventConfig:
    """Build the updated event from a submitted form.

    Returns a new validated EventConfig rather than mutating in place, so an
    invalid submission raises before anything is written. `id_event` and `source`
    are carried across untouched -- they are the provider's, not the form's.
    """
    fields = {k: _str_or_none(form.get(k, "")) for k in _EDITABLE}
    if not fields["summary"]:
        raise ValueError("summary is required")

    base = {
        "id_event": event.id_event if event else None,
        "uid": event.uid if event else _str_or_none(form.get("uid", "")),
        "alarms": event.alarms if event else None,
        "round": event.round if event else None,
        "disappeared_at": event.disappeared_at if event else None,
        "source": event.source if event else None,
    }
    if not base["id_event"] and not base["uid"]:
        raise ValueError("uid is required for a new event")

    return EventConfig(
        **base,
        summary=fields["summary"],
        start=fields["start"],
        date=fields["date"],
        duration=fields["duration"],
        location=fields["location"],
        status=fields["status"] or "CONFIRMED",
        note=fields["note"],
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; }}
form label {{ display: block; margin-top: 0.6rem; }}
input, select {{ width: 100%; max-width: 24rem; padding: 0.3rem; }}
.error {{ color: #b00020; }}
.local {{ color: #555; font-style: italic; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
{body}
</body></html>"""


def _render_list(events: list[PublishedEvent]) -> str:
    trs = "".join(
        "<tr><td>{series}</td><td>{session}</td><td>{summary}</td><td>{start}</td>"
        "<td>{status}</td><td><a href=\"/events/edit?uid={uid}\">edit</a></td></tr>".format(
            series=html.escape(e.series),
            session=html.escape(e.session_type.value),
            summary=html.escape(e.summary),
            start=html.escape(e.start.isoformat() if e.start else e.all_day_date or ""),
            status=html.escape(e.status.value),
            uid=html.escape(e.uid),
        )
        for e in events
    )
    body = (
        '<p><a href="/events/edit">+ add new event</a></p>'
        "<table><tr><th>Series</th><th>Session</th><th>Summary</th><th>Start</th>"
        f"<th>Status</th><th></th></tr>{trs}</table>"
    )
    return _page("Events", body)


def _render_form(published_uid: str, values: dict, error: str | None, series_keys) -> str:
    is_new = not published_uid
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""

    def field(name: str, label: str) -> str:
        value = html.escape(values.get(name, "") or "")
        return f'<label>{label}<input type="text" name="{name}" value="{value}"></label>'

    if is_new:
        options = "".join(f'<option value="{html.escape(s)}">{html.escape(s)}</option>'
                          for s in series_keys)
        identity = (
            field("uid", "uid (your identifier for this event)")
            + f'<label>series<select name="series">{options}</select></label>'
        )
    else:
        identity = ""

    status_options = "".join(
        f'<option value="{s}"{" selected" if values.get("status") == s else ""}>{s}</option>'
        for s in _STATUS_OPTIONS
    )

    body = f"""{error_html}
<form method="post" action="/events/edit">
<input type="hidden" name="published_uid" value="{html.escape(published_uid)}">
{identity}
{field("summary", "summary")}
{field("start", "start (ISO 8601 UTC, e.g. 2026-04-19T13:00:00Z)")}
{field("date", "date (all-day event, YYYY-MM-DD -- leave start blank if used)")}
{field("duration", "duration (e.g. 6h or 45m)")}
{field("location", "location")}
<label>status<select name="status">{status_options}</select></label>
{field("note", "note")}
<p><button type="submit">Save</button></p>
</form>
<p class="local">Edits are written straight into the series file. A field you change
here is yours: the next refresh will not overwrite it, even if TheSportsDB changes
its own value.</p>"""
    return _page("Add event" if is_new else "Edit event", body)


def create_admin_app(config_dir: Path, main_app: FastAPI) -> FastAPI:
    app = FastAPI()

    def _all_published() -> list[PublishedEvent]:
        return [e for events in main_app.state.published.values() for e in events]

    def _locate(published_uid: str) -> tuple[str, EventConfig]:
        """Find the series and configured event behind a published UID."""
        built = next((e for e in _all_published() if e.uid == published_uid), None)
        if built is None:
            raise HTTPException(404, f"No published event {published_uid!r}")
        series_config = main_app.state.config.series[built.series]
        event = next((e for e in series_config.events if e.key == built.event_key), None)
        if event is None:
            raise HTTPException(404, f"No configured event behind {published_uid!r}")
        return built.series, event

    @app.get("/status")
    def status():
        """Freshness and error report. Always HTTP 200.

        This doubles as the container healthcheck, so it reports liveness by
        answering at all: an upstream provider outage makes `healthy` false in the
        body, but must not fail the healthcheck and restart-loop a process that is
        serving its last-known-good feeds perfectly well.
        """
        config = main_app.state.config
        state = main_app.state.data
        now = datetime.now(timezone.utc)
        season = str(now.year)

        series_status = {}
        for series in config.series:
            snapshot = state.snapshots.get(scope_key(series, season))
            if snapshot is None:
                stale, last_complete_at = True, None
            else:
                last_complete_at = snapshot.last_complete_at
                age = now - datetime.fromisoformat(last_complete_at)
                stale = age.total_seconds() / 3600 > STALE_AFTER_HOURS
            series_status[series] = {
                "events": len(main_app.state.published.get(series, [])),
                "stale": stale,
                "last_complete_at": last_complete_at,
            }

        return {
            "ready": all(s["events"] > 0 for s in series_status.values()),
            "healthy": all(not s["stale"] for s in series_status.values()),
            "series": series_status,
            "unknown_events": main_app.state.diagnostics.get("unknown_events", []),
        }

    @app.get("/", response_class=HTMLResponse)
    def list_events():
        events = sorted(
            _all_published(),
            key=lambda e: e.start.isoformat() if e.start else e.all_day_date or "",
        )
        return _render_list(events)

    @app.get("/events/edit", response_class=HTMLResponse)
    def edit_form(uid: str = ""):
        event = _locate(uid)[1] if uid else None
        return _render_form(uid, _prefill(event), None, list(main_app.state.config.series))

    @app.post("/events/edit", response_class=HTMLResponse)
    async def edit_submit(request: Request):
        form = await _parse_form(request)
        published_uid = form.get("published_uid", "")
        series_keys = list(main_app.state.config.series)

        if published_uid:
            series, existing = _locate(published_uid)
        else:
            series, existing = form.get("series", "").strip(), None
            if series not in series_keys:
                return HTMLResponse(
                    _render_form(published_uid, form, f"Unknown series {series!r}", series_keys),
                    status_code=400,
                )

        try:
            updated = _apply(existing, form)
        except (ValidationError, ValueError, KeyError) as exc:
            return HTMLResponse(
                _render_form(published_uid, form, str(exc), series_keys), status_code=400
            )

        # Re-read from disk rather than serialising the in-memory copy: a refresh
        # cycle may have written new events since this page was rendered, and they
        # must not be dropped by this save.
        try:
            on_disk = load_config(config_dir).series[series]
        except Exception as exc:  # noqa: BLE001 -- surface it in the form, don't 500
            return HTMLResponse(
                _render_form(published_uid, form, str(exc), series_keys), status_code=400
            )

        if existing is None and any(e.key == updated.key for e in on_disk.events):
            return HTMLResponse(
                _render_form(
                    published_uid, form, f"uid {updated.key!r} already exists -- edit it instead",
                    series_keys,
                ),
                status_code=400,
            )

        on_disk.events = [e for e in on_disk.events if e.key != updated.key] + [updated]
        on_disk.events.sort(key=lambda e: (e.start or e.date or "", e.key))
        save_series(config_dir, series, on_disk)
        return RedirectResponse("/", status_code=303)

    return app
