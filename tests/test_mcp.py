from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from nanomuse.config import MCPServerSettings
from nanomuse.schema import RiskLevel
from nanomuse.tools import MCPManager, ToolCollection

SERVER = Path(__file__).with_name("mcp_echo_server.py")


async def test_mcp_tools_are_exposed_and_callable():
    manager = MCPManager(
        [
            MCPServerSettings(
                name="echo", command=sys.executable, args=[str(SERVER)], risk=RiskLevel.SAFE
            )
        ]
    )
    try:
        tools = await manager.connect()
        names = sorted(t.name for t in tools)
        assert names == ["echo__add", "echo__echo", "echo__today"]
        collection = ToolCollection(*tools)
        params = collection.to_params()
        assert params[0]["function"]["parameters"]["type"] == "object"
        result = await collection.execute("echo__echo", {"text": "hi"})
        assert result.ok and "echo: hi" in result.output
        result = await collection.execute("echo__add", {"a": 2, "b": 40})
        assert "42" in result.output
        # no arguments still sends an (empty) object — zod-based servers reject a missing one
        result = await collection.execute("echo__today", {})
        assert result.ok and "2026-09-24" in result.output
        assert tools[0].assess({"text": "x"}).summary.startswith("mcp:echo.")
    finally:
        await manager.close()


async def test_per_tool_policy_overrides_the_servers_defaults():
    """`[mcp.servers.tools.<name>]` makes one tool stricter (or looser) than its server;
    a field left out keeps the server's value."""
    from nanomuse.config import MCPToolPolicy

    manager = MCPManager(
        [
            MCPServerSettings(
                name="echo",
                command=sys.executable,
                args=[str(SERVER)],
                risk=RiskLevel.SAFE,
                reads_private_data=True,
                tools={"add": MCPToolPolicy(risk=RiskLevel.SENSITIVE, reads_private_data=False)},
            )
        ]
    )
    try:
        tools = {t.name: t for t in await manager.connect()}
        assert tools["echo__add"].risk == RiskLevel.SENSITIVE
        assert tools["echo__add"].reads_private_data is False
        assert tools["echo__add"].egress is False  # not set: the server's
        assert tools["echo__echo"].risk == RiskLevel.SAFE
        assert tools["echo__echo"].reads_private_data is True
    finally:
        await manager.close()


@pytest.mark.parametrize(
    "tools",
    [
        [NS(name="a.b", description=""), NS(name="a?b", description="")],
        [
            NS(name="x" * 65 + "a", description=""),
            NS(name="x" * 65 + "b", description=""),
        ],
    ],
)
def test_normalised_mcp_tool_name_collisions_skip_the_server(tools):
    """Two tools that would share one model-facing name make one unreachable; the server
    is left out with a warning, and the runtime starts without it."""
    manager = MCPManager([MCPServerSettings(name="server")])
    assert manager._add_tools(manager.servers[0], object(), NS(tools=tools)) is False
    assert manager.tools == []
    fine = [NS(name="a", description=""), NS(name="b", description="")]
    assert manager._add_tools(manager.servers[0], object(), NS(tools=fine)) is True
    assert [t.name for t in manager.tools] == ["server__a", "server__b"]


async def test_unavailable_server_is_skipped():
    manager = MCPManager([MCPServerSettings(name="nope", command="definitely-not-a-command-xyz")])
    try:
        assert await manager.connect() == []
    finally:
        await manager.close()


