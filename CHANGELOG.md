# Changelog

Notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

An entry earns its place by changing what an operator or a caller sees. A
refactor with no behaviour change does not belong here — the git log already
holds it.

## [Unreleased]

### Added

- Repository workflow: issue templates, PR template, CODEOWNERS, dependabot,
  and `docs/WORKFLOW.md` describing the risk tiers and who merges what.
- A separate agent identity, `traindb-agent[bot]`, authenticating as a GitHub
  App. Because the agent no longer authors as the owner, a code-owner review
  requirement is satisfiable by the owner and unsatisfiable by the agent, which
  turns tier 3 from a convention into something GitHub can enforce.
- `clock.py`: one place decides what "today" is, and the answer is UTC.
- LICENSE (MIT) and this changelog.

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

[Unreleased]: https://github.com/aidiaz/traindb/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/aidiaz/traindb/releases/tag/v0.1.0
