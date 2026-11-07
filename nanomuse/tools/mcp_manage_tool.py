"""The ``mcp`` tool: the agent connects to MCP servers and sees what they offer.

An MCP server is a program (or a URL) that hands the model extra tools — a file
manager, a web search, a company's own API. Connecting one is the same as turning
on a capability, so it asks the user first: the server runs with the machine's
permissions and may reach the network.

Credentials never pass through the model: a value given in ``env`` goes straight into
the encrypted vault and only a ``{{vault:NAME}}`` reference is kept, so the key is
never in the conversation, the config file or the audit log.
"""

from __future__ import annotations

import json
from typing import Any

from nanomuse.schema import RiskLevel, ToolResult
from nanomuse.tools.base import BaseTool, CallAssessment


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


class MCPServers(BaseTool):
    name: str = "mcp"
    description: str = (
        "MCP servers are programs (or web endpoints) that give you extra tools — a file "
        "manager, a web search, an internal API. Actions: `list`, the servers that are "
        "configured and whether they are connected, with the tools each one offers. "
        "`connect`, add a server and connect to it: `name` (letters, digits, dot, dash, "
        "underscore), and either `command` with `args` (a program to run, e.g. "
        "`npx` with `[\"-y\", \"@modelcontextprotocol/server-filesystem\", \"/data\"]`) or "
        "`url` (a streamable-HTTP endpoint). `env` holds the environment variables the "
        "server needs; a secret goes in there and is stored in the encrypted vault, never "
        "shown again. `remove`, disconnect a server and forget it (`name`). Connecting a "
        "server asks the user first."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "connect", "remove"]},
            "name": {"type": "string"},
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
            "url": {"type": "string"},
        },
        "required": ["action"],
    }
    risk: RiskLevel = RiskLevel.SAFE
    #: Connections — the service that keeps the servers and the vault
    connections: Any = None

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        action = str(args.get("action") or "")
        name = str(args.get("name") or "").strip()
        if action == "connect":
            # the server runs with this machine's permissions and may reach the network
            command = str(args.get("command") or "").strip()
            url = str(args.get("url") or "").strip()
            target = url if url else command
            return CallAssessment(
                risk=RiskLevel.SENSITIVE,
                egress=True,
                egress_target=target or None,
                target=name or None,
                summary=f"connect MCP server {name!r}" + (f" ({target})" if target else ""),
                warnings=(
                    ["the server's environment variables are stored in the encrypted vault"]
                    if args.get("env")
                    else []
                ),
            )
        if action == "remove":
            return CallAssessment(
                risk=RiskLevel.SENSITIVE,
                target=name or None,
                summary=f"remove MCP server {name!r}",
            )
        return CallAssessment(risk=RiskLevel.SAFE, summary=f"mcp {action}")

    async def execute(
        self,
        action: str = "",
        name: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str | None = None,
        **_: Any,
    ) -> ToolResult:
        conn = self.connections
        if conn is None:
            return ToolResult.fail("MCP servers are not available here")
        try:
            if action == "list":
                servers = (conn.view().get("mcp") or [])
                lines = []
                for s in servers:
                    lines.append(
                        "- {name}: {state}{tools}".format(
                            name=s.get("name"),
                            state="connected" if s.get("connected") else "not connected",
                            tools=f", {s.get('tools')} tool(s)" if s.get("tools") else "",
                        )
                    )
                if not lines:
                    return ToolResult(output="No MCP servers are configured.")
                return ToolResult(output=f"{len(servers)} MCP server(s):\n" + "\n".join(lines))
            if action == "connect":
                name = (name or "").strip()
                if not name:
                    return ToolResult.fail("`name` is required")
                if not command and not url:
                    return ToolResult.fail("give either a `command` to run or a `url` to connect to")
                body: dict[str, Any] = {"name": name}
                if command:
                    body["command"] = command
                if args:
                    body["args"] = [str(a) for a in args]
                if url:
                    body["url"] = url
                if env:
                    body["env"] = {str(k): str(v) for k, v in env.items()}
                try:
                    await conn.add_mcp(body)
                except ValueError as exc:
                    return ToolResult.fail(str(exc))
                except RuntimeError as exc:
                    # the server is kept and tried again at the next start
                    return ToolResult.fail(str(exc))
                return ToolResult(
                    output=f"Connected MCP server {name!r}. Its tools are available now."
                )
            if action == "remove":
                name = (name or "").strip()
                if not name:
                    return ToolResult.fail("`name` is required")
                if not await conn.remove_mcp(name):
                    return ToolResult.fail(f"no MCP server named {name!r}")
                return ToolResult(output=f"Removed MCP server {name!r}.")
            return ToolResult.fail(f"unknown action {action!r}")
        except Exception as exc:  # noqa: BLE001 — a tool must not crash the loop
            return ToolResult.fail(f"{type(exc).__name__}: {exc}")


__all__ = ["MCPServers"]
