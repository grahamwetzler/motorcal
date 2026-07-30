"""Admin web UI: view/edit published events by writing overrides.yaml.

Deliberately a separate FastAPI app/port from motorcal.web.create_app so it
is never reachable through the Cloudflare-tunnel-forwarded port.
"""
from __future__ import annotations

import html
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from motorcal.config import (
    ConfigError,
    OverridesConfig,
    PatchConfig,
    SyntheticEventConfig,
    load_overrides,
)
from motorcal.store import connect, get_published_event, list_published_events

_PATCH_FIELDS = ("start", "duration", "summary", "location", "status", "note")
_FORM_FIELDS = _PATCH_FIELDS + ("date",)
_STATUS_OPTIONS = ("", "CONFIRMED", "TENTATIVE", "CANCELLED")


def _str_or_none(value: str) -> str | None:
    value = value.strip()
    return value or None


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


async def _parse_form(request: Request) -> dict[str, str]:
    body = await request.body()
    return dict(parse_qsl(body.decode(), keep_blank_values=True))


def _write_overrides_atomic(path: Path, overrides: OverridesConfig) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".overrides-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(overrides.model_dump(exclude_none=True), f, sort_keys=False, default_flow_style=False)
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def _prefill(row, overrides: OverridesConfig, root_config) -> dict:
    if row is None:
        return {k: "" for k in ("uid", "series", *_FORM_FIELDS)}

    summary = row["summary"]
    if not row["time_confirmed"]:
        summary = summary.removesuffix(root_config.unknown_time.summary_suffix)

    if row["source_id_event"]:
        existing = next((p for p in overrides.patches if p.id_event == row["source_id_event"]), None)
    else:
        existing = next((e for e in overrides.events if e.uid == row["synthetic_uid"]), None)
    note = existing.note if existing and existing.note else ""

    return {
        "uid": row["synthetic_uid"] or "",
        "series": row["series"],
        "start": row["start"] or "",
        "date": row["all_day_date"] or "",
        "duration": _format_duration(row["duration_seconds"]),
        "summary": summary,
        "location": row["location"] or "",
        "status": row["status"],
        "note": note,
    }


def _apply(overrides: OverridesConfig, row, form: dict, root_config) -> None:
    """Upsert form values into overrides (patch for a source event, synthetic
    event otherwise), mutating overrides in place. Raises ValueError/KeyError/
    ValidationError on any invalid submission -- callers must leave the
    overrides file untouched when this raises."""
    fields = {k: _str_or_none(form.get(k, "")) for k in _FORM_FIELDS}

    if row is not None and row["source_id_event"]:
        patch = PatchConfig(id_event=row["source_id_event"], **{k: fields[k] for k in _PATCH_FIELDS})
        overrides.patches = [p for p in overrides.patches if p.id_event != patch.id_event] + [patch]
        return

    uid = row["synthetic_uid"] if row is not None else form["uid"].strip()
    series = row["series"] if row is not None else form["series"].strip()
    if not uid:
        raise ValueError("uid is required for a new event")
    if row is None:
        if series not in root_config.series:
            raise ValueError(f"Unknown series {series!r}")
        if any(e.uid == uid for e in overrides.events):
            raise ValueError(f"uid {uid!r} already exists -- edit it instead")

    alarms = next((e.alarms for e in overrides.events if e.uid == uid), [])
    event = SyntheticEventConfig(uid=uid, series=series, alarms=alarms, **{k: fields[k] for k in _FORM_FIELDS})
    overrides.events = [e for e in overrides.events if e.uid != event.uid] + [event]


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
</style></head><body>
<h1>{html.escape(title)}</h1>
{body}
</body></html>"""


def _render_list(rows) -> str:
    trs = "".join(
        "<tr><td>{series}</td><td>{session}</td><td>{summary}</td><td>{start}</td>"
        "<td>{status}</td><td><a href=\"/events/edit?uid={uid}\">edit</a></td></tr>".format(
            series=html.escape(r["series"]),
            session=html.escape(r["session_type"]),
            summary=html.escape(r["summary"]),
            start=html.escape(r["start"] or r["all_day_date"] or ""),
            status=html.escape(r["status"]),
            uid=html.escape(r["uid"]),
        )
        for r in rows
    )
    body = (
        '<p><a href="/events/edit">+ add new event</a></p>'
        "<table><tr><th>Series</th><th>Session</th><th>Summary</th><th>Start</th>"
        f"<th>Status</th><th></th></tr>{trs}</table>"
    )
    return _page("Events", body)


def _render_form(published_uid: str, values: dict, error: str | None, root_config) -> str:
    is_new = not published_uid
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""

    def field(name: str, label: str) -> str:
        value = html.escape(values.get(name, "") or "")
        return f'<label>{label}<input type="text" name="{name}" value="{value}"></label>'

    uid_field = field("uid", "uid (identifier used in overrides.yaml)") if is_new else ""
    series_field = (
        field("series", "series")
        if is_new
        else f'<p>series: {html.escape(values.get("series", ""))}</p>'
    )
    status_options = "".join(
        f'<option value="{s}"{" selected" if values.get("status") == s else ""}>{s or "(unset)"}</option>'
        for s in _STATUS_OPTIONS
    )

    body = f"""{error_html}
