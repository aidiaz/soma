"""Assert the Garmin library surface the sync worker depends on still exists.

The sync worker degrades quietly: :func:`soma.ingest.garmin.sync._fetch` turns a failing
endpoint into a logged warning so one dead call cannot abort a whole run. That
is right at runtime and dangerous in CI, because a method that vanished upstream
produces the same output as a genuine rest day — no rows, no error. This check
is the loud counterpart. Run it on a schedule, not only on push: it watches for
drift in someone else's library, which does not move when we commit.

    make contract

Exits non-zero and prints every failure. It makes no network calls; it inspects
the installed package.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import sys
from importlib import metadata

from garminconnect import Garmin

SYNC = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "soma" / "ingest" / "garmin" / "sync.py"
)

# Called outside sync.py's per-day loop, so the AST scan below will not see them.
EXTRA_METHODS = ("login", "get_full_name")

# garminconnect reaches Garmin through curl_cffi, which impersonates a browser
# TLS fingerprint. requests and httpx are both blocked by that fingerprinting,
# so a dependency swap upstream would break every call at runtime.
REQUIRED_DEPENDENCY = "curl_cffi"

# Deprecated March 2026. If it reappears as a dependency the auth story changed.
FORBIDDEN_MODULE = "garth"


def client_methods_called() -> set[str]:
    """Every ``client.<method>(...)`` name invoked in sync.py.

    Derived from the source rather than hard-coded, so a newly added endpoint
    gets contract coverage without anyone remembering to list it here.
    """
    tree = ast.parse(SYNC.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "client"
        ):
            found.add(func.attr)
    return found


def check_methods(failures: list[str]) -> None:
    called = client_methods_called() | set(EXTRA_METHODS)
    if not called:
        failures.append(
            "Found no client.<method>() calls in sync.py — the AST scan is broken, "
            "so this check is no longer verifying anything."
        )
    for name in sorted(called):
        if not hasattr(Garmin, name):
            failures.append(f"Garmin.{name}() no longer exists in garminconnect.")


def check_signatures(failures: list[str]) -> None:
    init = inspect.signature(Garmin.__init__).parameters
    for param in ("email", "password", "prompt_mfa"):
        if param not in init:
            failures.append(
                f"Garmin.__init__ no longer accepts {param!r} — the Garmin auth CLI relies on it."
            )
    if "tokenstore" not in inspect.signature(Garmin.login).parameters:
        failures.append(
            "Garmin.login no longer accepts 'tokenstore' — token-only login is how the "
            "sync worker avoids a credential login and the per-account rate limit."
        )


def check_transport(failures: list[str]) -> None:
    requires = metadata.requires("garminconnect") or []
    if not any(
        req.split()[0].split(">")[0].split("=")[0] == REQUIRED_DEPENDENCY for req in requires
    ):
        failures.append(
            f"garminconnect no longer depends on {REQUIRED_DEPENDENCY}. Garmin blocks "
            "requests and httpx by TLS fingerprint, so check what replaced it before "
            "trusting the sync."
        )


def check_forbidden(failures: list[str]) -> None:
    try:
        __import__(FORBIDDEN_MODULE)
    except ImportError:
        return
    failures.append(
        f"{FORBIDDEN_MODULE!r} is installed. It was deprecated in March 2026 and must not "
        "be reintroduced; check which dependency pulled it back in."
    )


def main() -> int:
    failures: list[str] = []
    check_methods(failures)
    check_signatures(failures)
    check_transport(failures)
    check_forbidden(failures)

    version = metadata.version("garminconnect")
    if failures:
        print(f"API CONTRACT BROKEN (garminconnect {version}):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    checked = sorted(client_methods_called() | set(EXTRA_METHODS))
    print(f"API contract OK (garminconnect {version}). Verified {len(checked)} methods:")
    for name in checked:
        print(f"  Garmin.{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
