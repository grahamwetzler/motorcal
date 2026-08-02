# motorcal

Self-hosted motorsports ICS calendar publisher. It publishes one combined ICS
feed — every session of every race weekend, practice through race, across
every series — that you can subscribe to from any calendar app, filtered down
to just the series and sessions you want via the URL.

**The data directory is the source of truth, and the only one.** One YAML file
per series holds that series' settings *and* its full event list — one entry per
race weekend, each with the list of sessions it runs. Times come from the
official timetables, kept in step by a scheduled agent that reads them and edits
these files. Nothing in the app fetches anything or writes to `data/`, so there
is no database, no patch layer, no override file, and no merge to lose an edit
to — you edit the event.

The feed is exposed to the internet via a Cloudflare Tunnel, so nothing needs
to be port-forwarded. It is **not** access-controlled — anyone who knows your
hostname can fetch `/events.ics`. That's fine for public race schedules; don't
put anything else in there.

## Quick start

1. Copy the example env file and fill it in:

   ```bash
   cp .env.example .env
   ```

2. Copy the example data directory and adjust it:

   ```bash
   cp -r data.example data
   ```

   Add one file per series you want. The filename is the series key, used to
   select or configure it in the feed URL: `f1.yaml` is series key `f1`.
   `events` is reserved for the combined feed, so there can be no `events.yaml`.

3. Make the state directory writable by the container's user:

   ```bash
   mkdir -p state && chown -R 1000:1000 state
   ```

4. Start everything:

   ```bash
   docker compose up -d
   ```

5. Subscribe at `https://<your-domain>/events.ics` — every series in one
   calendar by default. Shape the feed from the URL — see "Feed parameters"
   below — to cut it down to just the series and sessions you want.

   Or open `https://<your-domain>/` and tick what you want: the page builds the
   URL for you and previews the next event the feed would carry.

6. Edit events by hand in the series YAML files under `data/`. Changes are
   picked up within ~30 seconds.

## Feed parameters

The feed takes query parameters, so one deployment can serve as many different
calendars as you have subscriptions. Nothing is stored — the URL *is* the
setting, and two people can subscribe to the same server and get different
feeds. The builder page at `/` writes these for you; the table below is what it
is writing.

| Parameter | Example | Applies to |
| --- | --- | --- |
| `series` | `series=f1,wec` | which series to include; leave off for all of them |
| `emoji` | `emoji=flag` | whole feed — puts an emoji in front of every title: `flag` (🏁), `car` (🏎️), or `none` (default) |
| `name` | `name=Racing` | whole feed — the calendar's display name |
| `sessions` | `sessions=race,qualifying` | whole feed, or one series: `f1.sessions=race` |
| `alarms` | `alarms=-1d,-30m` | whole feed, or one series: `f1.alarms=-1h` |
| `alarms_<type>` | `alarms_race=-1h` | whole feed, or one series: `f1.alarms_race=-1h` |

`sessions` is the list of session types to keep — one or more of `practice`,
`warmup`, `qualifying`, `hyperpole`, `sprint_qualifying`, `sprint`, `race`,
`testing`. Leave it off and you get all of them.

Alarms default to whatever the YAML says; set them here only to override that.
The most specific setting wins:

```
f1.alarms_race  →  f1.alarms  →  alarms_race  →  alarms  →  the YAML default
```

`alarms=` with an empty value silences the feed. As in the YAML, alarms are
never attached to a session whose time isn't confirmed yet, or to `testing`
sessions.

So a feed of nothing but F1 and WEC races, flagged, reminding you a day and ten
minutes ahead, with F1 races an hour ahead instead:

```
/events.ics?series=f1,wec&sessions=race&emoji=flag&alarms=-1d,-10m&f1.alarms=-1h
```

A malformed parameter — an unknown name, an unknown series, a repeated key, a
bad alarm offset — is rejected with a 400 rather than ignored, so a typo can't
quietly hand you the wrong calendar.

`?practices=false` and `?qualifying=false` still work for feeds subscribed to
before `sessions` existed. They can't be combined with `sessions`; use one or
the other.

## The data directory

```
data/                 # mounted read-only; nothing in the app writes it
  defaults.yaml       # settings no single series owns
  f1.yaml             # everything about F1: settings + events
  wec.yaml
  indycar.yaml
  imsa.yaml
state/
  state.yaml          # machine-owned, gitignored -- see "State" below
```

