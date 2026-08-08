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

**Only log a change when the weekend it affects is less than 90 days away.**
Further out, a calendar is still provisional and moves repeatedly; logging it
fills the page with churn about dates nobody can plan around yet. Beyond 90
days, just make the edit — the commit message and `git log -p` are the record.
Measure from today to the weekend, not to the date of the announcement.

**Except for a cancellation or a move — log those however far out they are.** A
round canceled outright, or a weekend shifted to different dates or a different
circuit, is not the provisional churn the rule is there to suppress: it is the
calendar changing shape, it rarely gets walked back, and a subscriber wants to
know now. The 90-day rule is about session times drifting inside a weekend that
is still going ahead as scheduled.

Write every `text` in simple, concise American English. One or two short
sentences, American spelling ("program", not "programme"), no hedging and no
throat-clearing. Say what it is now and what it was before, then the source.

Put the entry at the **lowest level that owns the change** — `/schedule` folds
each level away in a different place, so the level you pick is what the reader
sees it attached to when they open it:

- a session retimed, renamed, or canceled → `changes` on that **session**,
  shown in a row under it (one session called off is not the cancellation the
  exception above means — that is a whole round)
- a weekend moved, added, or dropped, or a session removed from its timetable
  altogether → `changes` on that **event**, shown on its card (a deleted session
  takes its own entries with it, so its removal has to be recorded one level up)
- a round dropped from the calendar, or anything else spanning weekends →
  `changes` on the **series**, with a `season:` year, shown in the panel above
  the whole schedule. A file can hold more than one season, and the year must be
  one the series actually runs sessions in.

Both the 90-day rule and its exception apply at all three levels. A season's
calendar being published is still not worth logging — that is exactly the
provisional case the rule targets — but a round later dropped from it always is,
however far out, and a dropped weekend has no card left to hang a note on, which
is what the series level is for.

Do not log the first import of a session — that is not a change. Prune entries
from seasons that are over; nothing prunes them automatically.
