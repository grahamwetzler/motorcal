# Plan: an `all.ics` feed

**Goal:** one feed at `/all.ics` containing every series' sessions, honouring the
same `?practices=` / `?qualifying=` filters as the per-series feeds.

Everything needed already exists: `PublishedEvent.series` carries the series key,
and `build_vevent` already prefixes each summary with the series name
(`"F1: Bahrain Grand Prix"`), so a combined feed reads fine with no model changes.

## Changes

### 1. `src/motorcal/ics.py` — one combined renderer

Extract the calendar header out of `build_calendar` (signature unchanged, so
`tests/test_ics_calendar.py` keeps passing) and add:

```python
def render_combined_bytes(config: Config, published: dict[str, list[PublishedEvent]]) -> bytes:
    """Every series in one feed; each event keeps its own series' display name."""
    vevents = [
        _to_vevent(event, config.series[event.series].name)
        for series, events in published.items()
        for event in events
        if series in config.series
    ]
    return _calendar("Motorsports", "All series", vevents).to_ical()
```

`build_calendar` sorts by UID, so cross-series ordering is deterministic for free.
UIDs are `thesportsdb-<id>@domain` / `local-<uid>@domain`, already globally unique.

### 2. `src/motorcal/cli.py` — precompute it

`_render_feeds` gains one line: `feeds["all"] = render_combined_bytes(config, published)`.
All three swap sites (startup, refresh, reload) go through that function, so they
pick it up unchanged.

### 3. `src/motorcal/web.py` — serve it

`/{series}.ics` would swallow `all.ics`, so register a `/all.ics` route **before**
it (FastAPI matches in declaration order). The two handlers share:

- `_filtered(events, practices, qualifying)` — the existing exclusion-set logic,
  lifted out of `get_calendar` verbatim.
- `_conditional(ics_bytes, request, label)` — ETag, 304, access log, response.

Unfiltered requests serve `publication.feeds["all"]`; a filtered request
re-renders from `publication.published` across all series, exactly as the
per-series route already does. 503 when the combined feed is empty, matching
per-series behaviour.

### 4. `src/motorcal/config.py` — reserve the key

Two lines in `load_config`: reject a series file named `all.yaml`, because its
feed would be permanently shadowed by the combined route.

### 5. `README.md`

Mention `/all.ics` in step 4 of the quick start and note the reserved key next to
"the filename is the series key and the feed path".

## Tests

- `tests/test_ics_render.py`: combined render contains events from two series and
  each carries its own series name prefix.
- `tests/test_web_calendar_route.py`: `/all.ics` returns 200 with all series'
  events; `?practices=false` drops practices across all of them; a series file
  literally named `all` is unreachable (covered by the config test instead).
- `tests/test_config.py`: `all.yaml` is rejected.

## Skipped

- No per-series opt-out (`include_in_all: false`) — add when a series is actually
  unwanted in the combined feed.
- No separate `X-WR-CALNAME` config knob — hardcoded "Motorsports"; make it a
  `defaults.yaml` field only if someone wants it renamed.
- No caching of filtered combinations — the per-series route already re-renders
  on every filtered request and that has been fine.
