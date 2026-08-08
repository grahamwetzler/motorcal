# Schedule changelog design

## Goal

When a session moves, a weekend shifts, or a season's calendar is published,
record **what changed and why** in two places at once: the git history (because
the record lands in the same commit as the schedule edit) and a hidden-by-default
`<details>` on `/schedule` (because a subscriber wondering why their calendar
moved should not have to read a repo).

Scoped at three levels — season, event, session — so every kind of change has a
place to live.

## Why not changie

Changie batches fragment files into a versioned `CHANGELOG.md` on release.
Motorcal has no releases: every merge to `main` deploys. The entries also have to
be machine-readable and series-scoped so the schedule page can render and filter
them, so adopting changie would mean adding a Go binary to CI *and* writing a
parser for its markdown output. The whole schema below is one pydantic model plus
a three-line subclass.

## Why not git history at runtime

The image copies `data/` and nothing else — there is no `.git` in the container
(`Dockerfile:18`). Deriving the changelog from `git log` at runtime is impossible,
and doing it at build time means a generation step whose output can disagree with
the data it describes. An entry that lives *in* `data/` cannot drift from it.

Git still does the job it is good at: `git log -p data/wec.yaml` shows the entry
and the edit it explains in one diff, because they are the same commit.

## Season needs no data format change

A season is already `(series, calendar year)`, derived rather than stored:
`schedule.html:153-161` computes `seasonOf(event)` from the year of the weekend's
first session and keys `ROUNDS`/`FIRST_ROUNDS` by `series:year`. Its self-check at
`schedule.html:531` covers exactly the case that makes this load-bearing — WEC's
2026 and 2027 calendars in one file, both numbered from round 1, totals kept
apart.

`data/wec.yaml:379` is already a season-level changelog entry written by hand:

```yaml
# 2027 calendar announced 2026-06-12: https://www.fiawec.com/en/news/...
```

This design formalizes that comment. It does not restructure anything.

### Rejected: nesting the data as series → seasons → events → sessions

It would rewrite all four data files, `SeriesConfig.iter_sessions`,
`web._schedule()`, the page's grouping, and every test that touches them — in
order to store a number that is already derivable from the first session, and
that the page derives correctly today. The only thing that wanted the extra level
was addressing a changelog entry, and `season: 2027` addresses it in one field.

## Schema: placement, not pointers

Entries live on the object they describe.

```yaml
# data/wec.yaml

# season level — on the series, scoped by year
changes:
  - season: 2027
    date: '2026-06-12'
    text: >-
      Nine-round 2027 calendar announced; every date is day-only until the
      timetables land.

events:
  - name: 6 Hours of Imola
    # event level
    changes:
      - date: '2026-08-07'
        text: Moved a week later to clear the Giro; FIA published a revised calendar.
    sessions:
      - uid: wec-2027-6-hours-of-imola-qualifying
        # session level
        changes:
          - date: '2026-08-07'
            text: Now 13:00 (was 14:00), following the revised support programme.
```

No uid references, no cross-file lookups, and **nothing can dangle** — delete a
session and its history goes with it.

That property is also what forces all three levels to exist. "Practice 3 was
dropped from the timetable" has nowhere to live once the session is gone, so a
removal is an *event*-level entry; a dropped weekend is a *season*-level one. The
level is not decoration, it is where the entry survives.

## Loader (`src/motorcal/config.py`)

```python
class ChangeEntry(StrictModel):
    date: str  # ISO date the change was made
    text: str  # non-empty: what changed and why


class SeasonChangeEntry(ChangeEntry):
    season: int
```

- `changes: list[ChangeEntry] = []` on `SessionConfig` and `EventConfig`
- `changes: list[SeasonChangeEntry] = []` on `SeriesConfig`

`text` must be non-empty after strip, the same shape of guard as
`SessionConfig.validate_uid`.

`date` shares `SessionConfig.validate_date`, but that validator needs tightening
first: `date.fromisoformat` also accepts `20260807` and `2026-W32-5`, and the
validator keeps whatever string it was given. Require an exact round trip —
`date.fromisoformat(value).isoformat() == value` — so only `YYYY-MM-DD` survives.

Two reasons this is not optional for the changelog: `_schedule()` sorts these
values as raw strings, where a basic or ISO-week form sorts into the wrong place;
and the page builds its dateline by concatenation, where either form yields an
invalid JavaScript `Date`.

**This closes a pre-existing hole rather than only guarding the new field.** A
session today can carry `date: 20260807` and pass validation; the feed survives it
(`ics.py:60` parses with `date.fromisoformat`), but `schedule.html:88` does
`session.date + "T00:00:00"` and would render `Invalid Date` on `/schedule`. No
data file currently contains a non-canonical date — all 58 events are clean — so
this is latent, and fixing the one shared validator closes it for both fields in a
smaller diff than two separate guards.

The subclass exists so `extra="forbid"` stays honest: a stray `season:` on a
session-level entry is rejected rather than silently ignored.

