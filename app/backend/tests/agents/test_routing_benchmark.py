"""Synthetic, offline quality benchmark for Care Routing.

This is not a clinical validation dataset and does not call OpenAI, OneMap, or
GPGoWhere. It is a repeatable regression benchmark that pins the safety and
logistics behaviours the agent must preserve as its prompts/tools evolve.
Run with: pytest tests/agents/test_routing_benchmark.py -s -v
"""
import asyncio
import json

import pytest

from app.agents import CareRoutingAgent, CaseState
from app.tools.clinic_lookup import Clinic
from app.tools.gpgowhere import HoursProfile

pytestmark = pytest.mark.routing


class _Lookup:
    def find_candidate_clinics(self, *_args, **_kwargs):
        return [
            Clinic("Open Clinic", "1 Test Road", "100001", "60000001", 1.301, 103.801, ["CHAS"]),
            Clinic("Closed Clinic", "2 Test Road", "100002", "60000002", 1.302, 103.802, ["CHAS"]),
        ]


class _Hours:
    def match(self, *, postal, name):
        if name == "Closed Clinic":
            return HoursProfile(postal=postal, name=name, hours={})
        return HoursProfile(postal=postal, name=name, hours={}, twenty_four_hours=True, verified_at="test")


def _agent():
    return CareRoutingAgent(lookup=_Lookup(), hours=_Hours(), use_onemap=False)


def _state(**overrides):
    values = {
        "raw_text": "Synthetic benchmark case only.",
        "acuity_code": "P4_NON_URGENT",
        "latitude": 1.3,
        "longitude": 103.8,
        "transport_mode": "walk",
        "max_travel_time_min": 30,
    }
    values.update(overrides)
    return CaseState(**values)


async def _valid_llm(*_args, **_kwargs):
    return json.dumps({
        "selected_clinic_id": "chas-100001-open-clinic",
        "confidence": 0.88,
        "rationale": "Verified, open, and within the stated travel limit.",
        "tradeoffs": [],
        "tool_call": None,
    })


@pytest.mark.parametrize(
    "name,state,expected_tier,expected_source",
    [
        ("P1 remains Emergency Department", _state(acuity_code="P1_RESUSCITATION"), "Emergency Department", "safety_floor"),
        ("P2 remains Emergency Department", _state(acuity_code="P2_EMERGENT"), "Emergency Department", "safety_floor"),
        ("P3 remains Urgent Care", _state(acuity_code="P3_URGENT"), "Urgent Care", "safety_floor"),
        ("P5 remains Telehealth", _state(acuity_code="P5_SELF_CARE"), "Telehealth", "safety_floor"),
        ("missing location is an honest fallback", _state(latitude=None, longitude=None), "GP", "fallback"),
        ("missing transport requests clarification", _state(transport_mode="unknown"), "GP", "fallback"),
    ],
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_routing_safety_and_input_benchmark(name, state, expected_tier, expected_source):
    """Benchmark clinical tier lock and incomplete-input behaviour."""
    result = asyncio.run(_agent().run(state))
    print(f"[ROUTING BENCHMARK PASS] {name}")
    assert result["care_tier"] == expected_tier
    assert result["source"] == expected_source
    if name == "missing transport requests clarification":
        assert result["routing_clarification"]["kind"] == "transport_mode"


def test_routing_benchmark_valid_llm_choice_passes_final_evaluator(monkeypatch):
    """A legitimate constrained choice remains available and auditable."""
    import app.llm as llm
    monkeypatch.setattr(llm, "complete", _valid_llm)

    result = asyncio.run(_agent().run(_state()))

    print("[ROUTING BENCHMARK PASS] valid verified GP choice")
    assert result["source"] == "llm"
    assert result["clinic"] == "Open Clinic"
    assert result["decision_evaluation"]["status"] == "accepted"
    assert all(result["decision_evaluation"]["checks"].values())


def test_routing_benchmark_invented_clinic_falls_back(monkeypatch):
    """A model cannot turn a plausible answer into an unverified destination."""
    async def invented(*_args, **_kwargs):
        return json.dumps({
            "selected_clinic_id": "invented-clinic",
            "confidence": 0.99,
            "rationale": "Use this clinic.",
            "tradeoffs": [],
            "tool_call": None,
        })

    import app.llm as llm
    monkeypatch.setattr(llm, "complete", invented)

    result = asyncio.run(_agent().run(_state()))

    print("[ROUTING BENCHMARK PASS] invented clinic rejected")
    assert result["source"] == "deterministic_fallback"
    assert result["clinic"] == "Open Clinic"


def test_routing_benchmark_final_evaluator_rejects_a_post_selection_constraint_failure(monkeypatch):
    """Simulates corrupted post-LLM logistics; the evaluator must stop it."""
    agent = _agent()

    async def unsafe_select(_state, candidates, **_kwargs):
        unsafe = {**candidates[0], "travel_time_min": 999}
        return unsafe, {
            "confidence": 0.9,
            "rationale": "Unsafe synthetic selection.",
            "tradeoffs": [],
            "tool_trace": [],
        }, "llm"

    monkeypatch.setattr(agent, "_select", unsafe_select)
    result = asyncio.run(agent.run(_state()))

    print("[ROUTING BENCHMARK PASS] final evaluator rejects constraint failure")
    assert result["source"] == "deterministic_fallback"
    assert result["decision_evaluation"]["status"] == "rejected"
    assert result["decision_evaluation"]["fallback_applied"] is True
    assert "travel_time_valid" in result["decision_evaluation"]["failed_checks"]
