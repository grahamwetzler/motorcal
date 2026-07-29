# motorcal Operations Guide

## Token rotation and revocation

`MOTORCAL_TOKENS` is a comma-separated list of feed access tokens (see `.env.example`).
Multiple tokens are valid simultaneously, which is what makes rotation possible without
downtime:

1. Generate a new token (any sufficiently random string — e.g. `openssl rand -hex 32`).
2. Add it to `MOTORCAL_TOKENS` alongside the existing token(s): `old-token,new-token`.
3. Restart the `app` service (`docker compose up -d --force-recreate app`) so the new
   token list takes effect.
4. Distribute the new token/URL to calendar subscribers.
5. Once nobody is using the old token anymore, remove it from `MOTORCAL_TOKENS` and
   restart again — this revokes it. A revoked token immediately returns 404 on every
   route (`/c/{token}/{series}.ics` and `/c/{token}/status`) once the app is restarted
   with the updated list; there is no separate revocation step beyond removing it.

Tokens are compared with `secrets.compare_digest`, and application access logs redact
the token-bearing path segment (see `src/motorcal/web.py`'s `RedactTokenMiddleware`) —
but the Cloudflare Tunnel itself still sees the full request path, since it terminates
the connection before proxying to the app. Rotate a token immediately if you suspect
it has leaked (e.g. via a misconfigured non-motorcal log aggregator upstream of the
tunnel), rather than treating log redaction alone as sufficient protection.

## Restoring SQLite from a backup

1. Stop the app: `docker compose stop app` (leave `cloudflared` running or stop it too —
   either way, the feed will be briefly unavailable during restore).
2. Copy the desired backup file over the live database path (the volume-mounted
   `/data/motorcal.db` inside the container, or the corresponding host path if you're
   inspecting the named volume directly via `docker volume inspect`).
3. Start the app again: `docker compose start app`.
4. **Immediately run the force-version recovery command** (see "Forcing a refresh and
   recovering sequence numbers" below) — a restored backup's `SEQUENCE` numbers may be
   lower than what a subscribed calendar client already observed from the pre-restore
   database, and clients that see a *lower* `SEQUENCE` for the same `UID` may ignore
   the update entirely. `republish --force-version` prevents this.
5. Check `/livez` and `/readyz` (via `docker compose exec app` or directly if you've
   temporarily published the port for diagnosis) to confirm the restored database
   passes its integrity check and has usable published data.

If the *live* database (not a backup) is corrupted, `/livez` will report unhealthy and
the container's Docker health check will start failing — this is deliberate: the app
does **not** attempt to delete or auto-recreate a corrupted database. Corruption always
requires a manual restore from the most recent good backup using the steps above.

## Forcing a refresh and recovering sequence numbers

- **Force an immediate refresh cycle** (without waiting for the next scheduled cron
  tick): there is currently no dedicated "trigger now" CLI command — restarting the
  `app` service does not by itself force a refresh (the scheduler only runs on its cron
  schedule after startup). The straightforward way to force one is to temporarily set
  `source.refresh_cron` in `config.yaml` to a schedule that fires within the next
  minute (e.g. `"* * * * *"`), restart the app, wait for it to run once, then restore
  the original cron expression and restart again. A future version may add a direct
  "refresh now" command; until then, this is the supported manual method.
- **Force-advance sequence numbers after a restore**: run, from a shell with access to
  the running container (`docker compose exec app sh`, or directly if you have the
  `motorcal` CLI and the data volume available on the host):
  ```bash
  motorcal republish --db /data/motorcal.db --force-version
  ```
  This is idempotent and safe to re-run — it only advances event sequences that are
  *below* the current UTC Unix minute; already-current or already-ahead events are
  left untouched. Follow it with a forced refresh (above) so freshly-fetched content
  also gets rendered under the corrected sequence baseline.

## Validating configuration without activating it

Before restarting the app with an edited `config.yaml`/`overrides.yaml`, validate the
bundle without touching the running service or its database:

```bash
motorcal validate-config --config config/config.yaml --overrides config/overrides.yaml
```

This loads and schema-validates both files exactly as the running app would on
startup or on its periodic hot-reload check, but never opens the database and never
affects a running server. A nonzero exit code means the files are invalid — do not
restart/reload with them until this passes. Note that the running app's own hot-reload
poller performs the same validation automatically on every config file change and
silently keeps the previous configuration active if validation fails (see
`src/motorcal/refresh.py`'s `check_and_reload_config`) — running `validate-config`
by hand ahead of time is a convenience for catching mistakes before they're written
to the live config files at all, not the only safety net.

## Resolving unmatched patches and classifications

Check `GET /c/{token}/status` (JSON body) for two fields:

- `patch_errors`: each entry identifies a patch (`id_event` or `match`) that matched
  zero or more-than-one source events on the last rebuild, with a `reason` of
  `"no_match"` or `"multiple_matches"`. **When any patch error is present, the
  previously valid published configuration remains active** — the invalid patch does
  not corrupt anything, but it also doesn't take effect. Fix the patch's `id_event`
  or `match.{series,date,contains}` in `overrides.yaml` so it matches exactly one
  event, then let the next hot-reload or scheduled refresh pick it up.
- `unknown_events`: UIDs of events whose session name didn't match any of that
  series' classification rules (`src/motorcal/classify.py`). These events are still
  published normally (visible in the feed, just without an inferred alarm or
  duration) — an entry here usually means TheSportsDB introduced a new session-name
  format. Cross-reference the UID against `source_events` (via a direct SQLite query
  against the data volume, e.g. `sqlite3 /data/motorcal.db "SELECT name FROM
  source_events WHERE id_event = '...'"`) to see the actual event name, then extend
  the relevant series' rule list in `classify.py` and ship a code update — this is
  not something `overrides.yaml` can fix on its own, since classification rules are
  code, not configuration.

## Interpreting stale, incomplete, and suspicious-empty refreshes

`GET /c/{token}/status` and `GET /healthz` both report per-series freshness. Interpret
the states as follows:

- **Stale** (`stale: true`, `last_complete_at` is set but old): the last *complete*
  snapshot for that series is older than the freshness threshold (12 hours by
  default). This usually means the scheduled refresh has been failing repeatedly
  (network issues, TheSportsDB rate-limiting/outage, or a persistent config error
  blocking reloads) — check container logs for `ProviderError`/`ConfigError` entries.
  The previously published calendar keeps serving as last-known-good; nothing is
  silently emptied.
- **Never refreshed** (`last_complete_at: null`): either the app was started very
  recently and hasn't had a scheduled tick yet, or every attempted refresh for that
  series has been *incomplete* (some round request failed) or *suspicious-empty*
  (a complete scan returned zero events for a series/season that previously had
  data, or for the current calendar-year season at all) — both cases are discarded
  in full by design (see `src/motorcal/store.py`'s `ingest_snapshot`) rather than
  overwriting good data with a partial or suspicious result. This is not a bug to
  "fix" by forcing a commit; it means the upstream data genuinely wasn't safely
  usable on that attempt, and the next scheduled tick will simply try again.
- **Incomplete snapshot**: not directly exposed as its own status field today, but
  its effect is exactly "never refreshed" persisting for one series/season longer
  than expected. If this continues across many consecutive scheduled ticks, suspect
  a systemic issue (the round-scan `deadline_seconds`, the token-bucket rate limit,
  or an upstream API key nearing its request quota) rather than a one-off blip.
- **Suspicious-empty specifically for a *future* season** (e.g. the season fetched
  starting Oct 1 per `next_season_from`) is expected and harmless if that season's
  calendar simply hasn't been announced yet — it is only "suspicious" (and rejected)
  once that scope previously had real data and then reported zero.