async def test_vault_placeholders_are_resolved_before_connecting():
    """A key kept in the vault reaches the server through url, args or env; the config
    object itself keeps the placeholder."""
    secrets = {"ECHO_MODE": "loud", "AMAP_KEY": "k-123"}

    def resolve(value):
        import re

        if isinstance(value, str):
            return re.sub(r"\{\{vault:(\w+)\}\}", lambda m: secrets[m.group(1)], value)
        if isinstance(value, list):
            return [resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        return value

    cfg = MCPServerSettings(
        name="echo",
        command=sys.executable,
        args=[str(SERVER), "{{vault:ECHO_MODE}}"],
        env={"ECHO_KEY": "{{vault:AMAP_KEY}}"},
        url=None,
    )
    manager = MCPManager([cfg], resolve=resolve)
    resolved = manager._resolved(cfg)
    assert resolved.args == [str(SERVER), "loud"] and resolved.env == {"ECHO_KEY": "k-123"}
    assert cfg.args[1] == "{{vault:ECHO_MODE}}" and cfg.env["ECHO_KEY"] == "{{vault:AMAP_KEY}}"
    url = MCPServerSettings(name="amap", url="https://mcp.amap.com/mcp?key={{vault:AMAP_KEY}}")
    assert manager._resolved(url).url == "https://mcp.amap.com/mcp?key=k-123"
    try:
        tools = await manager.connect()  # the echo server ignores its extra argument
        assert sorted(t.name for t in tools) == ["echo__add", "echo__echo", "echo__today"]
    finally:
        await manager.close()


async def test_an_unreachable_url_server_is_skipped_not_cancelled():
    """The HTTP transports connect in a task group of their own; a host that does not
    resolve used to surface as a *cancellation* of the caller (a 500 from the
    Connections page, and "Attempted to exit cancel scope in a different task" in the
    log). It is a skipped server like any other, and the caller's task goes on."""
    manager = MCPManager([MCPServerSettings(name="nowhere", url="http://mcp.invalid./mcp?key=abc")])
    tools = await asyncio.wait_for(manager.connect(), timeout=30)
    assert tools == []
    task = asyncio.current_task()
    assert task is not None and task.cancelling() == 0
    await manager.close()
    # the task is still usable
    await asyncio.sleep(0)


async def test_a_refused_key_is_not_written_to_the_log():
    """httpx names the full URL in a 4xx error, and an MCP server's key rides in the query
    (``?key=…``): the warning masks every query value, and the vault's redaction runs on the
    rest (a secret an ``args`` or ``env`` value carried)."""
    import http.server
    import threading

    from nanomuse.logger import logger

    class Refuse(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(401)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_: object) -> None:
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Refuse)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    try:
        manager = MCPManager(
            [MCPServerSettings(name="paid", url=f"http://127.0.0.1:{port}/mcp?key=k-123&v=2")],
            redact=lambda text: text.replace("k-123", "[REDACTED:MCP_KEY]"),
        )
        tools = await asyncio.wait_for(manager.connect(), timeout=30)
        await manager.close()
    finally:
        logger.remove(sink)
        httpd.shutdown()
    assert tools == []
    joined = "\n".join(lines)
    assert "unavailable over sse" in joined and "401" in joined, joined
    assert "k-123" not in joined, joined
    assert "?key=***&v=***" in joined, joined
    # the vault's redaction is applied too, for a secret outside the query
    assert manager._scrub("spawn failed: k-123 x") == "spawn failed: [REDACTED:MCP_KEY] x"


def test_a_server_on_another_host_sends_data_out_unless_the_owner_says_otherwise():
    """Every argument of a call travels to the server: a `url` on another machine counts
    as egress to that host by default, a command or a loopback address does not, and an
    explicit `egress` wins either way."""
    remote = MCPServerSettings(name="maps", url="https://mcp.example.com/mcp?key={{vault:K}}")
    assert remote.remote_host == "mcp.example.com" and remote.sends_out is True
    local = MCPServerSettings(name="cal", url="http://localhost:8000/mcp")
    assert local.remote_host is None and local.sends_out is False
    assert MCPServerSettings(name="fs", command="npx").sends_out is False
    assert (
        MCPServerSettings(name="maps", url="https://mcp.example.com/", egress=False).sends_out
        is False
    )
    assert MCPServerSettings(name="fs", command="npx", egress=True).sends_out is True

    manager = MCPManager([remote])
    assert (
        manager._add_tools(remote, object(), NS(tools=[NS(name="geocode", description="")])) is True
    )
    tool = manager.tools[0]
    assert tool.egress is True and tool.remote_host == "mcp.example.com"
    assessment = tool.assess(
        {"address": "1 Main St", "api_key": "k-123", "n": 3, "tags": ["a", "b"]}
    )
    assert assessment.egress_target == "mcp.example.com" and assessment.target == "mcp.example.com"
    # the audit line carries the shape of the arguments, never their values
    assert "1 Main St" not in assessment.summary and "k-123" not in assessment.summary
    assert (
        assessment.summary
        == 'mcp:maps.geocode({"address":"str[9]","api_key":"***","n":3,"tags":"list[2]"})'
    )


async def test_a_call_the_server_never_answers_fails_after_its_timeout():
    """One call has a clock; a server that stops answering costs the model a failed call,
    not the whole run."""
    from nanomuse.tools.mcp_tools import MCPTool

    class Hangs:
        async def call_tool(self, name, args):
            await asyncio.sleep(5)

    tool = MCPTool(
        name="slow__wait",
        description="",
        parameters={"type": "object", "properties": {}},
        session=Hangs(),
        server="slow",
        original_name="wait",
        call_timeout_s=0.05,
    )
    result = await tool.execute()
    assert not result.ok and "did not answer within 0.05 s" in result.error


async def test_a_handshake_that_stalls_skips_the_server(monkeypatch):
    """A server that accepts the connection and then says nothing is skipped at start, like
    one that is down; the runtime starts without its tools."""
    manager = MCPManager(
        [
            MCPServerSettings(
                name="echo", command=sys.executable, args=[str(SERVER)], connect_timeout_s=0.01
            )
        ]
    )
    try:
        assert await manager.connect() == []
    finally:
        await manager.close()


def _annotated(name: str, **annotations):
    """A tool as an MCP server describes it: a name, a description, and the
    annotations the specification defines for it."""
    return NS(name=name, description="", annotations=NS(**annotations))


