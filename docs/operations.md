# motorcal Operations Guide

## Where things live

- `config/` is the source of truth: `motorcal.yaml` plus one file per series,
  each holding that series' settings and its full event list. Both you and the
  refresh cycle write here.
- `motorcal-data/state.yaml` is the machine-only sidecar: the uid_domain
  binding, per-scope fetch times, and the per-UID version ledger
  (`fingerprint`/`sequence`/`dtstamp`) that stops calendar clients re-notifying
  subscribers on every refresh.

Both are written atomically (full write + fsync, then a single rename), so
copying either from a running system is safe.

## Editing events by hand

Edit the series file directly. The next hot-reload (~30s) picks it up; a bad edit
is rejected and logged, and the previous configuration stays active.

A field you change is yours. The refresh compares each incoming provider value
against the `source:` block — its record of what the provider said last time —
and only overwrites a field when the provider actually changed it *and* your
value still matches the old provider value. To hand a field back to upstream
tracking, delete your value and let the next refresh repopulate it.

`duration`, `status`, `note`, and `alarms` are never provider-owned.

**Comments do not survive a refresh** — the file is rewritten from parsed data.
Put anything you want to keep in an event's `note:`, which also shows up in the
calendar description.

## Backups

```bash
cp -r config config-$(date +%F)
cp motorcal-data/state.yaml motorcal-data/state-$(date +%F).yaml
```

Restoring `config/` restores your events. Restoring an older `state.yaml` rolls
`sequence` numbers back, and a calendar client that already saw a *higher*
`SEQUENCE` for a UID may ignore the restored update — if that matters, hand-edit
the `versions:` block and raise every `sequence` above the highest value clients
could have seen (they're UTC Unix minutes).

Losing `state.yaml` entirely is recoverable: the next refresh rebuilds it from
`config/`, and every UID stays the same, but every event gets a fresh `dtstamp`
so subscribers see the whole calendar as modified once.

## Adding a series

Drop a new `config/<key>.yaml` in place with `league_id`, `name`, and
`max_round`. The filename stem is the series key and the feed path, so
`indycar.yaml` is served at `/indycar.ics`. The hot-reload picks it up without a
restart; the first scheduled refresh fills in its events.

Removing a series file stops publishing that feed. Events for a series that is
no longer configured are simply not published — nothing is deleted.

## Forcing an immediate refresh

There is no "refresh now" command, and restarting doesn't force one. Temporarily
set `source.refresh_cron` in `config/motorcal.yaml` to `"* * * * *"`, wait for
the reload (~30s) and the cycle to run, then restore the original expression. No
restart needed for either edit.

## Validating configuration without activating it

```bash
docker compose exec app motorcal validate-config --config /config
```

Schema-validates the whole directory exactly as the running app would, without
touching the running service. A nonzero exit means the files are invalid. The
hot-reload poller performs the same validation automatically and keeps the
previous bundle active on failure (see `check_and_reload_config` in
`src/motorcal/refresh.py`); running this by hand just catches mistakes earlier.

## Changing `uid_domain`

`uid_domain` is baked into every event's stable ICS `UID`, so changing it would
republish the entire calendar under fresh UIDs and duplicate every event in
subscribers' clients. Enforced twice:

- The hot-reload poller rejects any reload that changes it and logs a warning.
- On startup, `motorcal serve` compares `motorcal.yaml` against the `uid_domain:`
  recorded in `state.yaml` and refuses to start if they differ.

To actually change it, accept that subscribers must re-add the calendar URL, then
edit `state.yaml`'s `uid_domain:` and clear its `versions:` block. Every
still-retained event will appear twice in existing subscribers' calendars until
the old copies expire.

## Unclassified events

Container logs carry an `Unclassified events:` warning listing UIDs whose
session name didn't match any of that series' classification rules
(`src/motorcal/classify.py`). They are still published — just without an
inferred alarm or duration. An entry here usually means TheSportsDB introduced
a new session-name format.

Two fixes, depending on scope. For a one-off, set that event's `duration:` and
`alarms:` directly in the series file. For a naming pattern that will recur,
extend the series' rule list in `classify.py` — classification rules are code,
not configuration.

## Interpreting stale, incomplete, and suspicious-empty refreshes

There's no status endpoint; diagnose freshness from container logs.

- **Stale**: repeated refresh failures — network issues, TheSportsDB
  rate-limiting or outage — show up as `Provider scan:` and
  `Config reload rejected:` warnings. The previously published calendar keeps
  serving; nothing is silently emptied.
- **Never refreshed / incomplete / suspicious-empty**: every attempt for that
  series has been *incomplete* (a round request failed) or *suspicious-empty*
  (a complete scan returned zero events for a scope that previously had data,
  or for the current season at all), logged as `Refresh published nothing:`
  or `Refresh skipped:`. Both are discarded in full by design — see
  `sync_snapshot` in `src/motorcal/sync.py` — rather than overwriting good
  data with a partial or suspicious result. The next tick tries again.
- **Incomplete snapshot**: not its own status field, but it looks like "never
  refreshed" persisting longer than expected. Across many consecutive ticks,
  suspect something systemic: the round-scan `deadline_seconds`, the token-bucket
  rate limit, or an API key nearing its quota.
- **Suspicious-empty for a *future* season** (the season fetched from
  `next_season_from` onward) is expected and harmless if that calendar hasn't
  been announced yet. It's only rejected once that scope previously had data and
  then reported zero.
