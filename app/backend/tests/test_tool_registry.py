"""[Agentic][AI-Security] Central tool registry + gateway.

ArchAAS Day 3 (AM) teaches a tool REGISTRY (one catalogue of tools with
schemas and metadata) and a GATEWAY in front of it (argument validation,
authorisation, quotas, tracing). Until 2026-09-16 CareRoute's only registry was
a private dict inside Care-Routing, and every other "tool" was a string label.

These tests pin the gateway's contract for MODEL-CHOSEN tool calls: arguments
are validated against the declared schema before any handler runs, a caller
outside the tool's allowed agents — or without the tool on its own allow-list —
is refused, a failing handler produces an error OBSERVATION instead of raising
into the agent loop, and the catalogue is served on GET /api/tools.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.tools import registry
from app.tools.registry import ToolSpec, validate_arguments

client = TestClient(app)


class _Agent:
    def __init__(self, slug: str, allow: list[str]):
        self.SLUG = slug
        self.TOOL_ALLOWLIST = allow


REFLECTION = _Agent("reflection", ["critique", "llm.complete", "rag.retrieve", "redflags.lookup"])


# --- catalogue ----------------------------------------------------------------
def test_catalogue_lists_every_tool_with_schemas():
    names = {t["name"]: t for t in registry.catalog()}
    assert {"rag.retrieve", "redflags.lookup"} <= set(names)
    # Care-Routing's tools are gateway-executed since 2026-09-24 (they used to
    # be catalogued as "in-agent" and refused by the gateway).
    assert {"travel.estimate", "facility.hours.lookup", "clinic.lookup"} <= set(names)
    assert names["travel.estimate"]["executor"] == "gateway" and names["travel.estimate"]["contextual"]
    for tool in names.values():
        assert tool["description"]
        assert tool["parameters"]["type"] == "object"
        assert tool["allowedAgents"]


def test_api_serves_the_catalogue():
    body = client.get("/api/tools").json()
    assert {t["name"] for t in body["tools"]} >= {"rag.retrieve", "redflags.lookup", "travel.estimate"}


# --- argument validation ----------------------------------------------------------
_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 2, "maxLength": 10},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
        "mode": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["query"],
    "additionalProperties": False,
}


def test_validation_applies_defaults_and_accepts_valid_arguments():
    assert validate_arguments(_SCHEMA, {"query": "chest"}) == {"query": "chest", "top_k": 2}


def test_validation_rejects_every_malformed_shape():
    bad = [
        {},                                    # missing required
        {"query": 5},                          # wrong type
        {"query": "x"},                        # too short
        {"query": "far too long here"},        # too long
        {"query": "chest", "top_k": 9},        # out of range
        {"query": "chest", "top_k": True},     # bool is not an integer
        {"query": "chest", "mode": "z"},       # not in enum
        {"query": "chest", "extra": 1},        # unknown key
        "not a dict",
    ]
    for args in bad:
        try:
            validate_arguments(_SCHEMA, args)
        except registry.ToolArgumentError:
            continue
        raise AssertionError(f"accepted malformed arguments: {args!r}")


# --- gateway ----------------------------------------------------------------------
def test_gateway_executes_an_allowed_valid_call():
    result = registry.call(REFLECTION, "redflags.lookup", {"term": "chest"})
    assert result["ok"] is True and result["tool"] == "redflags.lookup"
    assert any(r["name"] == "cardiac_chest_pain" for r in result["observation"])
    assert result["latencyMs"] >= 0


def test_gateway_rag_tool_returns_screened_citations():
    result = registry.call(REFLECTION, "rag.retrieve", {"query": "crushing chest pain", "top_k": 1})
    assert result["ok"] is True
    assert len(result["observation"]) == 1
    assert set(result["observation"][0]) == {"title", "snippet", "source"}


def test_gateway_refuses_an_agent_the_tool_does_not_allow():
    intruder = _Agent("routing", ["redflags.lookup"])
    result = registry.call(intruder, "redflags.lookup", {"term": "chest"})
    assert result["ok"] is False and "not permitted" in result["error"]


def test_gateway_refuses_a_tool_missing_from_the_agents_own_allowlist():
    narrowed = _Agent("reflection", ["critique"])
    result = registry.call(narrowed, "redflags.lookup", {"term": "chest"})
    assert result["ok"] is False and "allow-list" in result["error"]


def test_gateway_refuses_unknown_tools_and_contextual_tools_without_a_case():
    assert registry.call(REFLECTION, "shell.exec", {})["ok"] is False
    routing_agent = _Agent("routing", ["travel.estimate"])
    result = registry.call(routing_agent, "travel.estimate", {"clinic_id": "x", "transport": "walk"})
    assert result["ok"] is False and "case context" in result["error"]


def test_gateway_rejects_invalid_arguments_before_the_handler_runs(monkeypatch):
    ran = []
    spec = ToolSpec(
        name="test.echo", description="echo", allowed_agents=frozenset({"reflection"}),
        parameters={"type": "object", "properties": {"x": {"type": "string", "maxLength": 3}},
                    "required": ["x"], "additionalProperties": False},
        handler=lambda args: ran.append(args) or args,
    )
    monkeypatch.setitem(registry.REGISTRY, spec.name, spec)
    agent = _Agent("reflection", ["test.echo"])
    result = registry.call(agent, "test.echo", {"x": "toolong"})
    assert result["ok"] is False and "argument" in result["error"]
    assert ran == []


def test_gateway_turns_a_handler_exception_into_an_error_observation(monkeypatch):
    def boom(_args):
        raise RuntimeError("upstream exploded with secret detail")

    spec = ToolSpec(
        name="test.boom", description="fails", allowed_agents=frozenset({"reflection"}),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=boom,
    )
    monkeypatch.setitem(registry.REGISTRY, spec.name, spec)
    result = registry.call(_Agent("reflection", ["test.boom"]), "test.boom", {})
    assert result["ok"] is False
    # The exception TYPE is reported; its message (which could carry data) is not.
    assert "RuntimeError" in result["error"] and "secret" not in result["error"]
