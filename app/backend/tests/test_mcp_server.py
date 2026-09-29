"""[Agentic] MCP server, driven by the official MCP client over real stdio.

The server is started as a subprocess (`python -m app.mcp_server`) and spoken to
with `mcp.client.stdio` + `ClientSession`, exactly as an agent host would:
initialise, list tools, call them. Skipped when the optional `mcp` package is
not installed (CI's minimal install).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

_BACKEND = Path(__file__).resolve().parents[1]


def _params() -> StdioServerParameters:
    env = {**os.environ, "CAREROUTE_RAG_EMBEDDINGS": "off", "PYTHONIOENCODING": "utf-8"}
    return StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"],
                                 cwd=str(_BACKEND), env=env)


async def _session_do(fn):
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def test_server_publishes_only_read_only_registry_tools_with_their_schemas():
    async def _list(session):
        return (await session.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(_session_do(_list))}
    assert set(tools) == {"rag_retrieve", "redflags_lookup"}
    assert tools["redflags_lookup"].inputSchema["required"] == ["term"]
    assert tools["rag_retrieve"].inputSchema["properties"]["top_k"]["maximum"] == 3


def test_calling_a_tool_goes_through_the_gateway_and_returns_its_observation():
    async def _call(session):
        return await session.call_tool("redflags_lookup", {"term": "chest"})

    result = asyncio.run(_session_do(_call))
    assert result.isError is False
    rules = json.loads(result.content[0].text)
    assert any(r["name"] == "cardiac_chest_pain" for r in rules)


def test_guideline_retrieval_over_mcp():
    async def _call(session):
        return await session.call_tool("rag_retrieve", {"query": "crushing chest pain", "top_k": 1})

    result = asyncio.run(_session_do(_call))
    hits = json.loads(result.content[0].text)
    assert len(hits) == 1 and set(hits[0]) == {"title", "snippet", "source"}


def test_invalid_arguments_are_rejected_by_the_gateway_as_a_tool_error():
    async def _call(session):
        return await session.call_tool("redflags_lookup", {"term": "x", "extra": True})

    result = asyncio.run(_session_do(_call))
    assert result.isError is True
    # mcp < 1.28 reported our gateway's "argument" error; 1.28+ validates the
    # input schema first ("Additional properties are not allowed"). Rejected either way.
    text = result.content[0].text
    assert "argument" in text or "Additional properties" in text


def test_an_unpublished_tool_cannot_be_called():
    async def _call(session):
        return await session.call_tool("travel_estimate", {"clinic_id": "c", "transport": "walk"})

    result = asyncio.run(_session_do(_call))
    assert result.isError is True
