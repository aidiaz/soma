# CLAUDE.md

Context for Claude Code sessions in this repository.

## What this is

A personal training data system. Rides, sleep, HRV, food and body measurements
in one SQLite database, correlated by date, served to Claude over MCP. It is not
related to the strongbyform projects.

Deployment target: a Raspberry Pi behind a Cloudflare Tunnel, with Google OAuth
and an email allowlist.

**`soma` is a working name.** Replace it before the first OAuth redirect URI
is registered — that is the point of no return, and it has not been reached.

## The rule that shapes everything

The database does the join, not the LLM. `get_training_week` returns one merged
week; the analysis layer only reasons over it. Do not add a tool that returns a
slice the caller then has to correlate by hand — that pushes the join back into
a context window, which is the problem this replaced.

## Architecture: separate by layer, not by vendor

```
src/soma/
  models.py            schema; date is the join key
  clock.py             what "today" means; UTC, in one place
  db.py                SQLite engine + WAL pragmas
  metrics.py           CTL/ATL/TSB and the weekly ramp
  sync_requests.py     the on-demand sync queue; the only server-to-worker path
  ingest/garmin/       auth CLI, client, sync, remap
  ingest/wahoo/        OAuth CLI, client, sync — trainer rides live here
  ingest/schedule.py   the wait between runs: next fixed hour, or a request
  serve/               queries, MCP tools, HTTP app, OAuth allowlist
```

`ingest/wahoo/` sits beside `ingest/garmin/`, and adding it changed nothing in
`serve/` — which was the point of splitting by layer rather than by vendor.

**The MCP server never contacts a vendor. It reads SQLite and writes three
tables.** Do not add a tool that calls the Garmin or Wahoo API. The reasons:

- only the sync workers can trip a vendor rate limit;
- tool calls stay fast;
- when a vendor breaks, the server keeps serving stored history.

`test_server_factory.py` asserts this by parsing the imports of `serve/server.py`
and `serve/queries.py`, so the rule fails CI rather than review. It reads the
AST rather than scanning the text, which it used to do: the text scan missed
`garth` and `curl_cffi` and tripped over a docstring that merely said "Wahoo".

**When a tool needs work done at a vendor, it queues it.** `request_sync` is the
pattern: it appends to `sync_requests` and returns, and the worker that already
holds that vendor's credential picks the row up within `SOMA_SYNC_POLL_S`. The
table is the entire interface between the two containers — no port, no socket,
no Docker API — and it is the third table the server writes. Copy this shape
rather than reaching for a vendor client; the rule above is what makes a broken
vendor a broken *sync* rather than a broken server.

## Facts verified against the installed libraries

Do not "correct" these without re-checking.

- `garth` was deprecated in March 2026. Do not reintroduce it. `make contract`
  fails if it reappears.
- `garminconnect` 0.3.11 is the foundation. Signatures:
  `Garmin(email, password, prompt_mfa=...)` and `login(tokenstore)`.
- **`garminconnect` 0.3.11 requires Python >= 3.12.** So does this package, and
  CI tests 3.12 and 3.13 only. 3.11 is not available to us.
- Garmin's SSO rate limit is **per account, not per IP**. Only time clears it.
  The auth CLI enforces a cooldown persisted to disk, so it survives the
  operator re-running the command in a new shell — which is how people retry.
- `garminconnect` reaches Garmin through `curl_cffi`, which impersonates a
  browser TLS fingerprint. `make contract` checks the dependency is still there;
  `make reach` checks the client works from this host.
- **Corrected 2026-08-24.** This file previously asserted as verified fact that
  `requests` and `httpx` "cannot reach Garmin — TLS fingerprinting blocks them".
  Probing says otherwise: both get HTTP 200 from `sso.garmin.com/sso/signin` and
  `connect.garmin.com`, and all three clients — `curl_cffi` included — get 403
  from `connectapi.garmin.com` unauthenticated. Whatever blocking exists is not
  visible at any endpoint reachable without credentials. The login POST is where
  bot detection would bite, and that cannot be tested without an account. So the
  reason not to swap the HTTP client is simply that `garminconnect` chooses it
  internally — not a measured block. Do not restate the original claim.
