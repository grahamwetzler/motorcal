## Start
- `start` represents the start of the broadcast for races, sprint races, and qualifying. If a clear broadcast time is not available, use official session start time.

## Notes
Add to notes if available:
  - Green flag time in delta minutes after event start time (i.e. Green flag start+10m)
  - Broadcast channels and streaming providers for US (i.e. Fox, FS1, Peacock, etc)
  - Round number out of total rounds for the season (i.e. Round 1 of 21)

Set an event's `url` to its official event-specific page when one exists. Leave
it unset rather than using a series schedule or venue page as a fallback.

## Changes
When you change a schedule that was already published, add a `changes` entry
saying what changed and why, in the same commit as the edit. The entries are
served at `/sessions.json` and shown on `/schedule`, so write them for a
subscriber, not for yourself: name what moved, and what source said so.

Each entry is a `date` (today, as `YYYY-MM-DD` — when the change was made, not
when the session runs) and a `text`.

Put the entry at the **lowest level that owns the change** — `/schedule` folds
each level away in a different place, so the level you pick is what the reader
sees it attached to when they open it:

- a session retimed, renamed, or cancelled → `changes` on that **session**,
  shown in a row under it
- a weekend moved, added, or dropped, or a session removed from its timetable
  altogether → `changes` on that **event**, shown on its card (a deleted session
  takes its own entries with it, so its removal has to be recorded one level up)
- a calendar published or withdrawn, or anything spanning weekends → `changes`
  on the **series**, with a `season:` year, shown in the panel above the whole
  schedule. A file can hold more than one season, and the year must be one the
  series actually runs sessions in.

Do not log the first import of a session — that is not a change. Prune entries
from seasons that are over; nothing prunes them automatically.
