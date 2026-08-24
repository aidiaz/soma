# How work moves through this repository

This describes who decides what, and where a human's attention is spent. It is
not process for its own sake: the repository is worked by an agent that can
produce a plausible diff faster than anyone can read one, so the question the
whole design answers is *which diffs are worth reading*.

## The loop

```
  OWNER                            AGENT                        CI
  ─────                            ─────                        ──
  file issue ──┐
               ├─ label agent:ready ──► branch, build
  answer a     │                             │
  decision ────┘                         open PR ─────► lint · test · contract · smoke
                                              │                     │
                   ◄──── review requested ────┘        green/red ───┘
  tier 1: merge on green
  tier 2: skim diff + merge
  tier 3: line by line
```

## Definition of ready

**An issue is ready when someone could write a failing test from it.**

That single rule is what stops an agent-authored PR from drifting into
something nobody asked for: the test is the contract, and it exists before the
implementation does. If the acceptance criteria cannot be stated as something
checkable, the issue is a *decision*, not a task. File it with the Decision
template, label it `needs-decision`, and it belongs to the owner.

The failure this prevents is specific. An issue reading "improve the weekly
summary" produces a diff that is impossible to reject and impossible to accept,
because there is no stated condition it either meets or misses.

## Risk tiers

Tier is decided by **the paths a PR touches**, not by the author's judgement of
how risky it feels.

| Tier | Covers | Owner effort |
|---|---|---|
| **1** | docs, tests, refactors with no behaviour change, dependency bumps | Merge on green. Seconds. |
| **2** | new MCP tool, query, metric, ingestion mapping | Read the PR description and the `make smoke` output. Minutes. |
| **3** | `serve/oauth.py`, `config.py`, `.github/`, `deploy/`, `Dockerfile`, `compose.pi.yaml`, `.env.example`, `scripts/agent_token.sh`, `CLAUDE.md`, this file | Line by line. Never merged by the agent. |

Tier 3 is where a mistake is either invisible or irreversible: an allowlist that
silently admits everyone, a secret in a committed file, a deploy that cannot be
rolled back. Tier 1 and 2 mistakes surface as a failing test or a wrong number
in a weekly review, and both are cheap to fix.

### Two identities, and why it matters

The owner and the agent are **separate GitHub accounts**, and that single fact
is what makes review enforceable.

| | Identity | Used for |
|---|---|---|
| Owner | `aidiaz` | Reviewing, approving, merging, deciding |
| Agent | `traindb-agent[bot]` | Branches, commits, PRs, issue comments |

The agent authenticates as a GitHub App installation, minting a one-hour token
per operation with `scripts/agent_token.sh`. The App's private key lives at
`~/.config/traindb/agent-app.pem`, outside the repository, and the script
refuses to run if it is readable by anyone but its owner.

It is deliberately **not** wired into a git credential helper. That would
rewrite the owner's own pushes as the bot, collapsing the two identities back
into one and undoing the point.

### What actually enforces tier 3

Be honest about the mechanism, because a gate that does not hold is worse than
no gate — it is a gate you stop checking.

- **`.github/CODEOWNERS` blocks.** GitHub does not let the author of a PR
  approve it. Because the agent authors as `traindb-agent[bot]` and the owner
  reviews as `aidiaz`, "require review from Code Owners" is satisfiable in the
  normal way — and unsatisfiable by the agent alone. This is the primary gate.
- **`.github/workflows/tier-gate.yml` is the visible check.** It compares the
  changed paths against the tier 3 list and fails unless the PR carries the
  `reviewed:tier3` label. It is now a second signal rather than the only one:
  red on the checks list is easier to notice than a missing approval.
- **The rule the agent follows: never apply `reviewed:tier3`.** The App's token
  can technically apply it. Only this rule prevents that, and the label event
  names who applied it — so the audit trail survives even if the rule does not.

### Both gates are advisory, and that is a decision rather than an oversight

Branch protection and rulesets are **not available on this repository**. Checked
on 2026-08-24, not assumed:

```
GET /repos/aidiaz/traindb/rulesets                   403
GET /repos/aidiaz/traindb/branches/main/protection   403
"Upgrade to GitHub Pro or make this repository public to enable this feature."
```

So nothing here can stop a merge: not a red `tier-gate`, not a missing
code-owner approval. The owner decided on 2026-08-24 to accept that rather than
pay for Pro or make the repository public — see #6, which stays open as the
trigger if either changes.

What follows is the only rule that matters while this holds:

> **Do not merge past a red `tier-gate` or a missing approval.** The gate is
> discipline, not machinery. The moment it is bypassed once out of convenience,
> it stops being read at all, and everything above becomes decoration.

The design is worth keeping anyway, because turning enforcement on later is a
settings change and no code change.

## Labels

| Label | Meaning |
|---|---|
| `type:task` / `type:bug` / `type:decision` | Set by the issue template. |
| `agent:ready` | The owner has confirmed this can be picked up unattended. **An agent does not apply this to its own issue.** |
| `needs-decision` | Blocked on a human judgement. No agent may close it by choosing. |
| `deps` | Opened by dependabot. |
| `reviewed:tier3` | A human read the diff line by line. Applied by a human, only. |

Run `scripts/setup_labels.sh` once to create them.

## Branches and commits

- Branch from `main`: `feat/`, `fix/`, `chore/`, `docs/` + a short slug.
- One issue per PR. A PR closing two issues is two PRs.
- The PR body copies the issue's acceptance criteria as a checklist, and ticks
  only what the diff actually delivers. An unticked box is information, not a
  failure.

## What CI proves, and what it does not

`make ci` runs lint, tests, the API contract check, and the HTTP smoke.

`make smoke` exists because `make test` uses an in-process ASGI client and
therefore cannot see a lifespan fault, a mount-order fault, or a handler
returning 200 while logging an exception. It is falsification-tested: a wrong
bearer token and a planted `ERROR` line each make it exit non-zero.

Neither proves the Docker image builds — it never has been built — nor that
anything works on the Pi. Those are deploy-time checks, and they are the owner's.

## Never delegated

These are the owner's by nature, not because of a capability limit:

- Registering the Google OAuth client; any handling of secrets or the tunnel
  token.
- `garmin-auth` — interactive, MFA-gated, and rate-limited **per account**, so a
  failed retry costs real waiting time.
- Approving a deploy to the Pi.
- Product judgement. What *should* the ramp threshold be before a week is called
  too much? That is not discoverable from the codebase.
- Applying `reviewed:tier3`.

## The owner's week

Roughly twenty minutes, in this order:

1. **Triage.** Label new issues `agent:ready`, or answer the one question
   blocking them.
2. **Review.** Merge tier 1 on green, skim tier 2, read tier 3 line by line.
3. **Decide.** Work the `needs-decision` queue. Open ones today: the real name
   (before the first OAuth redirect URI is registered — that is the one-way
   door), the Garmin/Wahoo TSS dedupe rule, and whether Phase 5 happens at all.

## The risk this process does not address

The proposal says it plainly: building this is more fun than logging breakfast,
and the system is worthless without the logging. No amount of workflow fixes
that. `log_nutrition` exists to make logging cheap, not to make it optional.
