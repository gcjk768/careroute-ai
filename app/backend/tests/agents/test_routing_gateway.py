"""[Agentic] Care-Routing's tools go THROUGH the central tool gateway.

Lecture gap: "Care-Routing's tools bypass the central gateway". Until
2026-09-24 the three routing tools (clinic.lookup, facility.hours.lookup,
travel.estimate) were catalogued in tools/registry.py but executed natively
inside the agent, so the gateway's policy — two-key authorisation, argument
schema validation, per-case quota, the careroute_tool_calls_total metric —
never saw them. These tests pin that every routing tool call now passes the
gateway, that the gateway's per-case quota and context requirement hold, and
that behaviour is otherwise unchanged (the existing routing suites pin that).
"""
import asyncio
import json

import pytest

from app.agents import CareRoutingAgent, CaseState
from app.agents.routing import ROUTING_TOOL_REGISTRY
from app.tools import registry
from app.tools.clinic_lookup import Clinic
from app.tools.gpgowhere import HoursProfile

pytestmark = pytest.mark.routing

OPEN_ID = "chas-100001-open-clinic"


class _Lookup:
    def find_candidate_clinics(self, *_args, **_kwargs):
        return [
            Clinic("Open Clinic", "1 Test Road", "100001", "60000001", 1.301, 103.801, ["CHAS"]),
            Clinic("Far Clinic", "5 Test Road", "100005", "60000005", 1.320, 103.820, ["CHAS"]),
        ]


class _Hours:
    def match(self, *, postal, name):
        return HoursProfile(postal=postal, name=name, hours={}, twenty_four_hours=True, verified_at="test")


def _state(**extra):
    values = {"raw_text": "mild rash", "acuity_code": "P4_NON_URGENT", "latitude": 1.3, "longitude": 103.8,
              "transport_mode": "public"}
    values.update(extra)
    return CaseState(**values)


def _agent():
    return CareRoutingAgent(lookup=_Lookup(), hours=_Hours(), use_onemap=False)


@pytest.fixture
def gateway_log(monkeypatch):
    """Spy on the gateway without changing what it does."""
    seen = []
    real = registry.call

    def spy(agent, name, arguments, **kwargs):
        result = real(agent, name, arguments, **kwargs)
        seen.append({"agent": getattr(agent, "SLUG", None), "tool": name, "ok": result["ok"],
                     "context": kwargs.get("context") is not None})
        return result

    monkeypatch.setattr(registry, "call", spy)
    return seen


def test_every_deterministic_routing_tool_call_passes_the_gateway(gateway_log):
    result = asyncio.run(_agent().run(_state()))
    tools = {entry["tool"] for entry in gateway_log}

    assert result["source"] == "deterministic_fallback"  # LLM disabled in tests
    assert {"clinic.lookup", "facility.hours.lookup", "travel.estimate"} <= tools
    assert all(e["agent"] == "routing" and e["ok"] and e["context"] for e in gateway_log)


def test_a_model_chosen_react_tool_call_passes_the_gateway(gateway_log, monkeypatch):
    ask = json.dumps({"selected_clinic_id": OPEN_ID, "confidence": 0.8, "rationale": "x", "tradeoffs": [],
                      "tool_call": {"name": "facility.hours.lookup",
                                    "arguments": {"clinic_id": OPEN_ID, "transport": None}}})
    decide = json.dumps({"selected_clinic_id": OPEN_ID, "confidence": 0.8, "rationale": "Open and near.",
                         "tradeoffs": [], "tool_call": None})
    answers = iter([ask, decide])

    async def complete(*_a, **_k):
        return next(answers)

    import app.llm as llm
    monkeypatch.setattr(llm, "complete", complete)
    state = _state(transport_mode="walk")
    result = asyncio.run(_agent().run(state))

    assert result["source"] == "llm"
    hours_calls = [e for e in gateway_log if e["tool"] == "facility.hours.lookup"]
    # two deterministic candidate checks + the one the model asked for
    assert len(hours_calls) == 3
    assert state.routing_plan["tool_calls"][0]["observation"]["open_now"] is True


def test_p2_self_transport_route_passes_the_gateway(gateway_log):
    state = _state(acuity_code="P2_EMERGENT", transport_mode="walk", emergency_self_transport_confirmed=True)
    asyncio.run(_agent().run(state))
    assert [e["tool"] for e in gateway_log] == ["travel.estimate"]


def test_gateway_catalogue_is_the_schema_the_model_sees():
    """One source of truth: the ReAct registry is derived from the gateway."""
    for name, meta in ROUTING_TOOL_REGISTRY.items():
        spec = registry.REGISTRY[name]
        assert meta["parameters"] is spec.parameters
        assert spec.executor == "gateway" and spec.contextual
    assert registry.REGISTRY["clinic.lookup"].allowed_agents == frozenset({"routing"})


def test_contextual_tool_without_a_case_context_is_refused():
    result = registry.call(CareRoutingAgent(use_onemap=False), "travel.estimate",
                           {"clinic_id": OPEN_ID, "transport": "walk"})
    assert result["ok"] is False and result["outcome"] == "refused"
    assert "context" in result["error"]


def test_gateway_enforces_the_per_case_quota():
    agent = _agent()
    state = _state()
    clinic = Clinic("Open Clinic", "1 Test Road", "100001", "60000001", 1.301, 103.801, ["CHAS"])
    context = {"agent": agent, "state": state, "facilities": {OPEN_ID: clinic}}
    limit = registry.REGISTRY["travel.estimate"].per_case_limit
    assert limit is not None
    for _ in range(limit):
        assert registry.call(agent, "travel.estimate", {"clinic_id": OPEN_ID, "transport": "walk"},
                             context=context)["ok"]
    over = registry.call(agent, "travel.estimate", {"clinic_id": OPEN_ID, "transport": "walk"}, context=context)
    assert over["ok"] is False and over["outcome"] == "quota_exceeded"


def test_gateway_rejects_an_unverified_facility_as_an_error_observation():
    agent = _agent()
    context = {"agent": agent, "state": _state(), "facilities": {}}
    result = registry.call(agent, "facility.hours.lookup", {"clinic_id": "chas-999999-invented"}, context=context)
    assert result["ok"] is False and result["outcome"] == "error"


def test_gateway_validates_routing_arguments_before_running():
    agent = _agent()
    context = {"agent": agent, "state": _state(), "facilities": {}}
    result = registry.call(agent, "travel.estimate", {"clinic_id": OPEN_ID, "transport": "rocket"}, context=context)
    assert result["ok"] is False and result["outcome"] == "invalid_arguments"


def test_each_run_gets_a_fresh_quota(gateway_log):
    agent = _agent()
    for _ in range(3):
        result = asyncio.run(agent.run(_state()))
        assert result["source"] == "deterministic_fallback"
    assert all(e["ok"] for e in gateway_log)
