# motorcal

Self-hosted per-series motorsports ICS calendar publisher. It pulls race
weekends from TheSportsDB, classifies sessions (practice/qualifying/race/etc.),
applies your own patches and manual events, and publishes one ICS feed per
series that you can subscribe to from any calendar app.

Feeds are served over a token-gated URL and exposed to the internet via a
Cloudflare Tunnel, so nothing needs to be port-forwarded.

A separate admin web UI (port 8001) lets you view events and edit/add
overrides without hand-editing YAML. It's LAN-reachable but intentionally
**not** exposed through the Cloudflare Tunnel (the tunnel only ever forwards
port 8000) and has no login of its own — keep it off untrusted networks.

## Quick start

1. Copy the example env file and fill it in:

   ```bash
   cp .env.example .env
   ```

2. Copy the example config and adjust series/defaults to taste:

   ```bash
   cp config/config.example.yaml config/config.yaml
   cp config/overrides.example.yaml config/overrides.yaml
   ```

3. Start everything:

   ```bash
   docker compose up -d
   ```

4. Subscribe to a feed at `https://<your-domain>/c/<one-of-your-tokens>/<series>.ics`
   (series keys come from `config.yaml`, e.g. `f1`, `wec`, `indycar`, `imsa`).

5. Open `http://<host-on-your-lan>:8001/` to view events and edit or add
   overrides. Changes are picked up by the running app within ~30 seconds,
   same as editing `overrides.yaml` by hand.

## Environment variables

Set these in a `.env` file next to `compose.yaml` (see `.env.example`). All
three are required — `compose.yaml` fails fast at startup if any are unset.

| Variable | Required | Description |
| --- | --- | --- |
| `THESPORTSDB_API_KEY` | Yes | TheSportsDB API key. Use a real (paid or free-tier) key, not the shared public `"3"` test key, for anything other than throwaway testing. |
| `MOTORCAL_TOKENS` | Yes | Comma-separated list of feed access tokens, e.g. `token-one,token-two`. Each token grants access to every series' feed and to `/c/{token}/status`. Multiple tokens let you rotate one out without downtime — see "Token rotation" in `docs/operations.md`. |
| `CLOUDFLARE_TUNNEL_TOKEN` | Yes | Token for a Cloudflare Tunnel (Zero Trust dashboard → Networks → Tunnels → create a tunnel → choose the Docker connector → copy the token shown, not the certificate). Used by the `cloudflared` service to expose the app without publishing a port. |

## Config files

`compose.yaml` mounts `./config` into the container, so these live on the
host, not in the image. It's mounted read-write because the admin UI writes
to `overrides.yaml`; the host `./config` directory must be writable by the
container's user (uid 1000) -- e.g. `chown -R 1000:1000 config` if you don't
need host-side write access as yourself, or `chmod -R g+w config` plus adding
yourself to a group with uid 1000.

- `config/config.yaml` — server/source/retention/series settings. Start from
  `config/config.example.yaml`.
- `config/overrides.yaml` — manual patches to source events and fully manual
  events. Start from `config/overrides.example.yaml`.

Before restarting the app after editing either file, validate them without
touching the running service:

```bash
docker compose exec app motorcal validate-config --config /config/config.yaml --overrides /config/overrides.yaml
```

The running app also hot-reloads both files automatically on change and keeps
the previous configuration active if validation fails.

## Data

The SQLite database lives in the `motorcal-data` named volume, at
`/data/motorcal.db` inside the `app` container. Back it up regularly — see
`docs/operations.md` for backup/restore steps, since restoring a backup
requires a follow-up `republish --force-version` step to keep calendar
clients from ignoring the restored data.

## Health checks

- `GET /livez` — process is up.
- `GET /readyz` — database is reachable and passes its integrity check.
- `GET /healthz` — per-series freshness summary.
- `GET /c/{token}/status` — authenticated per-series status, including patch
  errors and unmatched-classification warnings.

## More

See `docs/operations.md` for token rotation/revocation, restoring from a
backup, forcing a refresh cycle, and interpreting stale/incomplete/
suspicious-empty refresh states.
