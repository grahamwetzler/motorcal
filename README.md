# motorcal

[motorcal](https://motorcal.wetzler.dev/) publishes one combined ICS
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

## Subscribe

Open [motorcal.wetzler.dev](https://motorcal.wetzler.dev/) and tick the series
and sessions you want. The page builds the subscription URL and previews the
next event it contains.

## The schedule

[motorcal.wetzler.dev/schedule](https://motorcal.wetzler.dev/schedule) shows the
whole season without subscribing to anything: every race weekend of every series,
day by day, one row per session, in your own time zone, each weekend saying
which round of the season it is. What is next is at the top; the weekends
already run are folded away behind a single line. Untick a series to narrow it
down. The filtering is in the page, so there are no parameters to get right.

The same data is served as JSON at `/sessions.json`, weekend by weekend, in the
order they run. It is read straight from the data directory, so it is the full
season as written rather than the retention-pruned window the feeds publish.

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
data/                 # included read-only in the image; nothing in the app writes it
  defaults.yaml       # settings no single series owns
  f1.yaml             # everything about F1: settings + events
  wec.yaml
  indycar.yaml
  imsa.yaml
Docker volume `state`
  state.yaml          # machine-owned -- see "State" below
```

The deployed image contains this directory at `/data`. `compose.yaml` gives
`state.yaml` its own Docker-managed volume.

### One event

An event is a race weekend. Whatever the whole weekend shares — its name, where
it happens, which round it is — is stored once on the event; everything that
differs per session lives on the session.

```yaml
events:
  - name: 6 Hours of Imola
    url: https://www.fiawec.com/en/race/6-hours-of-imola-2026
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

`url:` is the official event page. It is published on every session in the
weekend as the ICS `URL` property.

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

A hand edit takes effect on the next deploy: push to `main` and the rebuilt
image lands within the watchtower poll interval (see "Deploys and
auto-update" in `docs/operations.md`). Config is validated at startup, so a
bad edit fails the container instead of serving a broken feed.

## Deployment configuration

| Variable | Description |
| --- | --- |
| `UID_DOMAIN` | Domain baked into every event's stable ICS UID. Pick it once — changing it later republishes and duplicates every event for subscribers (see `docs/operations.md`, "Changing UID_DOMAIN"). |
| `PUBLIC_DOMAIN` | Optional. Host named in the builder and schedule pages' canonical/og:url tags. Defaults to `UID_DOMAIN`; set it separately only if the site is served from a different domain than its UIDs are namespaced under. |

Config is validated in CI on every push and again by `motorcal serve` at
container startup; a bad file fails the build (or the container) instead of
serving a broken feed. Check it by hand with:

```bash
docker compose exec app motorcal validate-config --config /data
```

## State

The Compose `state` volume holds the only machine-owned file,
`state.yaml`. It contains the uid_domain binding and version ledger that keeps
calendar clients from re-notifying subscribers on every rebuild. See
`docs/operations.md` for backups and what losing it costs.

## License

The MIT License applies to this project's source code and documentation. We
make no copyright claim over individual schedule facts in `data/`; they are
compiled from third-party official timetables and may remain subject to their
source terms.

## More

See `docs/operations.md` for local development (`make dev`, `make test`, ...),
backups, adding a series, changing `UID_DOMAIN`,
and diagnosing a feed that looks wrong.
