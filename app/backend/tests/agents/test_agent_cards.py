"""[Agentic][A2A] Agent Cards + runtime discovery.

Lecture gaps: "A2A concepts borrowed; protocol not implemented" and "static
registry, no runtime discovery". Every agent now publishes an A2A-style Agent
Card GENERATED from its declared capability/contract/comms (no hand-maintained
duplicate), served on GET /api/agents/cards and, per agent container, on
GET /.well-known/agent.json. The registry can load + validate a card (from a
dict or over HTTP) and resolve an agent by skill.
"""
import copy

import pytest
from fastapi.testclient import TestClient

from app.agents import registry
from app.main import app
from app.microservices.agent_app import build_agent_app
from app.tools import registry as tool_registry

client = TestClient(app)


def test_every_agent_card_is_schema_valid():
    cards = registry.agent_cards()
    assert [c["slug"] for c in cards] == list(registry.PIPELINE_ORDER)
    for card in cards:
        assert registry.load_card(card) is card
        for skill in card["skills"]:
            assert skill["inputSchema"]["type"] == "object"
            assert skill["outputSchema"]["type"] == "object"


def test_cards_are_generated_from_the_declarations_not_copied():
    routing = registry.agent_card("routing")
    cls = registry.AGENT_CLASSES["routing"]
    assert {s["id"] for s in routing["skills"]} == set(cls.COMMS.publishes)
    out = routing["skills"][0]["outputSchema"]["properties"]
    assert set(out["result"]["required"]) == set(cls.CONTRACT.returns)
    assert set(out["state_patch"]["properties"]) == set(cls.CONTRACT.writes)
    assert [t["name"] for t in routing["tools"]] == list(cls.CAPABILITY.tools)
    assert routing["description"] == cls.CAPABILITY.reasoning


@pytest.mark.parametrize("mutate", [
    lambda c: c.pop("skills"),
    lambda c: c.update(skills=[]),
    lambda c: c.update(transport="carrier-pigeon"),
    lambda c: c["skills"][0].pop("inputSchema"),
    lambda c: c["skills"].append(copy.deepcopy(c["skills"][0])),  # duplicate skill id
    lambda c: c.update(name=""),
    lambda c: c["authentication"].update(required="yes"),
])
def test_malformed_cards_are_rejected(mutate):
    card = copy.deepcopy(registry.agent_card("routing"))
    mutate(card)
    with pytest.raises(registry.CardError):
        registry.load_card(card)


def test_resolve_an_agent_by_skill():
    assert registry.resolve_skill("care.routed")["slug"] == "routing"
    assert registry.resolve_skill("acuity.classified")["slug"] == "classifier"
    assert registry.resolve_skill("no.such.skill") is None


def test_api_serves_all_cards():
    body = client.get("/api/agents/cards").json()
    assert [c["slug"] for c in body["cards"]] == list(registry.PIPELINE_ORDER)
    for card in body["cards"]:
        registry.load_card(card)


def test_agent_container_serves_its_well_known_card_and_it_is_discoverable():
    agent_client = TestClient(build_agent_app("routing"))
    reply = agent_client.get("/.well-known/agent.json")
    assert reply.status_code == 200
    card = registry.fetch_card("http://testserver", client=agent_client)
    assert card["slug"] == "routing"
    assert registry.resolve_skill("care.routed", cards=[card]) is card


def test_fetch_card_rejects_an_invalid_remote_card():
    class _Reply:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"name": "impostor"}

    class _Client:
        def get(self, _url):
            return _Reply()

    with pytest.raises(registry.CardError):
        registry.fetch_card("http://evil", client=_Client())


def test_card_tools_agree_with_the_mcp_server():
    """A tool a card says is reachable over MCP is exactly one the MCP server
    publishes, and every MCP tool is used by some agent."""
    advertised = {t["mcpName"] for c in registry.agent_cards() for t in c["tools"] if t["mcpName"]}
    assert advertised == set(tool_registry.mcp_tools())
    for card in registry.agent_cards():
        for tool in card["tools"]:
            assert tool["gateway"] == (tool["name"] in tool_registry.REGISTRY)