Every field has a default, so all four existing data files stay valid unchanged,
and CI's `validate-config` guards the new fields from the first commit.

### One new cross-check in `load_config`

A season-level `season:` must be a year that actually appears in that series'
sessions — otherwise a typo'd `2026` on a 2027 entry files itself under a season
with no weekends and effectively vanishes from the page. This goes beside the
existing duplicate-uid and missing-duration walks, which are the same kind of
whole-directory check.

The year set is collected from each session's `start`/`date` directly, rather
than reusing the page's "year of the weekend's first session" rule.

**This relies on an assumption worth stating: no tracked series' season crosses a
calendar year, and no weekend straddles New Year.** Verified against the data —
F1 (23 events), IMSA (10) and IndyCar (17) sit entirely in 2026, WEC splits 8/9
across 2026/2027, and no event anywhere has sessions in two calendar years. Under
that assumption the two rules produce *identical* year sets, so the simpler one is
free.

Exact parity would mean lifting `web._sorts_at` into shared code, because the
naive version is wrong: a `min()` over raw `start` strings compares mixed UTC
offsets lexically and can pick a session that is not actually first. That refactor
buys nothing while the assumption holds.

The tripwire: **the first series whose season crosses a calendar year (Formula E,
Asian Le Mans) breaks the equivalence, and this check and `schedule.html`'s
`seasonOf` must then be reconciled against one shared rule.** At that point the
page's round totals need the same attention — `seasonKey` is already
calendar-year-based, so a cross-year season would split its rounds in two before
the changelog is even considered.

## `/sessions.json` (`web._schedule()`)

All three levels flatten into one list, sorted by `date` descending. The scope
labels are resolved while walking the tree, which already has the event and
session in hand:

```json
{"series": "wec", "season": 2027, "date": "2026-08-07",
 "event": "6 Hours of Imola", "session": "Qualifying",
 "text": "Now 13:00 (was 14:00), following the revised support programme."}
```

`event` and `session` are `null` at the higher levels. `season` is filled in for
all three — derived from the event's year for the lower two, so every entry can
say which season it belongs to.

Retention does not touch these. `_schedule()` reads the config as written, not
the retention-pruned window the feeds publish, so old entries are pruned by hand
(see the agent rule below).

## Page (`src/motorcal/schedule.html`)

One closed `<details class="changes">` between `.filters` and `#schedule`,
repopulated by `render()` so the series chips filter it the way they filter the
schedule.

The widget already exists: `details.past` and `.details-body` are styled
generically in `motorcal.css:101-109`, so this costs approximately no CSS.

Each entry is a list item whose dateline carries its scope, longest first:

```
7 Aug · WEC 2027 · 6 Hours of Imola · Qualifying
  Now 13:00 (was 14:00), following the revised support programme.
```

degrading to `7 Aug · WEC 2027` for a season-level entry. Everything goes in
through `textContent` via the existing `make()` helper — same rule as the rest of
the page, since this text comes from the data directory.

A flat newest-first list is the primary view because the question it answers is
"what changed since I last looked", not "what has ever happened to Imola".

## Process (`data/CLAUDE.md`, `docs/operations.md`)

The rule that decides whether any of this stays useful — log at the **lowest
level that owns the change**:

- a moved or retimed session → session level
- a moved, added, dropped weekend, or a session removed from the timetable →
  event level
- a published or withdrawn calendar, or anything spanning weekends → season level

Also: append the entry in the same commit as the edit it explains; say what moved
*and* what source said so; do not log the first-time import of a session (that is
not a change); convert existing `#`-comment announcements like `wec.yaml:379`
into entries as you touch those seasons; prune entries older than the season.

`docs/operations.md` gets the same rule under "Editing events by hand", since a
hand edit needs it as much as the agent does.

## Tests

- `tests/test_config.py`: a `season:` year absent from the series' sessions →
  `ConfigError`; a `season:` key on a session-level entry → `ConfigError`; a
  malformed `date` and an empty `text` → `ConfigError`; `20260807` and
  `2026-W32-5` rejected as a `date` on **both** a changelog entry and a session,
  since one validator now serves both.
- `tests/test_web_schedule.py`: `/sessions.json` carries entries from all three
  levels with `event`/`session` labels filled at the lower levels and `null`
  above, `season` present on all three, sorted newest-first across two series.

No `schedule.html` self-check: the existing one covers day grouping and the
past/upcoming split because those are the only real logic on the page, and
rendering a sorted list is not.

## Deliberately not built

- **Per-weekend `<details>` on the card**, so a moved race shows its own history
  in place. Every entry already carries its event name, so this is a rendering
  change only — add it when the flat list gets long enough that people cannot
  find the race they care about.
- **`season:` optional on series-level entries**, for a genuinely series-wide
  note ("IMSA added to Motorcal"). Required for now, which is the honest
  three-level model; relax it the first time such a note needs writing.
- **Deriving entries by diffing YAML in CI.** It can produce *what* but never
  *why*, which is the half worth having.
