"""Regression: build_mcp must be a factory, never a shared singleton.

`create_app` once mutated a module-level FastMCP instance to attach the HTTP
auth provider. Importing the HTTP app therefore reconfigured the stdio server in
the same process — silently, because nothing failed until a stdio client tried
to call a tool and was asked for a bearer token it had no way to supply.
"""

from __future__ import annotations

from conftest import make_settings
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from traindb.serve.oauth import build_auth
from traindb.serve.server import SCOPE, build_mcp

EXPECTED_TOOLS = {
    "get_training_week",
    "get_health_trend",
    "get_recent_activities",
    "get_tests",
    "log_nutrition",
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


async def test_no_tool_reaches_garmin():
    # The architectural rule, asserted rather than trusted to review: the tool
    # layer imports queries only. A tool that called Garmin would need a client.
    import inspect

    from traindb.serve import server

    source = inspect.getsource(server)
    for vendor in ("garminconnect", "garmin.client", "garmin.sync", "wahoo"):
        assert vendor not in source