`compose.yaml` mounts `./data` read-only and gives `state.yaml` its own writable
directory. `./state` must be writable by the container's user (uid 1000) — e.g.
`chown -R 1000:1000 state`.

### One event

An event is a race weekend. Whatever the whole weekend shares — its name, where
it happens, which round it is — is stored once on the event; everything that
differs per session lives on the session.

```yaml
events:
  - name: 6 Hours of Imola
    location: Imola Circuit, Italy
    round: 1
    sessions:
      - uid: wec-2026-imola-qualifying
        label: Qualifying
        type: qualifying
        start: '2026-04-18T12:30:00+00:00'
      - uid: wec-2026-imola-race
        type: race
        start: '2026-04-19T13:00:00+00:00'
        duration: 6h
        note: start time from the official timetable
```

Each session is published as `{series}: {event name} {label}` — "WEC: 6 Hours of
Imola Qualifying". Drop the `label:` and you get just the event name, which is
how a race reads best.

`uid:` is the session's identity. It is what the published ICS UID is built from,
so renaming one republishes that session as a new event — subscribers keep the
old copy until it expires. It has to be unique across the whole data directory,
not just its own file. The convention in use is
`{series}-{season}-{venue}-{session}`.

`type:` is what the feed filters on, and what duration and alarm defaults are
looked up by. It is required, and one of `practice`, `warmup`, `qualifying`,
`hyperpole`, `sprint_qualifying`, `sprint`, `race`, `testing`.

An event holds every session of its weekend, including a double-header's second
race. `round:` on the event is the weekend's first championship round; a session
running for a later one says so itself:

```yaml
  - name: Snap-on Milwaukee 250
    location: Milwaukee Mile, United States
    round: 16
    sessions:
      - uid: indycar-2026-milwaukee-qualifying
        label: Qualifying
        type: qualifying
        start: '2026-08-29T15:00:00+00:00'
      - uid: indycar-2026-milwaukee-makers-and-fixers-250
        label: Makers and Fixers 250
        type: race
        start: '2026-08-29T18:30:00+00:00'
      - uid: indycar-2026-milwaukee-250
        label: Milwaukee Mile 250
        type: race
        start: '2026-08-30T17:00:00+00:00'
        round: 17                # a second race, for the next round
```

### Times that aren't announced yet

Use `start:` for a confirmed time or `date:` for an all-day entry — exactly one.
An all-day session is taken at face value, which is what a test day wants:

```yaml
  - name: Pre-season testing
    location: Bahrain International Circuit
    sessions:
      - uid: f1-2026-preseason-test
        type: testing
        date: '2026-02-11'
```

When the official timetable has published the day but not the time, say so with
`tbc: true` and the session is titled "... (time TBC)" and gets no alarms:

```yaml
      - uid: imsa-2026-petit-le-mans-qualifying
        label: Qualifying
        type: qualifying
        date: '2026-10-09'
        tbc: true
```

### Nothing overwrites your edits

Nothing in the app writes these files, so there is no merge to lose an edit to
and **comments survive**. Times are kept in step with the official timetables by
a scheduled agent that reads them and edits the files directly; `schedule_url:`
on each series is where it looks.

A hand edit is picked up by the hot-reload within ~30 seconds. A bad one is
rejected and logged, and the previous configuration stays active.

## Environment variables

Set these in a `.env` file next to `compose.yaml`. Both are required —
`compose.yaml` fails fast at startup if either is unset.

| Variable | Description |
| --- | --- |
| `CLOUDFLARE_TUNNEL_TOKEN` | Token for a Cloudflare Tunnel (Zero Trust dashboard → Networks → Tunnels → create a tunnel → choose the Docker connector → copy the token shown, not the certificate). |
| `UID_DOMAIN` | Domain baked into every event's stable ICS UID. Pick it once — changing it later republishes and duplicates every event for subscribers (see `docs/operations.md`, "Changing UID_DOMAIN"). |

Validate config changes before restarting:

```bash
docker compose exec app motorcal validate-config --config /data
```

The running app also hot-reloads on change and keeps the previous configuration
active if validation fails.

## State

`./state/state.yaml` is the only machine-owned file. It holds the uid_domain
binding and the version ledger that keeps calendar clients from re-notifying
subscribers on every rebuild. You never need to read it; `cp` is a valid backup.
See `docs/operations.md` for what losing it costs.

## More

See `docs/operations.md` for backups, adding a series, changing `UID_DOMAIN`,
and diagnosing a feed that looks wrong.
