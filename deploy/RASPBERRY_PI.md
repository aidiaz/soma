# Deploying to a Raspberry Pi

The shape:

```
Claude ──► https://soma.example.dev ──► [Cloudflare Tunnel] ──► Pi (Docker)
                                                                   ├─ server        (MCP, reads SQLite)
                                                                   ├─ garmin-sync   (nightly, talks to Garmin)
                                                                   └─ cloudflared   (dials out)
```

The server publishes `127.0.0.1:8181` on the Pi — reachable from the Pi itself
or over SSH, and from nowhere else. The tunnel dials outward, so there is no inbound
firewall rule, no port forward, and no public IP.

## 1. Google OAuth client

Google Cloud console → APIs & Services → Credentials → **Create OAuth client ID**
→ Web application.

- Authorized redirect URI: `https://<your-host>/auth/callback`
- Keep the app **Published / In production**, or only test users can sign in.

Copy the client ID and secret into `.env`.

Google sign-in proves *who* a caller is. It does not decide whether they are
allowed — `SOMA_ALLOWED_EMAILS` does. The server refuses to start with OAuth
enabled and that list empty.

## 2. Cloudflare Tunnel

Zero Trust → Networks → Tunnels → **Create tunnel** (Cloudflared).

1. Route the public hostname to `http://server:8000`.
2. Copy the tunnel token into `.env` as `CLOUDFLARE_TUNNEL_TOKEN`.
3. DNS is created for you if the domain is already on Cloudflare.

**Do not add a Cloudflare Access policy to this hostname.** The MCP OAuth
handshake fetches `/.well-known/oauth-protected-resource/mcp` and
`/.well-known/oauth-authorization-server` *before* it has any credential.
Access answers those with an HTML login page, and the client gives up before the
OAuth flow starts. The email allowlist is the gate; Access is not.

Worth turning on at the edge: **Full (strict)** TLS, and a rate-limiting rule on
the hostname.

## 3. `.env`

Copy `.env.example` to `.env` and fill in. The values that matter here:

```bash
SOMA_BASE_URL=https://soma.example.dev      # pins the OAuth redirect
SOMA_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
SOMA_GOOGLE_CLIENT_SECRET=GOCSPX-...
SOMA_JWT_SIGNING_KEY=...                      # openssl rand -hex 32
SOMA_ALLOWED_EMAILS=you@example.com
SOMA_GARMIN_EMAIL=you@example.com              # the Garmin account to read
CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```

`SOMA_BASE_URL` is pinned rather than derived from `x-forwarded-host`, so a
spoofed header cannot redirect the OAuth flow somewhere else.

## 4. First Garmin login

The sync worker only ever logs in with saved tokens — it never sends a password,
because a credential login is what risks the per-account lockout. So the token
store has to be seeded once, interactively:

```bash
docker compose -f compose.pi.yaml run --rm garmin-sync garmin-auth
```

It prompts for the password and the MFA code, then writes tokens to the
`soma-data` volume. Expect to repeat this roughly every six months.

**Garmin rate-limits logins per account, not per IP.** Changing network or user
agent does nothing; only time clears it. `garmin-auth` refuses to retry inside a
15-minute cooldown for that reason. If you see a 429, wait — do not loop.

## 5. Bring the stack up

```bash
docker compose -f compose.pi.yaml up -d
docker compose -f compose.pi.yaml ps
docker compose -f compose.pi.yaml logs -f server
```

The first sync backfills a year and takes a while — roughly five requests per
day of history, paced deliberately. It is resumable: re-running with
`--skip-existing` picks up where a 429 stopped it.

To backfill by hand:

```bash
docker compose -f compose.pi.yaml run --rm garmin-sync garmin-sync --days 365 --skip-existing
```

## 6. Connect Claude Code

```bash
claude mcp add --transport http soma https://soma.example.dev/mcp
```

No `--client-id` and no `--callback-port`: the server publishes a
`registration_endpoint`, so the client registers itself dynamically. The first
tool call opens a Google sign-in.

## 7. Checking on it

The `coverage` block on `get_training_week` is the honest answer to "is my data
current?" — it names the days it has no data for. A nightly sync that quietly
died looks exactly like a week of rest days until you look at that list.

```bash
docker compose -f compose.pi.yaml logs --tail 50 garmin-sync
docker compose -f compose.pi.yaml exec server python -c \
  "import json; from soma.serve.queries import get_training_week; \
   print(json.dumps(get_training_week()['coverage'], indent=2))"
```

## 8. Updating

Today the Pi pulls. CI builds a multi-architecture image and pushes it to ghcr;
the Pi either gets restarted by hand or polls with the optional `watchtower`
profile:

```bash
docker compose -f compose.pi.yaml pull && docker compose -f compose.pi.yaml up -d
```

Watchtower is a stopgap: it polls, and it gives no logs in Actions, no approval
step and no rollback. The intended replacement is a self-hosted runner on the Pi
so deploys are a real job. That work is still open — see CLAUDE.md.

## Backups

`soma-data` holds the only copy of the database and the Garmin tokens.

```bash
docker run --rm -v soma_soma-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/soma-data-$(date +%F).tar.gz -C /data .
```

Every table also keeps the untouched Garmin payload in a `raw` column, so if a
field name changes upstream the old value is recoverable from `raw` — a resync
is not needed, and after Garmin has aged data out it may not even be possible.
