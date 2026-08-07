# motorcal Operations Guide

## Where things live

- `data/` is the source of truth: `defaults.yaml` plus one file per series,
  each holding that series' settings and its full event list. It is included
  **read-only** in the image — nothing in the app writes it. You and the
  scheduled agent that reads the official timetables are the only writers.
- The Compose `state` volume holds `/state/state.yaml`, the machine-only
  sidecar: the uid_domain binding and per-UID version ledger
  (`fingerprint`/`sequence`/`dtstamp`) that stops calendar clients re-notifying
  subscribers on every rebuild. It is replaced atomically (write a sibling
  tempfile, fsync, rename).

## Local development

`make dev` runs the app at `:8000` against `data/` and a local
`state/state.yaml` (gitignored), reading `UID_DOMAIN`/`PUBLIC_DOMAIN` from
`.env`. Other targets: `make test`, `make lint`, `make fmt`, `make validate`,
`make schedule` (prints the local feed's events; `URL=...` to point it
elsewhere), and `make check` (everything CI runs). See the `Makefile` for the
full list.

## Editing events by hand

Edit the series file directly and push to `main`. CI validates the directory,
CD rebuilds and pushes the image, and watchtower recreates `app` with it (see
"Deploys and auto-update" below). A bad edit fails `motorcal validate-config`
in CI before it ever reaches a running container.

Every field is yours — there is no provider to merge against and nothing
overwrites what you wrote. **Comments survive**, so annotate the file freely; a
session's `note:` is still the right place for anything a subscriber should see,
since it is published in the calendar description.

A session needs a `uid:` (its identity, and what the ICS UID is built from), a
`type:`, and exactly one of `start:` (a confirmed time) or `date:` (all-day). Set
`tbc: true` alongside `date:` when the timetable has the day but not the time —
that is what puts "(time TBC)" in the title. An all-day session without `tbc:` is
treated as deliberately all-day, which is how a test day should read.

**Renaming a `uid:` republishes that session as a new event.** Subscribers keep
the old copy until it expires. Rename only when you mean to.

## Backups

```bash
cp -r data data-$(date +%F)
docker compose cp app:/state/state.yaml state-$(date +%F).yaml
```

Restoring `data/` restores your events. Restoring an older `state.yaml` rolls
`sequence` numbers back, and a calendar client that already saw a *higher*
`SEQUENCE` for a UID may ignore the restored update — if that matters, hand-edit
the `versions:` block and raise every `sequence` above the highest value clients
could have seen (they're UTC Unix minutes).

Losing `state.yaml` entirely is recoverable: the next rebuild recreates it from
`data/`, and every UID stays the same, but every event gets a fresh `dtstamp`
so subscribers see the whole calendar as modified once.

## Adding a series

Drop a new `data/<key>.yaml` in place with a `name:` and, so the agent knows
where to look, a `schedule_url:`. The filename stem is the series key, selectable
in the combined feed's URL as `?series=indycar`. It goes live on the next deploy.

Removing a series file stops publishing that series' events. Events for a
series that is no longer configured are simply not published — nothing is
deleted.

## Validating configuration without activating it

```bash
docker compose exec app motorcal validate-config --config /data
```

Schema-validates the whole directory exactly as the running app would, without
touching the running service. A nonzero exit means the files are invalid. CI
runs the same check on every push (`validate-config` in
`.github/workflows/ci.yml`); running this by hand just catches mistakes earlier.

## Changing `UID_DOMAIN`

`UID_DOMAIN` (the environment variable, not a file -- see `.env`) is baked into
every event's stable ICS `UID`, so changing it would republish the entire
calendar under fresh UIDs and duplicate every event in subscribers' clients.
On startup, `motorcal serve` compares it against the `uid_domain:` recorded in
`state.yaml` and refuses to start if they differ.

To actually change it, accept that subscribers must re-add the calendar URL,
then edit `state.yaml`'s `uid_domain:` and clear its `versions:` block. Every
still-retained event will appear twice in existing subscribers' calendars until
the old copies expire.

## Deploys and auto-update

Pushing to `main` runs tests and config validation, then builds and pushes
`ghcr.io/grahamwetzler/motorcal:latest` (and a `sha-<commit>` tag) via GitHub
Actions. A `watchtower` container in `compose.yaml` polls GHCR every 30
minutes and recreates `app` when a new image lands — no manual pull or restart
needed on any host running this stack.

One-time setup after the very first push: the GHCR package is created
private by default even though the repo is public. Open the package's
GitHub settings and set visibility to Public, or `docker compose pull` will
fail with 403s.

To move this to a new machine: copy `compose.yaml`, `.env`, `data/` and
`state/` over and run `docker compose up -d`. No git checkout or build step
required — everything pulls from GHCR.

To roll back a bad release, pin the image to a known-good commit instead of
`latest`:

```yaml
image: ghcr.io/grahamwetzler/motorcal:sha-<previous-good-commit>
```

then `docker compose up -d`. Watchtower never touches a `sha-*` tag, so it
stays pinned until you switch back to `:latest`.

## Diagnosing a feed that looks wrong

There's no status endpoint; diagnose from container logs.

- **A stale feed** means the last deploy never landed. Check the Actions run for
  the push and `docker compose logs watchtower` on the host.
- **A missing session** is a data problem, not a runtime one: the app publishes
  exactly what `data/` holds. Check the series file, then
  `motorcal validate-config`.
- **An event that disappeared on its own** hit the retention window in
  `defaults.yaml` — `historical_days` (180) after it happened, or
  `cancelled_after_event_days` (90) for a `status: CANCELLED` one. It is still in
  the series file; only the feed drops it.