- FastMCP `GoogleProvider` has **no email allowlist**. Google OAuth proves
  identity, not authorisation. `serve/oauth.py` is what keeps other people out,
  and `build_auth` raises rather than build a provider when the list is empty.
- `required_scopes` must be the email scope **only**. Google's access-token
  introspection does not reliably report `openid` as granted, so including it
  makes verification fail *after* an otherwise successful sign-in.
- The MCP OAuth flow needs `/.well-known/oauth-protected-resource/mcp` and
  `/.well-known/oauth-authorization-server` reachable **without a login**.
  Therefore: **do not put Cloudflare Access in front of `/mcp`.** There is a
  test asserting both paths answer 200 unauthenticated.
- The server publishes a `registration_endpoint`, so dynamic client registration
  works. Claude Code needs no `--client-id` or `--callback-port`.
- The Google redirect URI to register is `<SOMA_BASE_URL>/auth/callback`.

## Bugs already fixed. Do not reintroduce them.

1. **Empty-day placeholder rows.** Garmin returns an empty payload for a day
   with no watch data. Writing it created a row holding only a date, which
   skewed every weekly average and made the coverage report claim days that hold
   nothing. `ingest/garmin/sync._write()` guards this and is the only place that
   writes — `remap.py` goes through it too, so a mapping regression cannot
   flatten a good row into a placeholder.
2. **Shared FastMCP singleton.** `create_app()` used to mutate a module-level
   `mcp`, so building the HTTP app silently broke the stdio server.
   `serve/server.build_mcp()` is a factory. There is a regression test.
3. **Rollback journal mode.** The sync worker and the server are separate
   containers on one database file. Without WAL a reader blocks a writer.
   `db.connect()` sets WAL, `busy_timeout` and `synchronous=NORMAL`.
