"""Call any traindb MCP tool over HTTP with the bearer token — a quick test client.

Usage (from the project root, with the server running):
    uv run python scripts/probe.py                          # lists tools
    uv run python scripts/probe.py get_training_week
    uv run python scripts/probe.py get_training_week '{"week_start": "2026-03-09"}'
    uv run python scripts/probe.py get_health_trend '{"days": 21}'
    uv run python scripts/probe.py get_recent_activities '{"n": 5}'
    uv run python scripts/probe.py log_body '{"weight_kg": 72.0, "waist_cm": 84.0}'

Token is read from $TRAINDB_API_TOKEN or the .env file; URL from $TRAINDB_MCP_URL
(default http://127.0.0.1:8000/mcp).

Bearer-token only, so it probes a local server. The deployed server uses Google
OAuth, where the client does the sign-in.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def _token() -> str:
    tok = os.environ.get("TRAINDB_API_TOKEN")
    if tok:
        return tok
    env = pathlib.Path(".env")
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("TRAINDB_API_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("No TRAINDB_API_TOKEN in environment or .env")


async def main() -> None:
    tool = sys.argv[1] if len(sys.argv) > 1 else None
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    url = os.environ.get("TRAINDB_MCP_URL", "http://127.0.0.1:8000/mcp")

    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {_token()}"})
    async with Client(transport) as client:
        if tool is None:
            tools = await client.list_tools()
            print("Available tools:")
            for t in sorted(tools, key=lambda x: x.name):
                print(f"  {t.name:24} {(t.description or '').splitlines()[0]}")
            return
        result = await client.call_tool(tool, args)
        print(json.dumps(result.data, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
