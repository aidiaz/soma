"""Regression: build_mcp must be a factory, never a shared singleton.

`create_app` once mutated a module-level FastMCP instance to attach the HTTP
auth provider. Importing the HTTP app therefore reconfigured the stdio server in
the same process — silently, because nothing failed until a stdio client tried
to call a tool and was asked for a bearer token it had no way to supply.
"""

from __future__ import annotations

from conftest import make_settings
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from soma.serve.oauth import build_auth
from soma.serve.server import SCOPE, build_mcp

EXPECTED_TOOLS = {
    "get_training_week",
    "get_daily_series",
    "get_health_trend",
    "get_recent_activities",
    "get_tests",
    "get_sync_status",
    "request_sync",
    "log_food",
    "log_water",
    "log_body",
}


async def _tool_names(mcp) -> set[str]:
    return {tool.name for tool in await mcp.list_tools()}


def test_each_call_returns_a_new_instance():
    assert build_mcp() is not build_mcp()


def test_stdio_instance_has_no_auth():
    assert build_mcp().auth is None


def test_auth_is_attached_to_the_instance_that_asked_for_it():
    verifier = StaticTokenVerifier(tokens={"t": {"client_id": "c", "scopes": [SCOPE]}})
    assert build_mcp(auth=verifier).auth is verifier


def test_building_an_authenticated_instance_leaves_the_stdio_one_open():
    # The exact regression: build the HTTP instance, then confirm a stdio
    # instance built afterwards is still unauthenticated.
    stdio = build_mcp()
    build_mcp(auth=build_auth(make_settings()))
    assert stdio.auth is None
    assert build_mcp().auth is None


async def test_every_expected_tool_is_registered():
    assert await _tool_names(build_mcp()) == EXPECTED_TOOLS


async def test_tools_survive_building_a_second_instance():
    first = build_mcp()
    build_mcp(auth=build_auth(make_settings()))
    assert await _tool_names(first) == EXPECTED_TOOLS


async def test_two_instances_hold_separate_tool_objects():
    first, second = build_mcp(), build_mcp()
    assert await _tool_names(first) == await _tool_names(second)
    # Same names, different objects: neither instance shares registry state.
    assert await first.get_tool("get_training_week") is not await second.get_tool(
        "get_training_week"
    )


# Anything that could put a vendor, or a way to reach one, inside a tool call.
# `soma.ingest` is on the list because that is where every vendor credential
# lives: importing it is how the rule would be broken by accident rather than
# on purpose.
FORBIDDEN_IMPORTS = ("garminconnect", "garth", "curl_cffi", "requests", "httpx", "soma.ingest")


def _imported_modules(module) -> set[str]:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_serving_layer_cannot_reach_a_vendor():
    """The architectural rule, asserted rather than trusted to review.

    This reads imports out of the AST. It used to scan the module text for
    vendor names, which was a proxy for the same thing and wrong in both
    directions: it missed `garth` and `curl_cffi` entirely, and it failed on a
    docstring that merely said the word Wahoo — so the first tool that had to
    *describe* ingestion could not be documented. Imports are what the rule is
    actually about, and prose cannot fake one.
    """
    from soma.serve import queries, server

    # queries too: server.py delegates to it, so a vendor call one layer down
    # is inside a tool call just the same.
    for module in (server, queries):
        for imported in _imported_modules(module):
            for forbidden in FORBIDDEN_IMPORTS:
                assert imported != forbidden and not imported.startswith(f"{forbidden}."), (
                    f"{module.__name__} imports {imported}"
                )
