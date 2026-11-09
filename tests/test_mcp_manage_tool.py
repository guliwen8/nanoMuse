"""The ``mcp`` tool: the agent lists, connects and removes MCP servers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from nanomuse.config import Settings
from nanomuse.llm import MockLLM
from nanomuse.schema import RiskLevel
from nanomuse.server.service import MuseService

ECHO = str(Path(__file__).with_name("mcp_echo_server.py"))


@pytest.fixture
def service(settings: Settings) -> MuseService:
    return MuseService(settings, llm=MockLLM([]))


def test_the_tool_is_registered_and_asks_first(service: MuseService):
    tool = service.app.tools.get("mcp")
    assert tool is not None
    assert tool.risk == RiskLevel.SAFE
    # listing is safe
    assert tool.assess({"action": "list"}).risk == RiskLevel.SAFE
    # connecting a server runs a program: the user is asked
    connect = tool.assess(
        {"action": "connect", "name": "echo", "command": "python", "args": ["x.py"]}
    )
    assert connect.risk == RiskLevel.SENSITIVE
    assert connect.egress is True
    assert "echo" in connect.summary
    # removing one, too
    assert tool.assess({"action": "remove", "name": "echo"}).risk == RiskLevel.SENSITIVE


async def test_listing_servers(service: MuseService):
    tool = service.app.tools.get("mcp")
    result = await tool.execute(action="list")
    assert result.ok
    assert "No MCP servers" in result.output


async def test_connecting_a_server_brings_its_tools(service: MuseService):
    tool = service.app.tools.get("mcp")
    result = await tool.execute(
        action="connect",
        name="echo",
        command=sys.executable,
        args=[ECHO],
    )
    try:
        assert result.ok, result.output
        assert "echo" in result.output
        # the server's tools are the agent's tools now
        names = {t.name for t in service.app.tools}
        assert "echo__echo" in names
        listed = await tool.execute(action="list")
        assert "echo" in listed.output
        assert "3 tool(s)" in listed.output
    finally:
        await service.connections.remove_mcp("echo")


async def test_a_server_that_cannot_be_reached_is_a_message(service: MuseService):
    tool = service.app.tools.get("mcp")
    result = await tool.execute(
        action="connect",
        name="broken",
        command=sys.executable,
        args=["-c", "print('not an mcp server')"],
    )
    assert not result.ok
    assert "broken" in (result.error or "")
    # the model sees the message too
    assert "broken" in result.for_model()


async def test_connecting_needs_a_command_or_a_url(service: MuseService):
    tool = service.app.tools.get("mcp")
    result = await tool.execute(action="connect", name="empty")
    assert not result.ok
    assert "command" in (result.error or "")


async def test_a_secret_goes_into_the_vault_and_never_to_the_config(service: MuseService):
    tool = service.app.tools.get("mcp")
    result = await tool.execute(
        action="connect",
        name="secretive",
        command=sys.executable,
        args=[ECHO],
        env={"ECHO_KEY": "k-123"},
    )
    try:
        assert result.ok, result.output
        stored = json.loads((service.data_dir / "app-settings.json").read_text("utf-8"))
        assert "k-123" not in json.dumps(stored)
        assert service.connections.vault.get("MCP_SECRETIVE_ECHO_KEY") == "k-123"
    finally:
        await service.connections.remove_mcp("secretive")
