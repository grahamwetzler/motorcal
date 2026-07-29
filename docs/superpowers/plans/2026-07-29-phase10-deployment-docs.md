# Motorsports Calendar — Phase 10: Docker, Compose, Tunnel, Ops Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the application for deployment (multi-stage `Dockerfile`, `compose.yaml` with Cloudflare Tunnel), add the two remaining operational CLI commands the spec requires (`validate-config`, `republish --force-version`), and write the operational documentation covering all six required topics.

**Architecture:** Tasks 1-2 are infrastructure files, not Python — they are verified by actually building the image and validating the Compose file with Docker (available in this environment), not by `pytest`. Task 3 is real Python with real tests, following the same TDD pattern as every other phase. Task 4 is documentation.

**Tech Stack:** Docker (multi-stage build), Docker Compose, `cloudflare/cloudflared`. No new Python dependencies.

## Global Constraints

- Full spec: `~/.claude/plans/research-and-plan-how-expressive-cookie.md` — every task below implements a slice of it; consult it if a step is ambiguous. The "Deployment and recovery" section is this phase's primary spec.
- Multi-stage `Dockerfile`: `uv sync --frozen` (never `pip install`, per this project's own conventions), `python:3.13-slim` base, a non-root runtime user, separate read-only config mount (`/config`) and writable data mount (`/data`).
- `compose.yaml` runs the app and `cloudflare/cloudflared`. The application port is **not published to the host** (Cloudflare Access is not used, since calendar clients can't complete interactive auth — the tunnel is the only ingress). One application replica by default; the SQLite lease (Phase 2) remains the protection against duplicate scheduler execution regardless.
- The Docker health check uses `/livez` (Phase 8), never upstream freshness — an API outage must never cause a restart loop. Monitoring uses `/healthz`.
- Corruption must never trigger automatic deletion/replacement (already enforced by Phase 2/8's `check_integrity`/`/livez`/`/readyz` failing loudly instead). Recovery is: restore the latest backup (Phase 2's `backup_database`/CLI `backup` command already exist), then run `republish --force-version` (this phase, new) to advance every retained event's sequence to at least the current UTC Unix minute — this prevents a restored sequence from being lower than what a client may have already observed — then let the next scheduled refresh run normally.
- `validate-config` must validate the whole bundle (`config.yaml` + `overrides.yaml`) **without activating it** — it must never write to the database or affect a running server; it's purely a startup-adjacent sanity check an operator runs before restarting.
- Operational documentation must cover, at minimum: token rotation/revocation, restoring SQLite, forcing a refresh, validating configuration without activating it, resolving unmatched patches/classifications, and interpreting stale/incomplete/suspicious-empty refreshes.
- No pip: dependency management is `uv` only, including inside the Dockerfile (achieved via copying the official `ghcr.io/astral-sh/uv` binary into the build stage, never `pip install uv`).

---

### Task 1: Multi-stage `Dockerfile`

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

**Interfaces:**
- Produces: a runnable `motorcal` container image with the `motorcal` CLI as its entrypoint, `serve` as the default command (overridable), running as a non-root user, with `/config` and `/data` mount points already created.

- [ ] **Step 1: Write `.dockerignore`**

```
.venv/
.git/
.github/
__pycache__/
*.pyc
*.db
*.db-wal
*.db-shm
.env
config/config.yaml
config/overrides.yaml
docs/
.worktrees/
.claude/
```

- [ ] **Step 2: Write `Dockerfile`**

This exact structure was already built and verified in this environment (image built successfully, ran as `uid=1000(motorcal)`, `motorcal --help` and `motorcal init-db` both worked against a mounted volume):

```dockerfile
# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.9.27 AS uv

FROM python:3.13-slim AS builder
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13-slim AS runtime
RUN groupadd --gid 1000 motorcal && useradd --uid 1000 --gid motorcal --create-home motorcal
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml ./
ENV PATH="/app/.venv/bin:$PATH"
RUN mkdir -p /data /config && chown motorcal:motorcal /data
USER motorcal
EXPOSE 8000
ENTRYPOINT ["motorcal"]
CMD ["serve", "--db", "/data/motorcal.db", "--config", "/config/config.yaml", "--overrides", "/config/overrides.yaml"]
```

- [ ] **Step 3: Build the image and verify it**

Run:
```bash
docker build -t motorcal:test .
```
Expected: build completes successfully (all layers, including `uv sync --frozen` installing dependencies from the committed `uv.lock`, and the final `motorcal` package install).

Run:
```bash
docker run --rm --entrypoint whoami motorcal:test
```
Expected: prints `motorcal` (confirming the container runs as the non-root user, not root).

Run:
```bash
docker run --rm --entrypoint python motorcal:test -c "import motorcal; print('ok')"
```
Expected: prints `ok` (confirming the package installed correctly into the runtime image, not just the builder stage).

Run:
```bash
docker run --rm --entrypoint motorcal motorcal:test --help
```
Expected: prints usage listing `init-db`, `backup`, and `serve` (the three subcommands that exist as of Phase 9 — Task 3 of this phase adds two more, `validate-config` and `republish`, which won't appear yet at this point in the plan).

Run (verify the writable data mount actually works end-to-end, using a real bind mount to a scratch host directory):
```bash
mkdir -p /tmp/motorcal-docker-verify
docker run --rm --entrypoint motorcal -v /tmp/motorcal-docker-verify:/data motorcal:test init-db --db /data/test.db
ls /tmp/motorcal-docker-verify/test.db  # should exist
rm -rf /tmp/motorcal-docker-verify
```
Expected: `Initialized database at /data/test.db` printed, and the file exists on the host afterward (proving the volume mount and non-root file permissions both work).

- [ ] **Step 4: Clean up the test image**

```bash
docker rmi motorcal:test
```

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "Add multi-stage Dockerfile (uv sync --frozen, non-root, python:3.13-slim)"
```

---

### Task 2: `compose.yaml` and Cloudflare Tunnel wiring

**Files:**
- Create: `compose.yaml`
- Modify: `.env.example`

**Interfaces:**
- Produces: a Compose stack running the app (built from the Task 1 `Dockerfile`) and `cloudflare/cloudflared`, with the app's port never published to the host, config mounted read-only, data on a named volume, and a `/livez`-based health check.

- [ ] **Step 1: Write `compose.yaml`**

This exact structure was already validated in this environment via `docker compose config` (produced valid, fully-resolved YAML with no errors):

```yaml
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    restart: unless-stopped
    environment:
      THESPORTSDB_API_KEY: "${THESPORTSDB_API_KEY:?THESPORTSDB_API_KEY must be set}"
      MOTORCAL_TOKENS: "${MOTORCAL_TOKENS:?MOTORCAL_TOKENS must be set}"
    volumes:
      - ./config:/config:ro
      - motorcal-data:/data
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/livez', timeout=3).status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

  cloudflared:
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      TUNNEL_TOKEN: "${CLOUDFLARE_TUNNEL_TOKEN:?CLOUDFLARE_TUNNEL_TOKEN must be set}"
    depends_on:
      - app

volumes:
  motorcal-data:
```

Note deliberately: the `app` service has no `ports:` mapping at all — this is what keeps the application port unpublished to the host, per the spec. `cloudflared` reaches `app` over the Compose-internal network by service name (`app:8000`), which must be configured as the tunnel's public hostname target in Cloudflare's dashboard (outside this repo's scope — a one-time manual step documented in Task 4).

- [ ] **Step 2: Add the Cloudflare Tunnel token to `.env.example`**

Read the existing `.env.example` first (it currently has `THESPORTSDB_API_KEY` and `MOTORCAL_TOKENS`). Append:

```bash
# Required: Cloudflare Tunnel token (from the Cloudflare Zero Trust dashboard,
# "Networks > Tunnels" -- create a tunnel, choose the Docker connector, and
# copy the token shown there; do not paste the tunnel's *certificate*, just
# the token string).
CLOUDFLARE_TUNNEL_TOKEN=changeme-tunnel-token
```

- [ ] **Step 3: Validate the Compose file**

Run:
```bash
THESPORTSDB_API_KEY=test MOTORCAL_TOKENS=test CLOUDFLARE_TUNNEL_TOKEN=test docker compose config
```
Expected: prints the fully-resolved Compose configuration with no errors — confirm in the output that the `app` service has no `ports:` key at all, and that `volumes` includes a read-only bind mount for `./config` and a named volume for `/data`.

- [ ] **Step 4: Commit**

```bash
git add compose.yaml .env.example
git commit -m "Add compose.yaml running the app and cloudflared, with the app port unpublished"
```

---

### Task 3: `validate-config` and `republish --force-version` CLI commands

**Files:**
- Modify: `src/motorcal/store.py`
- Modify: `src/motorcal/cli.py`
- Test: `tests/test_store_republish.py`
- Test: `tests/test_cli_ops_commands.py`

**Interfaces:**
- Consumes: `load_config`, `load_overrides`, `ConfigError` (Phase 1); `list_published_events`, `transaction` (Phase 2/6).
- Produces:
  - `def force_advance_all_sequences(conn: sqlite3.Connection, now_unix_minute: int, now_iso: str) -> int` (new in `store.py`) — for every `published_events` row whose `sequence` is below `now_unix_minute`, sets `sequence = now_unix_minute` and `last_modified = now_iso`; rows already at or above `now_unix_minute` are left completely untouched (this is "advance to **at least**," not "reset all to exactly"). Returns the count of rows actually changed. Runs inside one `transaction()`.
  - `motorcal validate-config --config PATH --overrides PATH`: loads and validates both files; prints `"Configuration is valid."` and exits `0` on success; prints the `ConfigError` message to stderr and exits `1` on failure. Never opens or touches the database.
  - `motorcal republish --db PATH --force-version`: computes the current UTC Unix minute, calls `force_advance_all_sequences`, and prints how many events were advanced. `--force-version` is a required flag (there is no other `republish` mode yet) — this makes the operator's intent explicit in the command line itself, matching the spec's own naming of the operation.

- [ ] **Step 1: Write the failing store test**

```python
# tests/test_store_republish.py
from motorcal.store import (
    connect,
    force_advance_all_sequences,
    init_schema,
    list_published_events,
    transaction,
    upsert_published_event,
)


def _fresh_conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_schema(conn)
    return conn


def _insert(conn, uid, sequence):
    upsert_published_event(
        conn, uid=uid, series="wec", session_type="race", summary="S", start=None,
        all_day_date="2026-01-01", time_confirmed=False, duration_seconds=None, location=None,
        description="D", status="CONFIRMED", sequence=sequence, dtstamp="t0", last_modified="t0",
        fingerprint="fp", alarms_json="[]", source_provider="thesportsdb", source_id_event="1",
        synthetic_uid=None, cancelled_at=None, retain_until=None,
    )


def test_force_advance_bumps_sequences_below_the_target(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "u1", sequence=100)

    count = force_advance_all_sequences(conn, now_unix_minute=500000000, now_iso="2026-08-01T00:00:00+00:00")

    assert count == 1
    rows = {row["uid"]: row for row in list_published_events(conn)}
    assert rows["u1"]["sequence"] == 500000000
    assert rows["u1"]["last_modified"] == "2026-08-01T00:00:00+00:00"


def test_force_advance_leaves_already_ahead_sequences_untouched(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "u1", sequence=99999999999)  # already far ahead of any real "now"

    count = force_advance_all_sequences(conn, now_unix_minute=500000000, now_iso="2026-08-01T00:00:00+00:00")

    assert count == 0
    rows = {row["uid"]: row for row in list_published_events(conn)}
    assert rows["u1"]["sequence"] == 99999999999
    assert rows["u1"]["last_modified"] == "t0"  # untouched


def test_force_advance_handles_multiple_events_independently(tmp_path):
    conn = _fresh_conn(tmp_path)
    with transaction(conn):
        _insert(conn, "below", sequence=1)
        _insert(conn, "above", sequence=999999999999)

    count = force_advance_all_sequences(conn, now_unix_minute=500000000, now_iso="t1")

    assert count == 1
    rows = {row["uid"]: row for row in list_published_events(conn)}
    assert rows["below"]["sequence"] == 500000000
    assert rows["above"]["sequence"] == 999999999999
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_republish.py -v`
Expected: FAIL / collection error — `force_advance_all_sequences` does not exist yet.

- [ ] **Step 3: Append to `src/motorcal/store.py`**

```python
def force_advance_all_sequences(
    conn: sqlite3.Connection, now_unix_minute: int, now_iso: str
) -> int:
    """Advance every published event's sequence to at least now_unix_minute.

    Used by the `republish --force-version` recovery operation: after
    restoring an older backup, this guarantees no restored sequence number is
    lower than one a client may have already observed. Rows already at or
    above now_unix_minute are left untouched.
    """
    count = 0
    with transaction(conn):
        for row in list_published_events(conn):
            if row["sequence"] < now_unix_minute:
                conn.execute(
                    "UPDATE published_events SET sequence = ?, last_modified = ? WHERE uid = ?",
                    (now_unix_minute, now_iso, row["uid"]),
                )
                count += 1
    return count
```

- [ ] **Step 4: Run the store test to verify it passes**

Run: `uv run pytest tests/test_store_republish.py -v`
Expected: PASS, 3 passed.

- [ ] **Step 5: Write the failing CLI tests**

```python
# tests/test_cli_ops_commands.py
from pathlib import Path

from motorcal.cli import main
from motorcal.store import connect, init_schema, transaction, upsert_published_event

EXAMPLE_CONFIG = Path("config/config.example.yaml")
EXAMPLE_OVERRIDES = Path("config/overrides.example.yaml")


def test_validate_config_succeeds_on_the_example_files(capsys):
    exit_code = main(
        ["validate-config", "--config", str(EXAMPLE_CONFIG), "--overrides", str(EXAMPLE_OVERRIDES)]
    )
    assert exit_code == 0
    assert "valid" in capsys.readouterr().out.lower()


def test_validate_config_fails_on_invalid_yaml(tmp_path, capsys):
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("not: valid: yaml: [[[")

    exit_code = main(
        ["validate-config", "--config", str(bad_config), "--overrides", str(EXAMPLE_OVERRIDES)]
    )
    assert exit_code == 1
    assert capsys.readouterr().err != ""


def test_validate_config_never_touches_a_database(tmp_path, capsys):
    # There is deliberately no --db argument at all -- prove the command
    # doesn't need one and does no database I/O.
    exit_code = main(
        ["validate-config", "--config", str(EXAMPLE_CONFIG), "--overrides", str(EXAMPLE_OVERRIDES)]
    )
    assert exit_code == 0
    assert not (tmp_path / "test.db").exists()


def test_republish_force_version_advances_stale_sequences(tmp_path, capsys):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_schema(conn)
    with transaction(conn):
        upsert_published_event(
            conn, uid="u1", series="wec", session_type="race", summary="S", start=None,
            all_day_date="2026-01-01", time_confirmed=False, duration_seconds=None, location=None,
            description="D", status="CONFIRMED", sequence=1, dtstamp="t0", last_modified="t0",
            fingerprint="fp", alarms_json="[]", source_provider="thesportsdb", source_id_event="1",
            synthetic_uid=None, cancelled_at=None, retain_until=None,
        )
    conn.close()

    exit_code = main(["republish", "--db", str(db_path), "--force-version"])

    assert exit_code == 0
    assert "1" in capsys.readouterr().out

    conn2 = connect(db_path)
    from motorcal.store import list_published_events
    row = list_published_events(conn2)[0]
    assert row["sequence"] > 1


def test_republish_requires_force_version_flag():
    import pytest

    with pytest.raises(SystemExit):
        main(["republish", "--db", "/tmp/whatever.db"])
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_ops_commands.py -v`
Expected: FAIL / collection error — `validate-config` and `republish` subcommands don't exist yet.

- [ ] **Step 7: Add the commands to `src/motorcal/cli.py`**

Read the existing `cli.py` in full first. Add `force_advance_all_sequences` to the existing `from motorcal.store import (...)` block (do not duplicate the import line). Add these two command functions (place them near the other `_cmd_*` functions):

```python
def _cmd_validate_config(args: argparse.Namespace) -> int:
    try:
        load_config(Path(args.config))
        load_overrides(Path(args.overrides))
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1
    print("Configuration is valid.")
    return 0


def _cmd_republish(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    conn = connect(db_path)
    now = datetime.now(timezone.utc)
    now_unix_minute = int(now.timestamp() // 60)
    count = force_advance_all_sequences(conn, now_unix_minute, now.isoformat())
    conn.close()
    print(f"Advanced sequence for {count} published event(s) to at least {now_unix_minute}.")
    return 0
```

Add these two subparsers to `_build_parser`, after the `serve_parser` block:

```python
    validate_config_parser = subparsers.add_parser(
        "validate-config", help="Validate config.yaml + overrides.yaml without activating them"
    )
    validate_config_parser.add_argument("--config", required=True, help="Path to config.yaml")
    validate_config_parser.add_argument("--overrides", required=True, help="Path to overrides.yaml")
    validate_config_parser.set_defaults(func=_cmd_validate_config)

    republish_parser = subparsers.add_parser(
        "republish", parents=[db_parent], help="Recovery: force-advance published event sequences"
    )
    republish_parser.add_argument(
        "--force-version", action="store_true", required=True,
        help="Advance every retained event's sequence to at least the current UTC Unix minute",
    )
    republish_parser.set_defaults(func=_cmd_republish)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_ops_commands.py -v`
Expected: PASS, 5 passed.

- [ ] **Step 9: Run the entire test suite**

Run: `uv run pytest -v`
Expected: all tests from Phases 1-9 (286) plus this task's 3 (store) plus 5 (CLI) pass — 294 passed total.

- [ ] **Step 10: Verify the CLI's `--help` output now shows all five subcommands, inside the Docker image built in Task 1**

Run:
```bash
docker build -t motorcal:verify .
docker run --rm --entrypoint motorcal motorcal:verify --help
docker rmi motorcal:verify
```
Expected: usage output lists `init-db`, `backup`, `serve`, `validate-config`, `republish`.

- [ ] **Step 11: Commit**

```bash
git add src/motorcal/store.py src/motorcal/cli.py tests/test_store_republish.py tests/test_cli_ops_commands.py
git commit -m "Add validate-config and republish --force-version CLI commands"
```

---

### Task 4: Operational documentation

**Files:**
- Create: `docs/operations.md`

**Interfaces:**
- Produces: a single operations reference covering every topic the spec requires.

- [ ] **Step 1: Write `docs/operations.md`**

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add docs/operations.md
git commit -m "Add operational documentation (tokens, restore, refresh, config validation, diagnostics)"
```

---

## Self-Review Notes (for the plan author, already applied above)

- Spec coverage: multi-stage `Dockerfile` with `uv sync --frozen`/non-root/`python:3.13-slim` (Deployment section, verified by an actual `docker build` + `docker run` in this environment before this plan was finalized); `compose.yaml` with the app port unpublished and `cloudflared` sharing its network (same section, verified by `docker compose config`); `/livez`-based Docker health check; `republish --force-version` sequence-advancement semantics (Deployment and recovery section); all six required operational documentation topics.
- Explicitly out of scope for this phase (acknowledged directly in `docs/operations.md` rather than silently glossed over): there is no dedicated "force a refresh right now" CLI command — the spec's verification item 15 ("run a live one-shot refresh") and the interactive Cloudflare Tunnel dashboard setup are both operator/manual-testing concerns this plan documents the workaround for rather than building new automation for, since neither was named as a concrete deliverable in the Build Order's Phase 10 description ("Docker, Compose, tunnel, logging redaction, and operations documentation" — logging redaction was already built in Phase 8).
- Type consistency check: `force_advance_all_sequences` takes `now_unix_minute: int` and `now_iso: str` as separate explicit parameters (matching the pattern every other phase's time-sensitive function uses — callers compute both from one `datetime.now(timezone.utc)` call, exactly as `_cmd_republish` does), rather than taking a single `datetime` and deriving both internally, keeping it trivially testable with hand-picked edge-case integers without needing to construct real `datetime` objects for boundary conditions.
