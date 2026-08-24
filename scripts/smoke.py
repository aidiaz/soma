"""End-to-end verification: drive the running app over real HTTP and assert what comes back.

`make test` exercises Python functions and an in-process ASGI client. That misses
everything between: the uvicorn lifespan, the route mount order, the real auth
middleware, the streamable-HTTP transport, and whether the process logged an
exception while returning 200. This drives the app the way a client does, then
reads the log to check it did so quietly.

Three targets, one script:

    make smoke                      # starts its own server on a temp database
    python scripts/smoke.py --url http://127.0.0.1:8000/mcp --token "$TOK" --log server.log
    python scripts/smoke.py --url https://host/mcp --token "$TOK"    # no log check

Exits non-zero on the first failure category, and prints every check either way.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from soma.clock import today as utc_today

EXPECTED_TOOLS = {
    "get_training_week",
    "get_health_trend",
    "get_recent_activities",
    "get_tests",
    "log_nutrition",
    "log_body",
}

# Distinctive enough that a stale database shows up as a mismatch rather than a
# coincidental pass.
KCAL, PROTEIN = 2345, 187.5
WEIGHT_KG, WAIST_CM = 71.4, 83.2

# Log lines that are correct and expected. Anything else at WARNING or above is
# a finding — a 200 response with an exception behind it is exactly the failure
# this script exists to catch.
ALLOWED_LOG_PATTERNS = [
    re.compile(r"Auth: static bearer token"),
    re.compile(r"Auth: Google OAuth"),
]
LOG_PROBLEM = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception|FAIL)\b")


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.section = ""

    def start(self, section: str) -> None:
        self.section = section
        print(f"\n{section}")

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [ok  ] {name}" + (f" — {detail}" if detail else ""))
        else:
            self.failed.append(f"{self.section} / {name}: {detail}")
            print(f"  [FAIL] {name} — {detail}")
        return ok

    def eq(self, name: str, actual: Any, expected: Any) -> bool:
        return self.check(name, actual == expected, f"got {actual!r}, expected {expected!r}")


def start_server(log_path: pathlib.Path, port: int, token: str, db_path: pathlib.Path):
    """Launch a real uvicorn process against a throwaway database."""
    env = {
        **os.environ,
        "SOMA_OAUTH_ENABLED": "false",
        "SOMA_API_TOKEN": token,
        "SOMA_HOST": "127.0.0.1",
        "SOMA_PORT": str(port),
        "SOMA_DB_PATH": str(db_path),
        "SOMA_GARMIN_TOKENSTORE": str(db_path.parent / "tokens"),
    }
    handle = log_path.open("w")
    return subprocess.Popen(
        [sys.executable, "-m", "soma.serve.app"],
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def wait_for_health(base: str, timeout_s: int = 45) -> bool:
    deadline = time.time() + timeout_s
    last: Exception | None = None
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=3).status_code == 200:
                return True
        except Exception as exc:  # noqa: BLE001 - not up yet is the normal case here
            # Kept rather than swallowed. A connection refused on every attempt
            # and a TLS failure on every attempt both time out identically, and
            # the log alone does not distinguish them — the process may never
            # have got far enough to write one.
            last = exc
        time.sleep(0.5)
    if last is not None:
        print(f"  last error contacting {base}/health: {last!r}", file=sys.stderr)
    return False


def check_transport_and_auth(r: Results, base: str, token: str) -> None:
    r.start("transport & auth")
    try:
        health = httpx.get(f"{base}/health", timeout=10)
        r.check(
            "health answers 200 without a token",
            health.status_code == 200,
            f"HTTP {health.status_code}",
        )
        r.eq("health body", health.json(), {"status": "ok"})
    except Exception as exc:  # noqa: BLE001
        r.check("health reachable", False, f"{type(exc).__name__}: {exc}")

    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    unauth = httpx.post(f"{base}/mcp", json=call, timeout=10)
    r.check(
        "unauthenticated /mcp is rejected", unauth.status_code == 401, f"HTTP {unauth.status_code}"
    )

    wrong = httpx.post(
        f"{base}/mcp", json=call, headers={"Authorization": "Bearer not-the-token"}, timeout=10
    )
    r.check("wrong bearer token is rejected", wrong.status_code == 401, f"HTTP {wrong.status_code}")


async def check_tools(r: Results, url: str, token: str) -> None:
    """Drive every tool. A transport or auth failure is reported, not raised.

    An unhandled traceback here would be indistinguishable from the script
    itself being broken. The point of a verification loop is that a failure
    reads as a failed check.
    """
    try:
        await _check_tools(r, url, token)
    except Exception as exc:  # noqa: BLE001 - the report is the output
        r.start("tool surface")
        r.check(
            "MCP session established",
            False,
            f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}",
        )


async def _check_tools(r: Results, url: str, token: str) -> None:
    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        r.start("tool surface")
        tools = await client.list_tools()
        names = {t.name for t in tools}
        r.eq("exactly the expected tools", names, EXPECTED_TOOLS)
        undocumented = [t.name for t in tools if not (t.description or "").strip()]
        r.check("every tool is documented", not undocumented, f"missing: {undocumented}")

        r.start("writes")
        today = utc_today().isoformat()
        food = (
            await client.call_tool(
                "log_nutrition",
                {"day": today, "kcal": KCAL, "protein_g": PROTEIN, "creatine": True},
            )
        ).data
        r.eq("log_nutrition returns the stored kcal", food.get("kcal"), KCAL)
        r.eq("log_nutrition returns the stored protein", food.get("protein_g"), PROTEIN)
        r.eq("log_nutrition records a supplement", food.get("creatine"), True)
        r.eq("log_nutrition stamps the date", food.get("date"), today)

        body = (
            await client.call_tool(
                "log_body", {"day": today, "weight_kg": WEIGHT_KG, "waist_cm": WAIST_CM}
            )
        ).data
        r.eq("log_body returns the stored weight", body.get("weight_kg"), WEIGHT_KG)
        r.eq("log_body returns the stored waist", body.get("waist_cm"), WAIST_CM)

        corrected = (
            await client.call_tool("log_nutrition", {"day": today, "kcal": KCAL + 100})
        ).data
        r.eq("re-logging a day corrects it", corrected.get("kcal"), KCAL + 100)

        r.start("reads")
        week = (await client.call_tool("get_training_week", {})).data
        for key in ("week_start", "week_end", "sessions", "days", "totals", "signals", "coverage"):
            r.check(
                f"get_training_week has {key!r}", key in week, "missing" if key not in week else ""
            )
        r.eq("a week is seven days", len(week.get("days", [])), 7)

        # The write above must be visible in the derived signals — this is the
        # join working end to end, not just a row landing in a table.
        signals = week.get("signals", {})
        r.eq("written food reaches signals.avg_kcal", signals.get("avg_kcal"), float(KCAL + 100))
        r.eq("written body reaches signals.weight_kg", signals.get("weight_kg"), WEIGHT_KG)
        r.eq("nutrition_days_logged counts the write", signals.get("nutrition_days_logged"), 1)

        cov = week.get("coverage", {}).get("nutrition", {})
        r.eq(
            "coverage adds up to the week",
            cov.get("days_present", 0) + cov.get("days_missing", 0),
            7,
        )
        r.eq("coverage sees the logged day", cov.get("days_present"), 1)
        r.check(
            "coverage does not list the logged day as missing",
            today not in cov.get("missing", []),
            f"missing list: {cov.get('missing')}",
        )

        trend = (await client.call_tool("get_health_trend", {"days": 14})).data
        for key in ("days", "signals", "coverage"):
            r.check(
                f"get_health_trend has {key!r}", key in trend, "missing" if key not in trend else ""
            )

        activities = (await client.call_tool("get_recent_activities", {"n": 5})).data
        r.check(
            "get_recent_activities returns a list",
            isinstance(activities, list),
            f"got {type(activities).__name__}",
        )

        tests = (await client.call_tool("get_tests", {})).data
        r.check("get_tests returns a list", isinstance(tests, list), f"got {type(tests).__name__}")

        r.start("empty-state honesty")
        # With no Garmin sync, health must report absent — not zero, and not a
        # silently short list. A sync that never ran has to be visible.
        r.eq(
            "health coverage reports nothing present", week["coverage"]["health"]["days_present"], 0
        )
        r.check(
            "absent health days are explicit nulls",
            all(d["health"] is None for d in week["days"]),
            "some days claimed health data that was never synced",
        )
        r.eq("resting HR is null, not zero", signals.get("resting_hr_avg"), None)
        r.eq("ramp from no prior load is null, not zero", signals.get("tss_ramp_pct"), None)


def check_log(r: Results, log_path: pathlib.Path) -> None:
    r.start("process log")
    if not log_path.exists():
        r.check("log file present", False, str(log_path))
        return
    lines = log_path.read_text().splitlines()
    r.check("log is not empty", bool(lines), f"{len(lines)} lines")
    problems = [
        line
        for line in lines
        if LOG_PROBLEM.search(line) and not any(p.search(line) for p in ALLOWED_LOG_PATTERNS)
    ]
    r.check(
        "no errors or tracebacks logged", not problems, f"{len(problems)} line(s): {problems[:3]}"
    )
    warnings = [
        line
        for line in lines
        if "WARNING" in line and not any(p.search(line) for p in ALLOWED_LOG_PATTERNS)
    ]
    r.check("no unexpected warnings", not warnings, f"{len(warnings)} line(s): {warnings[:3]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive the app over HTTP and assert the results.")
    parser.add_argument("--url", help="MCP endpoint of a running server. Omit to start one.")
    parser.add_argument("--token", help="Bearer token. Required with --url.")
    parser.add_argument("--log", help="Log file to scan. Omit to skip the log checks.")
    parser.add_argument("--port", type=int, default=8765, help="Port for the self-started server.")
    args = parser.parse_args()

    r = Results()
    server = None
    tmpdir = None
    log_path = pathlib.Path(args.log) if args.log else None

    try:
        if args.url:
            if not args.token:
                print("--token is required with --url", file=sys.stderr)
                return 2
            url, token = args.url, args.token
            base = url.rsplit("/mcp", 1)[0]
        else:
            tmpdir = tempfile.TemporaryDirectory(prefix="soma-smoke-")
            token = secrets.token_urlsafe(24)
            log_path = pathlib.Path(tmpdir.name) / "server.log"
            db_path = pathlib.Path(tmpdir.name) / "smoke.db"
            print(f"Starting a server on port {args.port} against {db_path}")
            server = start_server(log_path, args.port, token, db_path)
            base = f"http://127.0.0.1:{args.port}"
            url = f"{base}/mcp"
            if not wait_for_health(base):
                print("\nServer never became healthy. Log:", file=sys.stderr)
                print(log_path.read_text()[-2000:], file=sys.stderr)
                return 1

        check_transport_and_auth(r, base, token)
        asyncio.run(check_tools(r, url, token))
        if log_path:
            # Give the server a moment to flush anything the last call produced.
            time.sleep(0.5)
            check_log(r, log_path)
        else:
            r.start("process log")
            print("  [skip] no --log given")
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        if tmpdir is not None:
            tmpdir.cleanup()

    print(f"\n{r.passed} passed, {len(r.failed)} failed")
    if r.failed:
        print("\nFailures:", file=sys.stderr)
        for failure in r.failed:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