4. **"Today" resolved by whatever zone the process ran in.** Nothing set a
   timezone, so `date.today()` answered differently on the Pi (a container,
   therefore UTC) than on a laptop. Because date is the join key, that decided
   which row a write landed on: `log_nutrition` with no `day` could record
   dinner against tomorrow, and the tools are upsert-only, so it could not be
   corrected through the interface. `clock.today()` is now the only answer.
   Found by ruff's `DTZ` rules, which is the argument for adopting them.

   **The zone was UTC for one afternoon, then reversed to local (#33).** The
   reversal is deliberate and not a correction of a mistake: UTC was chosen with
   its cost written down — an evening meal west of UTC lands on tomorrow — and
   that cost was acceptable while the system held only training data, where it
   bites occasionally. Intake tracking makes it constant, because evening is
   when people eat. `SOMA_TIMEZONE` now decides, validated at startup so a typo
   fails loudly rather than at the first write. `clock.now()` stays an aware UTC
   instant: timestamps are absolute, only the calendar day is local.

   Note what the fix is *not*: `ingest/garmin/sync._parse_dt` stays naive on
   purpose, because Garmin's `startTimeLocal` is what makes a 23:30 ride count
   as that day's training, and `ingest/wahoo/sync` derives each activity's date
   from the zone it happened in. A ride done while travelling belongs to the day
   it happened *there*, which can differ from `clock.today()`. Both are right.
5. **A field named `date` shadowing the `date` type.** `models.py` imports
   `datetime as dt` and annotates `dt.date` for this reason. Reverting to
   `from datetime import date` makes every model fail to build at import.

## Known compromises

- **`activities.tss` holds two different quantities, by source.** Wahoo rows
  carry true TSS (`power_bike_tss_last`); Garmin rows carry its EPOC-derived
  training load. They are different quantities on a similar scale. Ramp is a *ratio*,
  so it survives a consistent scale — there is a test pinning that — but two
  things follow: absolute TSS should not be compared against published plans,
  and a week mixing both sources reports a ramp that is partly an artefact of
  the mix. In practice Garmin contributes no rides at all, so today every row
  is Wahoo's and the scale is consistent. `metrics._dedupe` guards the case
  where that stops being true.
- **VO2 max and Garmin's training status have no typed columns.** The schema has
  no home for them. They are still fetched and stored in `daily_health.raw`,
  because dropping them is irreversible — Garmin ages data out, so a later
  decision to want them back could not be honoured for history. Two extra calls
  a night is a cheap option to hold.

## Working on this

```bash
make install
make test      # 298 unit/integration tests
make smoke     # drives the app over real HTTP and scans its log
make reach     # probes third-party endpoints, no credentials
make lint      # ruff check + format check
make contract  # confirms Garmin endpoints still exist
make ci        # all of the above
```

Every change must keep `make ci` green.

`make smoke` exists because `make test` uses an in-process ASGI client and so
cannot see a lifespan fault, a mount-order fault, or a handler returning 200
while logging an exception. It also asserts the empty-state honesty rules: absent
health days must be explicit nulls, and a ramp with no prior load must be null
rather than zero. Point it at a container or the Pi with `--url/--token/--log`.

It is falsification-tested: a wrong bearer token and a planted `ERROR` log line
each make it exit non-zero with a reported failure rather than a traceback.
Those two were verified; the other failure paths were not. Keep it falsifiable —
a verification loop that cannot fail is decoration.

When testing the log check by hand, do not append to a log a running server
still holds open. Its fd offset is behind end-of-file, so its next write
overwrites the planted line and the check appears to miss it. Use a static file.

`scripts/check_api_contract.py` exists because the sync worker degrades quietly
when a library method disappears. That is right at runtime and dangerous in CI,
because a silent gap looks like a normal rest day. It derives the method list by
parsing `ingest/garmin/sync.py`, so a new endpoint gets coverage automatically.
Keep it loud.

Ingested rows keep the full vendor response in `raw`. When a field name changes,
recover the value with `garmin-remap` rather than resyncing.

## Deployment

`deploy/RASPBERRY_PI.md` holds the procedure. Summary:

- `compose.pi.yaml` runs `server`, `garmin-sync`, `cloudflared`, and optionally
  `watchtower` (behind the `autoupdate` profile). The server publishes
  `127.0.0.1:8181` — loopback only. Dropping the `127.0.0.1:` prefix would
  publish on every interface and let anyone on the same wifi reach the server
  directly, bypassing Cloudflare, the Google sign-in and the allowlist. Compose has no scheduler, so the sync service is a shell loop.
- `.github/workflows/ci.yml` lints, tests on 3.12 and 3.13, builds a
  multi-architecture image and pushes to ghcr. The contract check also runs
  weekly on a cron.

## Build order and open work

Phases 2 and 4 of the proposal are built: the serving layer and the write tools,
over Garmin data. The proposal's own order put Wahoo first, but that argument
was about shrinking the fragile surface *before* paying for it — Garmin was
already built and tested, so the order inverted.

1. **Phase 1 — Wahoo ingestion. Built 2026-08-24.** `ingest/wahoo/` holds an
   OAuth CLI, a client and a sync; `wahoo-sync` runs hourly in compose. No
   webhook receiver: the app has webhooks disabled, so ingestion polls, which
   also means the server never needs to be publicly reachable for it.
   Still to do: a `/callback` route so re-authorising does not mean reading a
   code out of a container log, and mapping scheduled workouts into
   `planned_workouts`.
2. **Phase 5 — calendar sync. Unblocked, not built.** Scheduled SYSTM sessions
   *are* readable — `/v1/workouts` returns them dated weeks ahead, and
   `wahoo-sync` already skips them rather than inventing training that never
   happened. The assumption that `suffersync` using raw credentials meant plans
   were unreachable was wrong. `planned_workouts` and `calendar_sync` remain
   unused; mapping into them is the next piece.
3. **Open questions from the proposal — answered 2026-08-24, against the live
   API rather than the docs.**
   - *Full 4DP, or only FTP and zones?* **Only FTP and zones.** No `NM`, `AC` or
     `MAP` on any endpoint. 4DP stays inside SYSTM.
   - *Are SYSTM plans reachable?* **Yes.** `/v1/workouts` returns scheduled
     sessions alongside completed ones, dated weeks ahead, and `plans_read` is
     granted. Phase 5 is not blocked after all.
   - *Does a personal-use app get approved?* **Approval is not needed for your
     own account.** The app sits in `sandbox` and issues working tokens for the
     owner. Production approval is for letting *other people* authorise.
   - *Does a SYSTM ride feed Garmin's Body Battery?* **It does not arrive at
     all.** A 15-day Garmin sync returned zero activities while Wahoo held the
     same period's riding. That is what put Wahoo on the critical path (#3).
   - Still open: whether Garmin's `sleep` and `hrv` payloads are empty because
     the watch is not capturing them or because the API is not returning them.
     Both came back empty for all 15 days; `raw` holds the evidence.
4. **Image verified 2026-08-24, on `linux/arm64` — the Pi's architecture.** It
   builds; `curl_cffi` 0.15.0 imports on aarch64, which was the risk; all seven
   console scripts resolve bare, which is what the `PATH` line in the
   `Dockerfile` exists to guarantee. Under compose the server reaches `healthy`
   in six seconds with no restarts, publishes only `127.0.0.1:8181`, and passes
   all 39 smoke assertions. The empty-allowlist fail-safe was exercised in the
   image: OAuth on with no allowlist exits 1 rather than serving, and with an
   allowlist it builds `_AllowlistedGoogle` scoped to the email scope alone.
   **Still unexercised: `garmin-sync`.** Running it would spend a real Garmin
   login, and that rate limit is per account with only time clearing it. It has
   never run in a container. Verify it on the Pi, where a cooldown costs less.
5. Repository hygiene landed: LICENSE, CHANGELOG, dependabot, issue and PR
   templates, CODEOWNERS, and a separate agent identity (`soma-sentry[bot]`).
   **Branch protection is unavailable** — a free private repo returns 403 from
   the rulesets and protection endpoints. Decided 2026-08-24 to accept advisory
   gates rather than pay for Pro or go public (#6). So `tier-gate` and
   code-owner approval inform; neither blocks a merge. Do not merge past a red
   check.

**The real risk is not technical.** The proposal says it plainly: building this
is more fun than logging breakfast, and the system is worthless without the
logging. `log_nutrition` exists to make the logging cheap, not to make it
optional.

## How work moves

`docs/WORKFLOW.md` is the process, and it binds an agent working in this repo.
The parts that change what you do:

- **An issue is ready when you could write a failing test from it.** If you
  cannot, it is a decision, not a task. Do not close it by choosing — label it
  `needs-decision` and hand it back.
- **Tier is decided by the paths a PR touches**, not by how risky the change
  feels. The list is `.github/tier3-paths.txt` — read it there rather than
  trusting a copy, because a copy is what this line used to be.
- **Never apply `reviewed:tier3`, never approve a PR, and never apply
  `agent:ready` to your own issue.** Each is a human stating they checked
  something. Doing it yourself makes the audit trail a lie.
- **Push and open PRs as the bot, not as the owner.** Mint a token with
  `scripts/agent_token.sh` and set the commit identity explicitly; the script
  header carries the exact invocation. A PR authored by `aidiaz` cannot be
  approved by `aidiaz`, so getting this wrong makes the change unmergeable
  without weakening the gate.

## Style

Prose and comments: short, active sentences. No emoji. Explain why a decision
was made, not what the line does. State uncertainty rather than guessing, and
offer an alternative when a choice is not clear-cut.

For FastMCP OAuth, Cloudflare Tunnel and deploy patterns, `../leychile-rag/` is
the working reference — same FastMCP version, same Pi, same tunnel setup.
