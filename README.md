# motorcal

Self-hosted per-series motorsports ICS calendar publisher. It pulls race
weekends from TheSportsDB, classifies sessions (practice/qualifying/race/etc.),
and publishes one ICS feed per series that you can subscribe to from any
calendar app.

**The data directory is the source of truth.** One YAML file per series holds
that series' settings *and* its full event list — one entry per race weekend,
each with the list of sessions it runs. A refresh merges TheSportsDB into those
files without clobbering anything you changed by hand. There is no separate
database, no patch layer, no override file — you edit the event.

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
   path: `f1.yaml` is served at `/f1.ics`. `motorsports` is reserved for the
   combined feed, so there can be no `motorsports.yaml`.

3. Start everything:

   ```bash
   docker compose up -d
   ```

4. Subscribe at `https://<your-domain>/<series>.ics`, or at
   `https://<your-domain>/motorsports.ics` for every series in one calendar.
   Shape the feed from the URL — see "Feed parameters" below.

5. Edit events by hand in the series YAML files under `data/`. Changes are
   picked up within ~30 seconds.

## Feed parameters

Both feeds take query parameters, so one deployment can serve as many different
calendars as you have subscriptions. Nothing is stored — the URL *is* the
setting, and two people can subscribe to the same server and get different
feeds.

| Parameter | Example | Applies to |
| --- | --- | --- |
| `series` | `series=f1,wec` | whole feed; `/motorsports.ics` only |
| `emoji` | `emoji=true` | whole feed — puts 🏁 in front of every title |
| `name` | `name=Racing` | whole feed — the calendar's display name |
| `sessions` | `sessions=race,qualifying` | whole feed, or one series: `f1.sessions=race` |
| `alarms` | `alarms=-1d,-30m` | whole feed, or one series: `f1.alarms=-1h` |
| `alarms_<type>` | `alarms_race=-1h` | whole feed, or one series: `f1.alarms_race=-1h` |

`sessions` is the list of session types to keep — one or more of `practice`,
`qualifying`, `hyperpole`, `sprint_qualifying`, `sprint`, `race`, `testing`.
Leave it off and you get all of them.

Alarms default to whatever the YAML says; set them here only to override that.
The most specific setting wins:

```
f1.alarms_race  →  f1.alarms  →  alarms_race  →  alarms  →  the YAML default
```

`alarms=` with an empty value silences the feed. As in the YAML, alarms are
never attached to a session whose time isn't confirmed yet, or to `testing` and
`unknown` sessions.

So a feed of nothing but F1 and WEC races, flagged, reminding you a day and ten
minutes ahead, with F1 races an hour ahead instead:

```
/motorsports.ics?series=f1,wec&sessions=race&emoji=true&alarms=-1d,-10m&f1.alarms=-1h
```

A malformed parameter — an unknown name, an unknown series, a repeated key, a
bad alarm offset — is rejected with a 400 rather than ignored, so a typo can't
quietly hand you the wrong calendar.

`?practices=false` and `?qualifying=false` still work for feeds subscribed to
before `sessions` existed. They can't be combined with `sessions`; use one or
the other.

## The data directory

```
data/
  defaults.yaml     # settings no single series owns
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

An event is a race weekend. Whatever the whole weekend shares — its name, where
it happens, which round it is — is stored once on the event; everything that
differs per session lives on the session.

```yaml
events:
  - name: 6 Hours of Imola
    location: Imola, Italy
    round: 1
    sessions:
      - id_event: "2467176"        # provider identity; machine-owned
        label: Qualifying
        type: qualifying
        start: '2026-04-18T12:30:00+00:00'
        source:                    # what TheSportsDB last said; machine-owned
          name: 6 Hours of Imola Qualifying
          date: '2026-04-18'
          time: '12:30:00'
          venue: Imola Circuit
          country: Italy
          round: 1
          season: '2026'
      - id_event: "2421035"
        label: Race
        type: race
        start: '2026-04-19T13:00:00+00:00'
        duration: 6h
        status: CONFIRMED
        note: start time from the official timetable
        source:
          name: 6 Hours of Imola
          date: '2026-04-19'
          time: null
          venue: Imola Circuit
          country: Italy
          round: 1
          season: '2026'
```

Each session is published as `{series}: {event name} {label}` — "WEC: 6 Hours of
Imola Qualifying". Drop the `label:` and you get just the event name, which is
how a one-session weekend with nothing to distinguish reads best.

`type:` is what the feed filters on (`?practices=false`), and what duration and
alarm defaults are looked up by. It is guessed from the label when the session
first appears; correct it by hand and your value stands.

An event holds every session of its weekend, including a double-header's second
race. `round:` on the event is the weekend's first championship round; a session
running for a later one says so itself:

```yaml
  - name: Snap-on
    location: Milwaukee Mile, United States
    round: 16
    sessions:
      - uid: indycar-2026-milwaukee-qualifying
        label: INDYCAR Weekend Qualifying
        type: qualifying
        start: '2026-08-29T15:00:00+00:00'
      - id_event: "2402411"
        label: Makers and Fixers 250
        type: race
        start: '2026-08-29T18:30:00+00:00'
      - id_event: "2402412"
        label: Milwaukee Mile 250
        type: race
        start: '2026-08-30T17:00:00+00:00'
        round: 17                # a second race, for the next round
```

Use `start:` for a confirmed time or `date:` for an all-day entry — exactly one.
A provider session with only `date:` is published with a "(time TBC)" suffix,
since that means the time hasn't been announced yet.

To add your own session, give it a `uid:` instead of an `id_event:` and no
`source:`. Refreshes never touch it. Put it in the weekend it belongs to, or in
an event of its own:

```yaml
  - name: Pre-season testing
    location: Bahrain International Circuit
    sessions:
      - uid: f1-2026-preseason-test
        type: testing
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
opt back into tracking upstream. The same holds for an event's `name:` and
`location:`, whose baseline is what the provider called that whole weekend.

Fields the provider never owns at all — `duration`, `status`, `note`, `alarms` —
are always yours.

New sessions join the weekend that already holds the round they belong to, so a
qualifying session you added by hand keeps sitting next to the race when the
provider finally publishes one of its own.

**Comments inside a series file do not survive a rewrite.** Use an event's
`note:` field for anything you want to keep; it's published in the calendar
description too.

## Environment variables

Set these in a `.env` file next to `compose.yaml`. All three are required —
`compose.yaml` fails fast at startup if any is unset.

| Variable | Description |
| --- | --- |
| `THESPORTSDB_API_KEY` | TheSportsDB API key. Use a real (paid or free-tier) key, not the shared public `"3"` test key, for anything other than throwaway testing. |
| `CLOUDFLARE_TUNNEL_TOKEN` | Token for a Cloudflare Tunnel (Zero Trust dashboard → Networks → Tunnels → create a tunnel → choose the Docker connector → copy the token shown, not the certificate). |
| `UID_DOMAIN` | Domain baked into every event's stable ICS UID. Pick it once — changing it later republishes and duplicates every event for subscribers (see `docs/operations.md`, "Changing UID_DOMAIN"). |

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

See `docs/operations.md` for backups, forcing a refresh, changing `UID_DOMAIN`,
and interpreting stale/incomplete/suspicious-empty refresh states.
