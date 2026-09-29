"""[Microservices] An agent container that does not answer never makes the outcome less safe."""
from __future__ import annotations

import asyncio

import pytest

from app.agents import SymptomIntakeAgent
from app.agents.base import AgentUnavailableError, CaseState
from app.agents.hitl import HumanInTheLoopAgent
from app.agents.routing import CareRoutingAgent, tier_for_acuity, tier_rank
from app.microservices.workers import AGENT_CLASSES

CHEST_PAIN = "crushing chest pain radiating to my left arm and sweating"
MILD = "I have had a mild headache since this morning"


class _Down:
    """A RemoteAgent whose container is gone."""

    def __init__(self, slug: str) -> None:
        cls = AGENT_CLASSES[slug]
        self.SLUG, self.COMMS, self.CONTRACT = cls.SLUG, cls.COMMS, cls.CONTRACT
        self.TOOL_ALLOWLIST = getattr(cls, "TOOL_ALLOWLIST", [])

    def consume(self, inbox) -> None:
        pass

    async def run(self, state):
        raise AgentUnavailableError(f"{self.SLUG} down")

    async def areason(self, state):
        raise AgentUnavailableError(f"{self.SLUG} down")

    async def prescreen(self, text):
        raise AgentUnavailableError(f"{self.SLUG} down")

    async def emit(self, state):
        raise AgentUnavailableError(f"{self.SLUG} down")


class _SilentHitl(HumanInTheLoopAgent):
    """A HITL that never escalates: nothing upstream of Reflection flags the case."""

    def run(self, state):
        state.escalated = False
        state.escalation_reason = ""
        return {"source": "stub", "action": "proceed"}


class _UnderRouting(CareRoutingAgent):
    """Routes, then parks the case on the least cautious tier."""

    async def run(self, state):
        result = await super().run(state)
        state.care_tier = "Telehealth"
        return result


class _MuteRouting(CareRoutingAgent):
    """Routing whose run succeeds but whose announcement never arrives."""

    async def emit(self, state):
        raise AgentUnavailableError("routing emit lost")


def _drive(down: str, text: str, **workers) -> tuple[CaseState, list[dict]]:
    orchestrator = SymptomIntakeAgent(**{down: _Down(down)}, **workers)
    state = CaseState(raw_text=text)

    async def _no_delay():
        return None

    async def go():
        return [e async for e in orchestrator.orchestrate(
            state, audit=lambda **_kw: None, log=lambda *_a: None, delay=_no_delay)]

    return state, asyncio.run(go())


def _results(events, agent):
    return [e for e in events if e.get("event") == "agent_result" and e.get("agent") == agent]


@pytest.mark.parametrize("down", ["classifier", "safety", "routing", "hitl"])
def test_a_decision_agent_down_forces_human_review(down):
    state, events = _drive(down, MILD)
    assert state.escalated is True
    assert "unavailable" in (state.escalation_reason or "")
    assert _results(events, down)[0]["data"]["source"] == "unavailable"
    assert _results(events, "reflection"), "the pipeline must still complete"


def test_safety_down_fails_closed_at_the_route_gate():
    state, events = _drive("safety", CHEST_PAIN)
    assert state.escalated is True
    assert any(e.get("event") == "safety_protocol_issue" for e in events)


def test_routing_down_still_gives_the_care_tier():
    state, _ = _drive("routing", MILD)
    assert state.care_tier == tier_for_acuity(state.acuity_code)
    assert "unavailable" in (state.routing_reason or "")


def test_reflection_down_stops_the_loop_and_completes():
    state, _ = _drive("reflection", MILD)
    assert state.reflection["source"] == "unavailable"
    assert state.reflection["loop"]["stopReason"] == "agent_unavailable"
    assert state.escalated is True
    assert "Reflection" in state.escalation_reason and "unavailable" in state.escalation_reason


def test_handoff_down_leaves_no_packet():
    state, events = _drive("handoff", CHEST_PAIN)
    assert state.escalated is True
    assert state.handoff_summary == ""
    assert _results(events, "handoff")[0]["data"]["source"] == "unavailable"


def test_reflection_down_still_escalates_a_severe_case_nothing_else_flagged():
    """Reflection's P1/P2 backstop must not vanish with its container."""
    state, events = _drive("reflection", CHEST_PAIN, hitl=_SilentHitl())
    assert state.acuity_code in ("P1_RESUSCITATION", "P2_EMERGENT")
    assert state.escalated is True
    handoff = _results(events, "handoff")
    assert handoff, "the handoff step must run for an escalated case"
    assert handoff[0]["data"]["source"] != "unavailable"


def test_reflection_down_still_applies_the_tier_floor():
    state, _ = _drive("reflection", CHEST_PAIN, routing=_UnderRouting())
    assert tier_rank(state.care_tier) >= tier_rank(tier_for_acuity(state.acuity_code))


def test_reflection_down_never_lowers_a_more_cautious_tier():
    orchestrator = SymptomIntakeAgent()
    state = CaseState(raw_text=MILD, acuity_code="P5_SELF_CARE", care_tier="Emergency Department")
    orchestrator._degrade(state, "reflection", AgentUnavailableError("down"))
    assert state.care_tier == "Emergency Department"


def test_a_lost_decision_announcement_escalates():
    state, events = _drive("handoff", MILD, routing=_MuteRouting())
    assert state.escalated is True
    assert "Care Routing unavailable" in state.escalation_reason
    assert not [e for e in events if e.get("event") == "agent_message" and e.get("intent") == "care.routed"]


def test_repeated_degradation_does_not_repeat_the_reason():
    orchestrator = SymptomIntakeAgent()
    state = CaseState(raw_text=MILD)
    for _ in range(2):
        orchestrator._degrade(state, "classifier", AgentUnavailableError("down"))
    assert state.escalation_reason.count("Severity Classifier unavailable") == 1


def test_degraded_step_summaries_say_what_is_missing():
    _, events = _drive("classifier", MILD)
    assert _results(events, "classifier")[0]["summary"] == (
        "Severity Classifier unavailable — acuity not assessed; escalated for clinician review.")
    state, events = _drive("routing", MILD)
    assert _results(events, "routing")[0]["summary"] == (
        f"Care Routing unavailable — care tier {state.care_tier} only; clinician review requested.")
    _, events = _drive("handoff", CHEST_PAIN)
    assert _results(events, "handoff")[0]["summary"] == (
        "Clinician Handoff unavailable — no packet; case is in the review queue.")
