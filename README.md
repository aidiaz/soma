# soma

A personal training data system. Rides, sleep, HRV, food and body measurements,
correlated by date in one database and served to Claude over
[MCP](https://modelcontextprotocol.io).

> **Working name.** Replace it before the first OAuth redirect URI is
> registered — that is the point of no return.

## The question

> "Here is my week. What should change?"

Answering it needs rides, sleep, HRV, food and body measurements correlated by
date. That correlation used to happen by hand, in a spreadsheet, and again in
conversation. This system does the join once, in storage, so the analysis layer
only reasons.

It is not a dashboard and not a replacement for SYSTM or Garmin Connect. It is
the layer that makes the weekly coaching conversation cost thirty seconds
instead of fifteen minutes of pasting.

## Principles

| Principle | Consequence |
|---|---|
| The database does the join, not the LLM | One MCP call returns a merged week |
| Separate by layer, not by vendor | One ingestion worker per source, one serving layer |
| Never hit a vendor on the hot path | Sync writes, the server only reads |
| Isolate the fragile from the stable | Garmin breaking must not stop anything else |
| Read by default, write narrowly | Only nutrition and body are writable |
| Judgement lives in a skill, not a context window | Coaching rules are version-controlled |

## Layout

```
src/soma/
  models.py            the schema — date is the join key
  db.py  metrics.py    storage, and CTL/ATL/TSB + weekly ramp
  ingest/garmin/       the fragile source, isolated
  serve/               MCP tools, HTTP app, OAuth allowlist
.claude/skills/weekly-review/   the coaching rules
```

## Processes

| Process | Command | When | Touches a vendor? |
|---|---|---|---|
| Garmin auth | `garmin-auth` | Rarely, interactive | Yes (login + MFA) |
| Garmin sync | `garmin-sync` | Nightly | Yes (read-only) |
| MCP server | `soma` | Always on (stdio) | **No** |
| MCP server | `soma-http` | Always on (HTTP) | **No** |

## Tools

Ten, on purpose. One person, one read surface.

| Tool | What it does |
|---|---|
| `get_training_week` | **The primary tool.** The merged week: sessions, health, food, body, and the derived signals |
| `get_daily_series` | One row per day over a long window, every variable already joined |
| `get_health_trend` | Resting HR, HRV, sleep and Body Battery over a window |
| `get_recent_activities` | Session detail when one ride needs looking at |
| `get_tests` | FTP and 4DP history with W/kg |
| `get_sync_status` | Whether ingestion is actually running, per source, with the last few runs |
| `request_sync` | Ask a sync worker to run now, for the ride that just finished |
| `log_food` | Record one thing eaten — reporting it in conversation is what creates the entry |
| `log_water` | Add water, a glass at a time |
| `log_body` | Weight and waist, weekly |

`request_sync` is the one tool that causes anything outside the database to
happen, and it still does not leave it: the server holds no vendor credential,
so it appends to a queue and the sync worker that does hold one picks the row
up. That keeps a vendor outage out of the path of every other question.

No delete tool. Corrections happen by upsert on the date key.

`get_training_week` returns a `coverage` block. Read it before reading anything
into a gap — a sync that failed looks exactly like a week of rest days. The
block carries a `sync` report for exactly that reason: every run of every worker
is recorded in `sync_runs`, so "nothing was ingested" and "nothing happened" are
different answers rather than the same silence. `get_sync_status` asks the same
question directly, with the last few runs and any error.

## Setup

```bash
make install
cp .env.example .env      # then edit
uv run garmin-auth        # one-time, ~every 6 months
uv run garmin-sync        # first backfill; takes many minutes
```

> **Do not loop `garmin-auth`.** Garmin's SSO limit is per account, and no
> network change clears it. The CLI refuses to retry inside a 15-minute cooldown.

Then connect over stdio:

```bash
claude mcp add soma -- uv run --directory /path/to/soma soma
```

Or over HTTP with a bearer token for testing:

```bash
uv run soma-http
claude mcp add --transport http soma http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $SOMA_API_TOKEN"
```

In production the HTTP server uses Google OAuth plus an email allowlist. See
[deploy/RASPBERRY_PI.md](deploy/RASPBERRY_PI.md).

## Working on this

```bash
make test      # 175 unit/integration tests
make smoke     # drives the running app over real HTTP, asserts every tool, scans the log
make reach     # probes every third-party endpoint, no credentials needed
make lint      # ruff check + format check
make contract  # confirms the Garmin endpoints still exist
make typecheck # basedpyright
make ci        # lint + test + contract + smoke
```

`make test` cannot see a lifespan fault, a mount-order fault, or a handler that
returns 200 while logging an exception. `make smoke` can — it starts a real
server on a throwaway database, drives every tool as a client, and fails if
the process logged anything unexpected. Point it at any target:

```bash
python scripts/smoke.py --url http://127.0.0.1:8001/mcp --token "$TOK" --log container.log
```

## What is not built yet

Wahoo ingestion (blocked on developer approval), SYSTM planned workouts, and
Google Calendar sync. The `planned_workouts` and `calendar_sync` tables exist
and are unused; nothing depends on them. See CLAUDE.md for the open questions.
