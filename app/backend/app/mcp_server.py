"""[Agentic] MCP server — CareRoute's read-only tools over the Model Context Protocol.

ArchAAS Day 3 presents MCP as the standard connector between an agent host and
the tools it may use: the host discovers tools (`tools/list`, each with a JSON
schema) and invokes them (`tools/call`) over a transport — here stdio, the same
transport as the course's local MCP demo.

What is exposed is decided by the TOOL REGISTRY, not by this file: every tool
whose `allowed_agents` includes the external principal "mcp" and which is
read-only. Today that is guideline retrieval and red-flag rule lookup — no
patient data, no triage, nothing that writes. Each call goes through
`registry.call`, so an MCP client gets the same schema validation, two-key
authorisation, error observations and `careroute_tool_calls_total` metrics as
the in-process critic. Tool names use underscores (`rag_retrieve`) because MCP
tool names may not contain dots.

Run it (stdout is the protocol channel; logs go to stderr):

    python -m app.mcp_server

Connect from any MCP client by registering `python -m app.mcp_server` as a
stdio server (the client's own `mcp add` / config-file mechanism).

Needs the optional `mcp` package (requirements-agentic.txt).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

logger = logging.getLogger("careroute.mcp")

PRINCIPAL_SLUG = "mcp"


def _exposed_specs() -> dict[str, Any]:
    """MCP name -> registry ToolSpec. [Agentic] Delegates to `registry.mcp_tools()`,
    the same definition the agent cards and the MCP client path use, so a card
    that says a tool is reachable over MCP and this server can never disagree.
    Contextual tools (Care-Routing's, which carry the patient's location in
    their case context) are excluded there."""
    from .tools import registry

    return registry.mcp_tools()


class McpPrincipal:
    """The external caller, as the gateway sees it: one slug, and an allow-list
    that is exactly the set of tools published to MCP."""

    SLUG = PRINCIPAL_SLUG

    def __init__(self) -> None:
        self.TOOL_ALLOWLIST = [spec.name for spec in _exposed_specs().values()]


def build_server():
    import mcp.types as types
    from mcp.server.lowlevel import Server

    from .tools import registry

    server = Server("careroute")
    principal = McpPrincipal()

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=mcp_name, description=spec.description, inputSchema=spec.parameters)
            for mcp_name, spec in _exposed_specs().items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        spec = _exposed_specs().get(name)
        if spec is None:
            raise ValueError(f"unknown tool {name!r}")
        result = await asyncio.to_thread(registry.call, principal, spec.name, arguments or {})
        if not result["ok"]:
            # Raising makes the SDK return isError=true with this message.
            raise ValueError(result["error"])
        return [types.TextContent(type="text", text=json.dumps(result["observation"], ensure_ascii=False))]

    return server


def warm_up() -> None:
    """Load every tool's heavy dependencies BEFORE the stdio transport starts.

    Measured on Windows: the first `rag_retrieve` call imported scikit-learn
    (and, with embeddings on, onnxruntime) inside a worker thread while the
    stdio transport was reading the console, and the call hung with no output.
    Importing and building the indexes here, on the main thread and before any
    protocol traffic, removes that. It also makes the first real call fast."""
    from . import rag, rag_embed
    from .tools import registry  # noqa: F401 - imports the agents package once, up front

    rag._ensure_index()
    embedder = rag_embed.get_embedder()
    if embedder is not None:
        rag._dense_index(embedder)


async def serve_stdio() -> None:
    from mcp.server.stdio import stdio_server

    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        import mcp  # noqa: F401 - availability probe
    except ImportError:
        sys.stderr.write("the `mcp` package is not installed: pip install -r requirements-agentic.txt\n")
        return 2
    warm_up()
    asyncio.run(serve_stdio())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
