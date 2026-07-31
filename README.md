# motorcal

Self-hosted per-series motorsports ICS calendar publisher. It pulls race
weekends from TheSportsDB, classifies sessions (practice/qualifying/race/etc.),
and publishes one ICS feed per series that you can subscribe to from any
calendar app.

**The data directory is the source of truth.** One YAML file per series holds
that series' settings *and* its full event list. A refresh merges TheSportsDB
into those files without clobbering anything you changed by hand. There is no
separate database, no patch layer, no override file — you edit the event.

Feeds are exposed to the internet via a Cloudflare Tunnel, so nothing needs to
be port-forwarded. They are **not** access-controlled — anyone who knows your
hostname can fetch `/f1.ics`. That's fine for public race schedules; don't put
anything else in there.

## Quick start

1. Copy the example env file and fill it in:

   ```bash
   cp .env.example .env
   ```

2. Copy the example data directory and adjust it:

   ```bash
   cp -r data.example data
   ```

   Add one file per series you want. The filename is the series key and the feed
   path: `f1.yaml` is served at `/f1.ics`.

3. Start everything:

   ```bash
   docker compose up -d
   ```

4. Subscribe at `https://<your-domain>/<series>.ics`.

5. Edit events by hand in the series YAML files under `data/`. Changes are
   picked up within ~30 seconds.

## The data directory

```
data/
  motorcal.yaml     # settings no single series owns
  f1.yaml           # everything about F1: settings + events
  wec.yaml
  indycar.yaml
  imsa.yaml
  state.yaml        # machine-owned, gitignored -- see "State" below
```

`compose.yaml` mounts `./data` into the container read-write, because the
refresh cycle writes back into it. The host directory must be writable by the
container's user (uid 1000) — e.g. `chown -R 1000:1000 data`.

### One event

```yaml
events:
  - id_event: "2421035"          # provider identity; machine-owned
    summary: 6 Hours of Imola
    start: '2026-04-19T13:00:00+00:00'
    duration: 6h
    location: Imola, Italy
    status: CONFIRMED
    note: start time from the official timetable
    round: 1
    source:                      # what TheSportsDB last said; machine-owned
      name: 6 Hours of Imola
      date: '2026-04-19'
      time: null
      venue: Imola
      country: Italy
      round: 1
      season: '2026'
```

Use `start:` for a confirmed time or `date:` for an all-day entry — exactly one.
A provider event with only `date:` is published with a "(time TBC)" suffix, since
that means the time hasn't been announced yet.

To add your own event, give it a `uid:` instead of an `id_event:` and no
`source:`. Refreshes never touch it.

```yaml
  - uid: f1-2026-preseason-test
    summary: Pre-season testing
    date: '2026-02-11'
```

### How your edits survive a refresh

Every six hours the refresh refetches and rewrites these files. It will not undo
your work: `source:` records what the provider said last time, and a field is
only overwritten when the provider **actually changed it** *and* your stored
value still matches the old provider value. Edit a field and it's yours — even
if TheSportsDB later changes its own.

Concretely: if the provider has no time yet and you fill in `start:` from the
official timetable, a later fetch that still has no time leaves your value alone.
If the provider *does* announce a time, you keep yours; delete your `start:` to
opt back into tracking upstream.

Fields the provider never owns at all — `duration`, `status`, `note`, `alarms` —
are always yours.

**Comments inside a series file do not survive a rewrite.** Use an event's
`note:` field for anything you want to keep; it's published in the calendar
description too.

## Environment variables

Set these in a `.env` file next to `compose.yaml`. Both are required —
`compose.yaml` fails fast at startup if either is unset.

| Variable | Description |
| --- | --- |
| `THESPORTSDB_API_KEY` | TheSportsDB API key. Use a real (paid or free-tier) key, not the shared public `"3"` test key, for anything other than throwaway testing. |
| `CLOUDFLARE_TUNNEL_TOKEN` | Token for a Cloudflare Tunnel (Zero Trust dashboard → Networks → Tunnels → create a tunnel → choose the Docker connector → copy the token shown, not the certificate). |

Validate config changes before restarting:

```bash
docker compose exec app motorcal validate-config --config /data
```

The running app also hot-reloads on change and keeps the previous configuration
active if validation fails.

## State

`./data/state.yaml` is the only machine-owned file. It holds the
uid_domain binding, per-scope fetch times, and the version ledger that keeps
calendar clients from re-notifying subscribers on every refresh. You never need
to read it; `cp` is a valid backup. See `docs/operations.md` for what losing it
costs.

## More

See `docs/operations.md` for backups, forcing a refresh, changing `uid_domain`,
and interpreting stale/incomplete/suspicious-empty refresh states.
