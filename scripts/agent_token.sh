#!/usr/bin/env bash
# Mints a short-lived GitHub App installation token, so an agent pushes and
# opens pull requests under its own identity rather than the owner's.
#
# Why this exists: with one identity, "the author wrote it" and "the reviewer
# approved it" are the same GitHub event, and GitHub refuses to let an author
# approve their own PR. Separate identities make CODEOWNERS a real merge
# blocker instead of a convention.
#
# Deliberately not wired into a git credential helper. That would rewrite the
# owner's own pushes as the bot, which destroys the distinction this creates.
# Callers opt in explicitly:
#
#   GH_TOKEN=$(scripts/agent_token.sh) gh pr create ...
#   git push "https://x-access-token:$(scripts/agent_token.sh)@github.com/${REPO}.git" HEAD
#
# Commits must carry the bot identity too, or the PR is the bot's while every
# commit inside it is still the owner's:
#
#   git -c user.name='traindb-agent[bot]' \
#       -c user.email='320682002+traindb-agent[bot]@users.noreply.github.com' commit ...
#
# Tokens expire after one hour. Mint one per operation; never store one.
set -euo pipefail

repo="${TRAINDB_AGENT_REPO:-aidiaz/traindb}"
app_id="${TRAINDB_AGENT_APP_ID:-4704941}"
key="${TRAINDB_AGENT_KEY:-$HOME/.config/traindb/agent-app.pem}"

[ -r "$key" ] || { echo "private key not readable: $key" >&2; exit 1; }

# The key is the whole of the App's authority. Refuse to run if it is group- or
# world-readable, because the usual way this leaks is a permissive default.
perms=$(stat -f '%Lp' "$key" 2>/dev/null || stat -c '%a' "$key")
case "$perms" in
  600|400) ;;
  *) echo "private key $key is mode $perms; chmod 600 it" >&2; exit 1 ;;
esac

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

now=$(date +%s)
# iat backdated 60s to tolerate clock skew; GitHub rejects a JWT older than 10
# minutes, so 9 is the longest safe expiry.
header=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$app_id" | b64url)
signed="${header}.${payload}"
sig=$(printf '%s' "$signed" | openssl dgst -sha256 -sign "$key" -binary | b64url)
jwt="${signed}.${sig}"

api() { curl -sS -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" "$@"; }

installation=$(api "https://api.github.com/repos/${repo}/installation" | jq -r '.id // empty')
[ -n "$installation" ] || {
  echo "no installation of app ${app_id} on ${repo}. Install the App on the repo." >&2
  exit 1
}

token=$(api -X POST "https://api.github.com/app/installations/${installation}/access_tokens" \
  | jq -r '.token // empty')
[ -n "$token" ] || { echo "failed to mint an installation token" >&2; exit 1; }

printf '%s\n' "$token"
