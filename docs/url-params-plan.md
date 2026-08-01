# Plan: URL params control the feed

Let a subscriber shape their own feed from the URL: which series, which session
types, what alarms, and whether titles carry a 🏁. Also renames the combined
feed from `/all.ics` to `/motorsports.ics`.

## What already exists

`/all.ics` and `/{series}.ics` serve prebuilt bytes, and already fall back to
*filter `publication.published` → re-render → ETag over the bytes* when
`?practices=false` or `?qualifying=false` is set. The whole feature is widening
that fallback path. No caching, invalidation, or ETag work is needed: the ETag
is a hash of the bytes actually served, so every param variant self-validates.

## Query grammar

Unprefixed params are the default for every series; `<series>.<param>` overrides
one series. Split each key on the first `.` — left side a known series key means
a per-series override, otherwise the whole key is a global param name. So
`alarms_race` can never collide with a series prefix.

| Param | Example | Scope |
| --- | --- | --- |
| `series` | `series=f1,wec` | global; `/motorsports.ics` only |
| `emoji` | `emoji=true` | global |
| `name` | `name=Racing` | global (`X-WR-CALNAME`) |
| `sessions` | `sessions=race,qualifying` | global or `f1.sessions=race` |
| `alarms` | `alarms=-1d,-30m` | global or `f1.alarms=-1h` |
| `alarms_<type>` | `alarms_race=-1h` | global or `f1.alarms_race=-1h` |

`sessions` is an allow-list of `SessionType` values; the default is all of them.
`alarms=` with an empty value silences alarms.

Comma-separated values are the only list syntax. A repeated key
(`sessions=practice&sessions=race`) is a 400, not a merge — silently picking the
first or last value is how a subscriber ends up with a feed they didn't ask for.

Alarm precedence, first hit wins:

```
<series>.alarms_<type>  →  <series>.alarms  →  alarms_<type>  →  alarms
                        →  the YAML-resolved default already on the event
```

Omit them all and nothing changes — defaults by default. URL alarms obey the
same guard as YAML ones (skip unconfirmed times, `unknown`, `testing`), reusing
`PublishedEvent.time_confirmed`.

## Steps

1. **Rename the combined feed.** `COMBINED_SERIES_KEY = "motorsports"` in
   `config.py`, plus its usages in `cli.py`, `web.py`, the reserved-filename
   check and its error text, the README, and the `test_all_ics_*` tests. Safe:
   that constant is only a `feeds` dict key and a route path — it never reaches
   a UID or `state.yaml`, so no subscriber sees an event change. Side effect:
   `data/motorsports.yaml` becomes the rejected filename instead of
   `data/all.yaml`. It also matches the combined feed's existing
   `X-WR-CALNAME` of "Motorsports", so the name and the URL agree.

2. **`web.py`: `_parse_selection(query_params, config, series=None)`** returning
   a frozen `Selection`. `Selection` stays private to `web.py` — see step 3 for
   why it must not travel into `ics.py`.

   Strict, raising 400 and naming the offending value for: unknown param names,
   unknown series, unknown session types, malformed alarm offsets, per-series
   `name`/`series`/`emoji`, and any repeated key. Alarm strings validate through
   the existing `parse_alarm_offset`. Silently ignoring a typo would hand
   someone a quietly wrong feed.

   `practices` and `qualifying` survive as global-only legacy aliases, since
   those URLs are already in people's calendar apps. Rather than define how they
   interact with the new allow-list, forbid the combination: a request carrying
   both a legacy alias and any `sessions` param is a 400, and so is a prefixed
   `f1.practices=false` (which never existed). Each alias alone keeps working
   exactly as it does today. Nothing live sends both.

3. **`ics.py`: one render path, no new imports.** Collapse
   `render_calendar_bytes` and `render_combined_bytes` into a single
   `render_bytes(calname, caldesc, published, config, *, prefix="")`; keep both
   existing names as thin wrappers so `cli.py` and its tests don't move.
   `build_vevent` gains `prefix: str = ""`, applied outermost — `🏁 [Postponed]
   WEC: …`. It reaches the alarm DISPLAY text for free, since that already
   reuses `rendered_summary`.

   `render_bytes` must **not** take a `Selection`: `web.py` imports rendering
   from `ics.py`, so `ics.py` importing a type from `web.py` would cycle the
   modules. It doesn't need to. `web.py` is already building the filtered
   `list[PublishedEvent]`, so it also resolves each event's alarms there
   (`dataclasses.replace(event, alarms=[...])` — `PublishedEvent` is a mutable
   dataclass) and passes `ics.py` nothing but events and a prefix string.

4. **Routes.** Both handlers become: parse → a default selection serves the
   prebuilt bytes → otherwise filter, apply alarm overrides, render → existing
   `_conditional_response`. `/{series}.ics` is the same handler with its series
   pre-seeded.

5. **Tests.** Parser units per param and per 400 case, including a repeated key
   and a legacy-alias/`sessions` combination. Route tests for series
   selection, alarm override including the confirmed-time guard, the emoji
   prefix, a per-series override beating the global, and that a param-less
   request still returns the *exact* prebuilt bytes.

6. **README.** Replace the `?practices=false` line in quick-start step 4 with
   the table above.

## Worth knowing

Emoji and alarms are applied at **render** time, not build time, so neither
touches `compute_fingerprint` or the version ledger. Nobody's calendar
re-notifies because you added `?emoji=true`. Same reason the existing `{series}:`
title prefix is safe.

## Deliberately skipped

- A `/all.ics` → `/motorsports.ics` redirect for existing subscriptions. Two
  lines if wanted.
- Response caching. Rendering a few hundred events per request is cheap;
  revisit only if latency shows up, and work out a sound cache key then — an
  `id()`-based one is not it, since CPython reuses addresses after a
  `Publication` is collected and a new one would inherit stale entries.
- Round filtering, a date window (`from=`/`to=`), and a URL-builder page.
