"""[Agentic] MCP CLIENT path — an agent consuming a gateway tool over MCP.

Lecture gap: "agents do not consume MCP tools". CareRoute already SERVED its
read-only tools over MCP (`app/mcp_server.py`); nothing inside the app was a
client. With CAREROUTE_TOOL_TRANSPORT=mcp, `tools.registry.call()` authorises
and validates a call locally, then hands it here: this module starts the MCP
server over stdio (the course demo's transport), initialises a session, calls
`tools/call`, and returns the observation. The server then applies its own
gateway checks as the "mcp" principal, so the call is policed on both sides.

Optional by design: needs the `mcp` package from requirements-agentic.txt,
imported lazily behind `available()`, so the minimal install CI runs never
imports it and the default in-process path is unaffected.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parents[2]
#: The server warms the RAG index before it speaks, so allow for that start-up.
TIMEOUT_SECONDS = float(os.getenv("CAREROUTE_MCP_TIMEOUT", "60"))


def available() -> bool:
    return importlib.util.find_spec("mcp") is not None


async def _call(mcp_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # The child must run its tools IN-PROCESS: an MCP server that is itself an
    # MCP client would spawn servers forever.
    env = {**os.environ, "CAREROUTE_TOOL_TRANSPORT": "inprocess", "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(_BACKEND), env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(mcp_name, arguments)
    text = result.content[0].text if result.content else ""
    if result.isError:
        return {"ok": False, "error": text}
    return {"ok": True, "observation": json.loads(text)}


def call_tool(mcp_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Synchronous facade for the gateway. Runs the async client on its own
    thread + event loop, because the gateway may be called from inside a
    running loop (asyncio.run would refuse there).

    ponytail: one server process per call — seconds of start-up each. Fine for
    the demo path and the reference-data tools it serves; keep one long-lived
    session if MCP becomes the hot path."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(asyncio.run, _call(mcp_name, arguments)).result(timeout=TIMEOUT_SECONDS)
    finally:
        # Not `with`: its exit waits for the worker, which would turn the
        # timeout above back into a hang.
        pool.shutdown(wait=False)
