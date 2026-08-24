#!/usr/bin/env bash
# Creates the label taxonomy docs/WORKFLOW.md depends on. Idempotent: re-running
# updates colour and description rather than failing.
#
# Labels are not decoration here. `agent:ready` is the handoff signal and
# `reviewed:tier3` is the tier 3 gate, so both must exist before the workflow
# means anything.
set -euo pipefail

repo="${1:-aidiaz/traindb}"

label() {
  gh label create "$1" --repo "$repo" --color "$2" --description "$3" --force >/dev/null
  echo "  $1"
}

echo "Labels on $repo:"
label "type:task"       "0e8a16" "Work with a clear outcome"
label "type:bug"        "d73a4a" "Something behaves incorrectly"
label "type:decision"   "5319e7" "A question a human must answer"
label "agent:ready"     "1d76db" "Can be picked up unattended by an agent"
label "needs-decision"  "fbca04" "Blocked on human judgement; no agent may choose"
label "deps"            "c5def5" "Dependency update"
label "reviewed:tier3"  "b60205" "A human read this diff line by line. Applied by a human, only."