def test_a_tools_own_annotations_say_how_dangerous_it_is():
    """The MCP specification's hints about a tool are used when the config says
    nothing about that tool: the author of a tool knows what it does, while a
    blanket ``risk`` in the config is one guess about every tool of a server."""
    manager = MCPManager([MCPServerSettings(name="srv")])
    assert manager._add_tools(
        manager.servers[0],
        object(),
        NS(
            tools=[
                _annotated("reader", readOnlyHint=True),
                _annotated("breaker", destructiveHint=True),
                _annotated("writer", readOnlyHint=False),
                NS(name="plain", description=""),
            ]
        ),
    )
    risks = {t.original_name: t.risk for t in manager.tools}
    assert risks["reader"] == RiskLevel.SAFE
    assert risks["breaker"] == RiskLevel.SENSITIVE
    assert risks["writer"] == RiskLevel.MODERATE
    # a tool that says nothing keeps the server's default
    assert risks["plain"] == RiskLevel.MODERATE


def test_a_risk_written_by_hand_is_a_floor_the_annotations_cannot_lower():
    """A ``risk`` the person wrote for the server is their own verdict about that
    server: an annotation may raise it, never lower it."""
    manager = MCPManager([MCPServerSettings(name="srv", risk=RiskLevel.SENSITIVE)])
    assert manager._add_tools(
        manager.servers[0],
        object(),
        NS(tools=[_annotated("reader", readOnlyHint=True)]),
    )
    assert manager.tools[0].risk == RiskLevel.SENSITIVE

    manager = MCPManager([MCPServerSettings(name="srv", risk=RiskLevel.MODERATE)])
    assert manager._add_tools(
        manager.servers[0],
        object(),
        NS(tools=[_annotated("breaker", destructiveHint=True)]),
    )
    assert manager.tools[0].risk == RiskLevel.SENSITIVE


def test_a_policy_for_one_tool_wins_over_its_annotations():
    """``[mcp.servers.tools.<name>]`` is more specific than anything the server says."""
    from nanomuse.config import MCPToolPolicy

    manager = MCPManager(
        [
            MCPServerSettings(
                name="srv",
                tools={"breaker": MCPToolPolicy(risk=RiskLevel.MODERATE)},
            )
        ]
    )
    assert manager._add_tools(
        manager.servers[0],
        object(),
        NS(tools=[_annotated("breaker", destructiveHint=True)]),
    )
    assert manager.tools[0].risk == RiskLevel.MODERATE


# --------------------------------------------------------------- the server's own rules


class _QuietUI:
    """Just enough of the UI for the agent to build its prompt."""

    def on_text_delta(self, text: str) -> None: ...
    def on_assistant_message(self, content, reasoning) -> None: ...  # noqa: ANN001
    def on_tool_call(self, call, summary: str) -> None: ...  # noqa: ANN001
    def on_tool_result(self, call, result) -> None: ...  # noqa: ANN001
    def on_sentinel(self, decision: str, summary: str, reasons: list[str]) -> None: ...
    def info(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...

    async def ask_approval(self, request) -> None:  # noqa: ANN001
        raise AssertionError("building the prompt must not need an approval")


def _agent(settings):
    """An agent with only the terminate tool — nothing connects to anything."""
    from nanomuse.agent.core import MuseAgent
    from nanomuse.llm import MockLLM
    from nanomuse.sentinel import AuditLog, Sentinel
    from nanomuse.tools import Terminate, ToolCollection

    ui = _QuietUI()
    audit = AuditLog(settings.audit_file)
    sentinel = Sentinel(settings.sentinel, audit=audit, ui=ui)
    return MuseAgent(settings, MockLLM([]), ToolCollection(Terminate()), sentinel, ui, audit)


def test_a_servers_own_instructions_reach_the_system_prompt(settings):
    """A server can carry its own rules — which store a write goes to, how often to
    post, what a caption has to carry. They belong with the server, so they travel
    with it into the prompt instead of being repeated in the user's own instructions."""
    settings.mcp.servers = [
        MCPServerSettings(
            name="shopify",
            command="true",
            instructions="Before any write, name the store it will change.",
        ),
        MCPServerSettings(name="echo", command="true"),
    ]
    prompt = _agent(settings).build_system_prompt("hi")
    assert "## MCP servers" in prompt
    assert "Before any write, name the store it will change." in prompt
    # the server's name and its tool prefix are there, so the rules can be tied to
    # the right tools
    assert "**shopify**" in prompt and "shopify__*" in prompt
    # a server that said nothing adds nothing
    assert "**echo**" not in prompt


def test_no_mcp_section_when_no_server_said_anything(settings):
    """No instructions anywhere → no section, so the prompt stays as it was."""
    settings.mcp.servers = [MCPServerSettings(name="echo", command="true")]
    assert "## MCP servers" not in _agent(settings).build_system_prompt("hi")


def test_the_instructions_can_be_read_from_the_config_file(tmp_path):
    """``[[mcp.servers]] instructions = "..."`` in config.toml reaches the settings."""
    from nanomuse.config import load_settings

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'data_dir = "{tmp_path.as_posix()}"\n'
        "[[mcp.servers]]\n"
        'name = "shopify"\n'
        'command = "true"\n'
        'instructions = "Post at most twice a day."\n'
    )
    servers = load_settings(cfg).mcp.servers
    assert len(servers) == 1
    assert servers[0].instructions == "Post at most twice a day."
