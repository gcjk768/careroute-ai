"""Opt-in live A2A integration: real Safety Override -> Care Routing.

Unlike the unit-level message traces, this test uses no fake agent, clinic
directory, map client, or LLM response. It is excluded from ordinary test runs
because it calls external providers and has a small OpenAI cost.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from app import config, llm
from app.agents import AgentMessage, CareRoutingAgent, CaseState, MessageBus, SafetyOverrideAgent
from app.agents.supervisor import Supervisor
from app.services.onemap import ONEMAP_EMAIL, ONEMAP_PASSWORD

pytestmark = [pytest.mark.routing, pytest.mark.integration, pytest.mark.comms]


def _live_enabled() -> bool:
    return os.environ.get("RUN_LIVE_A2A_ROUTING_TESTS") == "1"


@pytest.mark.parametrize(
    "scenario,case_text,classifier_acuity,latitude,longitude,transport,max_travel,expected_tier,safety_triggered,uses_live_routing",
    [
        (
            "safety_override_from_stale_gp",
            "Synthetic QA only: sudden crushing chest pain spreading to the left arm with sweating.",
            "P4_NON_URGENT", None, None, "walk", None,
            "Emergency Department", True, False,
        ),
        (
            "p2_emergent",
            "Synthetic QA only: severe but non-red-flag symptoms already assessed as emergent.",
            "P2_EMERGENT", None, None, "walk", None,
            "Emergency Department", False, False,
        ),
        (
            "p3_urgent",
            "Synthetic QA only: symptoms already assessed as urgent.",
            "P3_URGENT", None, None, "walk", None,
            "Urgent Care", False, False,
        ),
        (
            "p4_city_walk",
            "Synthetic QA only: mild rash for two days, no fever.",
            "P4_NON_URGENT", 1.3000, 103.8000, "walk", 45,
            "GP", False, True,
        ),
        (
            "p4_ang_mo_kio_walk",
            "Synthetic QA only: mild skin irritation, no red-flag symptoms.",
            "P4_NON_URGENT", 1.3521, 103.8198, "walk", 45,
            "GP", False, True,
        ),
        (
            "p5_telehealth",
            "Synthetic QA only: minor self-care concern already assessed as low acuity.",
            "P5_SELF_CARE", None, None, "walk", None,
            "Telehealth", False, False,
        ),
    ],
)
def test_live_safety_to_routing_a2a_uses_real_agents_and_providers(
    monkeypatch, scenario, case_text, classifier_acuity, latitude, longitude,
    transport, max_travel, expected_tier, safety_triggered, uses_live_routing,
):
    """Real A2A handoff preserves safety policy and routes GP cases with live providers."""
    if not _live_enabled():
        pytest.skip("set RUN_LIVE_A2A_ROUTING_TESTS=1 to run the live Safety -> Routing A2A test")
    if not config.OPENAI_API_KEY:
        pytest.skip("OPENAI_API_KEY is not configured")
    if not ONEMAP_EMAIL or not ONEMAP_PASSWORD:
        pytest.skip("ONEMAP_EMAIL and ONEMAP_PASSWORD are not configured")

    provider = llm.OpenAIProvider()
    llm_request: object | None = None
    raw_llm_response: str | None = None
    llm_response_summary: dict[str, object] = {}

    async def openai_only(system: str, prompt: str, json_mode: bool = False, json_schema: dict | None = None) -> str:
        nonlocal llm_request, raw_llm_response
        try:
            llm_request = json.loads(prompt)
        except (TypeError, ValueError):
            llm_request = {"parse": "routing prompt was not JSON"}
        raw = await provider.complete(system, prompt, json_mode, json_schema)
        raw_llm_response = raw
        try:
            decision = json.loads(raw)
            llm_response_summary.update({
                "selected_clinic_id": decision.get("selected_clinic_id"),
                "confidence": decision.get("confidence"),
                "rationale": decision.get("rationale"),
                "tradeoffs": decision.get("tradeoffs"),
                "rationale_length": len(str(decision.get("rationale", ""))),
                "tradeoff_count": len(decision.get("tradeoffs", [])) if isinstance(decision.get("tradeoffs"), list) else None,
            })
        except (TypeError, ValueError):
            llm_response_summary["parse"] = "non-JSON response"
        return raw

    # The global test fixture disables LLMs. Restore only the real OpenAI
    # provider for this explicitly requested integration test.
    monkeypatch.setattr(llm, "complete", openai_only)

    state = CaseState(
        raw_text=case_text,
        acuity_code=classifier_acuity,
        confidence=0.8,
        latitude=latitude,
        longitude=longitude,
        transport_mode=transport,
        max_travel_time_min=max_travel,
    )
    bus = MessageBus()
    classifier_message = bus.publish(AgentMessage(
        sender="classifier", recipient="broadcast", intent="acuity.classified",
        payload={"acuity_code": state.acuity_code, "confidence": state.confidence, "evidence": ["synthetic QA"]},
    ))

    # Use the real Supervisor protocol request, then the real Safety agent and
    # its emitted response. This is the same A2A shape used in orchestration.
    supervisor = Supervisor()
    safety_request = bus.publish(supervisor._safety_request(state, classifier_message.seq))
    safety = SafetyOverrideAgent()
    safety.consume(bus.inbox(safety))
    safety_result = safety.run(state)
    bus.publish(safety.emit(state))
    protocol_issues = supervisor._verify_safety_response(bus, state, safety_request.seq)

    routing = CareRoutingAgent()
    routing.consume(bus.inbox(routing))
    routing_result = asyncio.run(routing.run(state))
    bus.publish(routing.emit(state))

    print("\n[LIVE A2A SAFETY -> CARE ROUTING TRACE]")
    print(json.dumps({
        "scenario": scenario,
        "synthetic_case": {
            "case_text": case_text,
            "classifier_acuity": classifier_acuity,
            "location": {"latitude": latitude, "longitude": longitude} if latitude is not None else None,
            "transport_mode": transport,
            "max_travel_time_min": max_travel,
        },
        "safety_result": safety_result,
        "safety_protocol_issues": protocol_issues,
        "exact_llm_input": llm_request,
        "raw_llm_response": raw_llm_response,
        "llm_response_summary": llm_response_summary,
        "routing_result": routing_result,
        "a2a_message_history": bus.history(),
    }, indent=2, default=str))

    assert safety_result["triggered"] is safety_triggered
    assert protocol_issues == []
    assert routing_result["care_tier"] == expected_tier
    if uses_live_routing:
        assert routing_result["source"] == "llm"
        assert routing_result["selected_clinic_id"].startswith("chas-")
        assert routing_result["travel_estimate_source"] == "onemap_route"
        assert routing_result["route_available"] is True
        assert isinstance(llm_response_summary.get("tradeoffs"), list)
    else:
        assert routing_result["source"] == "safety_floor"
        assert llm_request is None
        assert raw_llm_response is None
    assert [message["intent"] for message in bus.history()] == [
        "acuity.classified", "safety.assessment.requested", "safety.override", "care.routed",
    ]
