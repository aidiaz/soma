# Changelog

Notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

An entry earns its place by changing what an operator or a caller sees. A
refactor with no behaviour change does not belong here — the git log already
holds it.

## [Unreleased]

### Added

- **On-demand sync: the `request_sync` tool.** Ingestion is scheduled — Garmin
  at 08:00, Wahoo hourly — which is right for the data and wrong for the ride
  you just finished. `request_sync` asks a worker to run now, for one source or
  both. It does *not* sync: the server holds no vendor credential and is not
  allowed to, so it appends a row to a new `sync_requests` table and the
  worker for that source picks it up within `SOMA_SYNC_POLL_S` (default 30s).
  Repeat asks coalesce into one sync, and a source that ran in the last two
  minutes declines another — Garmin's rate limit is per account and only time
  clears it. The reply carries the worker's health, because a request handed to
  a worker that died on Tuesday is never served and nothing else would say so.
  Requests are kept after they are served, with the `sync_runs` row that served
  them.

- **Sync runs are recorded.** Every attempt by `garmin-sync` and `wahoo-sync`
  writes a `sync_runs` row — before the work, so a killed process still leaves
  a trace, and again with the outcome and what it counted. A new
  `get_sync_status` tool reports it per source, and `get_training_week`'s
  `coverage` block carries the same answer beside the gaps it explains.
  Until now the only evidence a sync had run was the newest data row, which
  cannot tell a worker that ran and found nothing from one that has been dead
  for a week: both look like a rest week.

- Repository workflow: issue templates, PR template, CODEOWNERS, dependabot,
  and `docs/WORKFLOW.md` describing the risk tiers and who merges what.
- A separate agent identity, `soma-sentry[bot]`, authenticating as a GitHub
  App. Because the agent no longer authors as the owner, a code-owner review
  requirement is satisfiable by the owner and unsatisfiable by the agent, which
  turns tier 3 from a convention into something GitHub can enforce.
- `clock.py`: one place decides what "today" is, and the answer is UTC.
- LICENSE (MIT) and this changelog.

- `ingest/wahoo/`: OAuth CLI, client and sync for the Wahoo Cloud API, plus a
  `wahoo-sync` service in compose. Wahoo is the source of truth for virtual and
  trainer rides — Garmin records none of them — so this is where training load
  comes from rather than a refinement of it.

### Changed

- **The sleep between sync runs became `soma-sync-wait`.** It returns early
  when a request is queued, which is what makes `request_sync` mean anything —
  a plain `sleep` could not be interrupted, so an ask at 21:00 would have waited
  for 08:00. The fixed-hour arithmetic moved out of four lines of shell and into
  Python along with it, where it has tests for the boundaries that bite: 07:59
  waits for today, 08:00 exactly goes to tomorrow, and a DST change moves the
  run with the clock. Behaviour and `SOMA_GARMIN_SYNC_AT` are unchanged.
- **The vendor-isolation guard reads imports instead of scanning text.** It
  missed `garth` and `curl_cffi`, and failed on any docstring containing the
  word "Wahoo" — so no tool could describe ingestion. It now parses the AST of
  both `serve/server.py` and `serve/queries.py`, which is stricter in the
  direction that matters and no longer fooled by prose.

- **`garmin-sync` runs at a fixed local hour, 08:00 by default**
  (`SOMA_GARMIN_SYNC_AT`), instead of every `SOMA_GARMIN_INTERVAL_S` seconds.
  An interval is not a schedule: it began wherever the container last started,
  slipped forward by each run's own duration, and would have moved an hour at
  the next DST change — in practice the nightly sync had settled on 23:40 local
  by accident. 08:00 also lands after Garmin finalises a night's sleep and HRV,
  which is when the day's most valuable signals first exist. The worker now
  logs `next garmin-sync at ...` after each run. `wahoo-sync` stays hourly.

### Fixed

- **"Today" depended on where the process ran.** No timezone was configured, so
  `date.today()` returned the container's UTC date on the Pi and a different
  date on a developer machine. Since date is the join key, `log_nutrition` and
  `log_body` with no explicit day could write to the wrong row — uncorrectably,
  as the tools are upsert-only — and `get_training_week()` could return the
  wrong week. Surfaced by ruff's `DTZ` rules on an 0.16 upgrade.
- `_parse_dt` converted Garmin epoch millis using the process's local zone, so
  the same payload produced a different date depending on the host.

## [0.1.0] - 2026-08-24

First working system. Rides, sleep, HRV, food and body measurements in one
SQLite database, correlated by date and served to Claude over MCP.

### Added

- `models.py` schema keyed on date, with WAL-mode SQLite in `db.py`.
- `ingest/garmin/` — auth CLI with a disk-persisted rate-limit cooldown, sync
  worker, and `garmin-remap` for recovering fields from stored raw payloads.
- `metrics.py` — CTL, ATL, TSB and the weekly ramp.
- `serve/` — six MCP tools, four read and two write, over stdio and HTTP, with
  Google OAuth and an email allowlist. `get_training_week` returns one merged
  week so the join happens in the database rather than in a context window.
- Verification: 175 tests, `make smoke` driving the app over real HTTP,
  `make reach` probing third-party endpoints without credentials, and
  `make contract` failing when a garminconnect method the sync worker calls
  disappears.
- Deployment: multi-architecture image, `compose.pi.yaml`, and
  `deploy/RASPBERRY_PI.md`.

### Known limitations

- `activities.tss` holds Garmin's EPOC-derived training load, not true TSS.
- VO2 max and Garmin training status are stored only in `daily_health.raw`.
- The Docker image has never been built. No Docker daemon was available.

[Unreleased]: https://github.com/aidiaz/soma/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/aidiaz/soma/releases/tag/v0.1.0
