# Docker auto-deploy design

## Goal

Push to `main` on GitHub → the running container updates itself, on this
machine and on whatever machine eventually runs this instead. No host-level
cron/systemd/git-checkout on the deployment machine — the update mechanism
lives inside the compose stack itself.

## Architecture

GitHub Actions builds the image and pushes it to
`ghcr.io/grahamwetzler/motorcal` on every push to `main`, after tests and
config validation pass. A `watchtower` container in `compose.yaml` polls GHCR
every 30 minutes and recreates the `app` container when the `latest` tag's
digest changes.

## CI changes (`.github/workflows/ci.yml`)

New `build-and-push` job:

- `needs: [test, validate-config]` — a failing test or invalid config never
  produces a published image
- triggers only on `push` to `main` (not on pull requests — forks can't push
  packages)
- logs into GHCR with the built-in `GITHUB_TOKEN`, no new secret
- pushes tags `latest` and `sha-<short-sha>` (the sha tag exists purely so a
  bad `latest` can be rolled back to a known-good image)

One-time manual step after the first push: set the new GHCR package's
visibility to Public in its GitHub settings — it defaults to private even
though the repo is public.

## `compose.yaml` changes

- `app` gets `image: ghcr.io/grahamwetzler/motorcal:latest`
- `app` gets label `com.centurylinklabs.watchtower.enable=true`
- new `watchtower` service (`containrrr/watchtower` image), docker.sock
  mounted read-only, `--label-enable --cleanup --interval 1800`, restart
  unless-stopped

## Rollback

Pin `app.image` to a previous `sha-<shortsha>` tag and `docker compose up
-d`. A sha tag is never repushed, so Watchtower leaves it alone until the
tag is switched back to `:latest`.

## Verification

- Push a trivial commit to `main`, confirm the `build-and-push` job runs and
  the GHCR package appears with `latest` + `sha-*` tags
- On this machine: `docker compose pull && docker compose up -d`, confirm
  `docker compose logs watchtower` shows it monitoring `app`
- Confirm `docker compose config` is valid and existing tests still pass
