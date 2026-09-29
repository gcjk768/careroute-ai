"""[Agentic] An agent CAN consume a gateway tool over MCP.

Lecture gap: "agents do not consume MCP tools". With
CAREROUTE_TOOL_TRANSPORT=mcp the gateway still authorises and validates the
call locally, then executes an MCP-published tool through the MCP CLIENT
(tools/mcp_client.py) against `python -m app.mcp_server` instead of in-process.
The default stays in-process. The real-stdio test skips cleanly when the
optional `mcp` package is not installed.
"""
from __future__ import annotations

import pytest

from app.tools import mcp_client, registry


class _Agent:
    SLUG = "reflection"
    TOOL_ALLOWLIST = ["critique", "llm.complete", "rag.retrieve", "redflags.lookup"]


def test_default_transport_is_in_process(monkeypatch):
    monkeypatch.delenv("CAREROUTE_TOOL_TRANSPORT", raising=False)
    monkeypatch.setattr(mcp_client, "call_tool", lambda *_a, **_k: pytest.fail("MCP used by default"))
    result = registry.call(_Agent(), "redflags.lookup", {"term": "chest"})
    assert result["ok"] and result["transport"] == "inprocess"


def test_mcp_transport_dispatches_through_the_mcp_client(monkeypatch):
    seen = []
    monkeypatch.setenv("CAREROUTE_TOOL_TRANSPORT", "mcp")
    monkeypatch.setattr(mcp_client, "available", lambda: True)

    def fake_call(mcp_name, arguments):
        seen.append((mcp_name, arguments))
        return {"ok": True, "observation": [{"name": "from-mcp"}]}

    monkeypatch.setattr(mcp_client, "call_tool", fake_call)
    result = registry.call(_Agent(), "redflags.lookup", {"term": "chest"})
    assert result == {**result, "ok": True, "transport": "mcp", "observation": [{"name": "from-mcp"}]}
    # Arguments were validated BEFORE leaving the process: the MCP name, not the dotted one.
    assert seen == [("redflags_lookup", {"term": "chest"})]


def test_mcp_transport_still_authorises_and_validates_locally(monkeypatch):
    monkeypatch.setenv("CAREROUTE_TOOL_TRANSPORT", "mcp")
    monkeypatch.setattr(mcp_client, "available", lambda: True)
    monkeypatch.setattr(mcp_client, "call_tool", lambda *_a, **_k: pytest.fail("should not reach MCP"))
    assert registry.call(_Agent(), "redflags.lookup", {"term": "x"})["outcome"] == "invalid_arguments"

    class _Intruder:
        SLUG = "routing"
        TOOL_ALLOWLIST = ["redflags.lookup"]

    assert registry.call(_Intruder(), "redflags.lookup", {"term": "chest"})["outcome"] == "refused"


def test_mcp_failure_is_an_error_observation(monkeypatch):
    monkeypatch.setenv("CAREROUTE_TOOL_TRANSPORT", "mcp")
    monkeypatch.setattr(mcp_client, "available", lambda: True)

    def boom(*_a, **_k):
        raise TimeoutError("server hung with secret detail")

    monkeypatch.setattr(mcp_client, "call_tool", boom)
    result = registry.call(_Agent(), "redflags.lookup", {"term": "chest"})
    assert result["ok"] is False and result["outcome"] == "error"
    assert "TimeoutError" in result["error"] and "secret" not in result["error"]


def test_mcp_unavailable_falls_back_in_process(monkeypatch):
    monkeypatch.setenv("CAREROUTE_TOOL_TRANSPORT", "mcp")
    monkeypatch.setattr(mcp_client, "available", lambda: False)
    result = registry.call(_Agent(), "redflags.lookup", {"term": "chest"})
    assert result["ok"] and result["transport"] == "inprocess"


def test_contextual_tools_are_never_published_or_sent_over_mcp():
    """Routing tools carry the patient's location in their case context; they
    must never leave the process."""
    published = registry.mcp_tools()
    assert set(published) == {"rag_retrieve", "redflags_lookup"}
    assert not any(spec.contextual for spec in published.values())


def test_real_mcp_round_trip(monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setenv("CAREROUTE_TOOL_TRANSPORT", "mcp")
    monkeypatch.setenv("CAREROUTE_RAG_EMBEDDINGS", "off")
    result = registry.call(_Agent(), "redflags.lookup", {"term": "chest"})
    assert result["ok"] and result["transport"] == "mcp"
    assert any(r["name"] == "cardiac_chest_pain" for r in result["observation"])
