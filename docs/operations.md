# motorcal Operations Guide

## Where things live

- `data/` is the source of truth: `defaults.yaml` plus one file per series,
  each holding that series' settings and its full event list. It is mounted
  **read-only** into the container — nothing in the app writes it. You and the
  scheduled agent that reads the official timetables are the only writers.
- `state/state.yaml` is the machine-only sidecar: the uid_domain binding and the
  per-UID version ledger (`fingerprint`/`sequence`/`dtstamp`) that stops calendar
  clients re-notifying subscribers on every rebuild. It gets its own writable
  directory because it is replaced atomically (write a sibling tempfile, fsync,
  rename), which a single-file bind mount does not survive.

## Editing events by hand

Edit the series file directly. The next hot-reload (~30s) picks it up; a bad edit
is rejected and logged, and the previous configuration stays active.

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
cp state/state.yaml state/state-$(date +%F).yaml
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
where to look, a `schedule_url:`. The filename stem is the series key and the
feed path, so `indycar.yaml` is served at `/indycar.ics`. The hot-reload picks it
up without a restart.

Removing a series file stops publishing that feed. Events for a series that is
no longer configured are simply not published — nothing is deleted.

## Validating configuration without activating it

```bash
docker compose exec app motorcal validate-config --config /data
```

Schema-validates the whole directory exactly as the running app would, without
touching the running service. A nonzero exit means the files are invalid. The
hot-reload poller performs the same validation automatically and keeps the
previous bundle active on failure (see `check_and_reload_config` in
`src/motorcal/refresh.py`); running this by hand just catches mistakes earlier.

## Cutting over from the TheSportsDB release

Removing the provider changed all three things a host owns: the series files
(`source:`, `id_event:` and the old series settings are now rejected), the
`state.yaml` schema (no `snapshots:`, no `thesportsdb-*` versions), and the
mounts (`./data` read-only, `./state` writable). **Watchtower will pull the new
image on its own within 30 minutes of the release**, so pin the image first and
do the whole cutover in one pass:

```bash
# 1. Pin to what is running now, so watchtower can't roll forward mid-migration.
#    Put image: ghcr.io/grahamwetzler/motorcal:sha-<current-commit> in compose.yaml
docker compose up -d

# 2. Stop, and take the new compose.yaml, .env and data/ from the repo.
docker compose down
cp -r data data-backup && cp -r <checkout>/data .

# 3. Move the ledger across, dropping what the schema no longer accepts.
mkdir -p state && chown -R 1000:1000 state
uv run python scripts/migrate_state.py data-backup/state.yaml state/state.yaml

# 4. Unpin back to :latest and start.
docker compose up -d
```

Step 3 keeps `uid_domain` and the `local-*` version entries — the hand-added
sessions whose UIDs are unchanged — and drops the rest. The original file is left
alone; keep it until the new feeds look right. Sessions that were provider-backed
get new UIDs regardless, so subscribers see those republished once.

Skipping step 2 leaves the old series files in place and the app exits on every
start with a validation error naming the offending file; the fix is still to copy
`data/` from the repo. Skipping the `./state` mount is caught the same way, by the
first save failing — the ledger is never silently written somewhere ephemeral.

Delete `scripts/migrate_state.py` afterwards; it exists for this one cutover.

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
minutes and recreates `app` (and `cloudflared`) when a new image lands — no
manual pull or restart needed on any host running this stack.

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

- **A stale feed** means the last reload was rejected. `Config reload rejected:`
  names the file and the validation error. The previously published calendar
  keeps serving; nothing is silently emptied. Fix the file and the next poll
  (~30s) picks it up.
- **A missing session** is a data problem, not a runtime one: the app publishes
  exactly what `data/` holds. Check the series file, then
  `motorcal validate-config`.
- **An event that disappeared on its own** hit the retention window in
  `defaults.yaml` — `historical_days` (180) after it happened, or
  `cancelled_after_event_days` (90) for a `status: CANCELLED` one. It is still in
  the series file; only the feed drops it.