<form method="post" action="/events/edit">
<input type="hidden" name="published_uid" value="{html.escape(published_uid)}">
{uid_field}
{series_field}
{field("start", "start (ISO 8601 UTC, e.g. 2026-04-19T13:00:00Z)")}
{field("date", "date (all-day event, YYYY-MM-DD -- leave start blank if used)")}
{field("duration", "duration (e.g. 6h or 45m)")}
{field("summary", "summary")}
{field("location", "location")}
<label>status<select name="status">{status_options}</select></label>
{field("note", "note")}
<p><button type="submit">Save</button></p>
</form>"""
    return _page("Add event" if is_new else "Edit event", body)


def create_admin_app(db_path: Path, overrides_path: Path, main_app: FastAPI) -> FastAPI:
    app = FastAPI()

    def _load_overrides() -> OverridesConfig:
        try:
            return load_overrides(overrides_path)
        except ConfigError as exc:
            raise HTTPException(500, f"overrides.yaml is currently invalid: {exc}") from exc

    def _get_row_or_404(uid: str):
        conn = connect(db_path)
        try:
            row = get_published_event(conn, uid)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(404, f"No published event {uid!r}")
        return row

    @app.get("/", response_class=HTMLResponse)
    def list_events():
        root_config = main_app.state.root_config
        conn = connect(db_path)
        try:
            rows = [r for r in list_published_events(conn) if r["series"] in root_config.series]
        finally:
            conn.close()
        rows.sort(key=lambda r: r["start"] or r["all_day_date"] or "")
        return _render_list(rows)

    @app.get("/events/edit", response_class=HTMLResponse)
    def edit_form(uid: str = ""):
        row = _get_row_or_404(uid) if uid else None
        overrides = _load_overrides()
        values = _prefill(row, overrides, main_app.state.root_config)
        return _render_form(uid, values, error=None, root_config=main_app.state.root_config)

    @app.post("/events/edit", response_class=HTMLResponse)
    async def edit_submit(request: Request):
        form = await _parse_form(request)
        published_uid = form.get("published_uid", "")
        row = _get_row_or_404(published_uid) if published_uid else None

        try:
            overrides = _load_overrides()
            _apply(overrides, row, form, main_app.state.root_config)
        except (ValidationError, ValueError, KeyError) as exc:
            rendered = _render_form(published_uid, form, error=str(exc), root_config=main_app.state.root_config)
            return HTMLResponse(rendered, status_code=400)

        _write_overrides_atomic(overrides_path, overrides)
        return RedirectResponse("/", status_code=303)

    return app
