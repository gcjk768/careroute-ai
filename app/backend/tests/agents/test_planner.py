"""Per-case planner (agents/planner.py) and the orchestrator executing it.

[Agentic] Plan-and-Execute: the plan is chosen per case, but every plan is
validated against an allowed-transition graph, and anything rejected — or a
planner that raises — runs today's fixed full sequence instead. The pipeline
tests below drive the real SSE stream with the LLM off (root conftest).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from harness import make_case

from app import metrics
from app.agents import planner
from app.agents.planner import FULL_PLAN, Plan, PlanStep, make_plan, plan_case, validate
from app.audit import audit_log
from app.models import TriageRequest

CHEST_PAIN = "Sudden crushing chest pain radiating to my left arm, sweating and short of breath."
MILD = "I have a mild sore throat and a runny nose, no fever."
VAGUE = "I feel a bit off today, not really sure what's going on."


# ---------------------------------------------------------------- pure planner
def test_p1_plans_emergency_guidance_instead_of_routing():
    plan = plan_case(make_case(acuity_code="P1_RESUSCITATION", safety_triggered=True))
    assert plan.shape == "emergency" and plan.urgent
    assert plan.names == ("emergency_guidance", "hitl", "reflection", "handoff")


def test_red_flag_below_p1_keeps_routing_for_the_nearest_ed():
    plan = plan_case(make_case(acuity_code="P2_EMERGENT", safety_triggered=True, confidence=0.9))
    assert plan.shape == "red_flag" and plan.urgent
    assert plan.names[0] == "routing"


def test_a_proposed_question_puts_hitl_before_routing():
    plan = plan_case(make_case(acuity_code="P4_NON_URGENT", confidence=0.4,
                               clarification={"feature": "fever", "question": "Fever?", "gain": 1.0}))
    assert plan.shape == "clarify"
    assert plan.names == ("hitl", "routing", "reflection", "handoff")


def test_low_confidence_and_normal_cases():
    assert plan_case(make_case(acuity_code="P4_NON_URGENT", confidence=0.3)).shape == "low_confidence"
    assert plan_case(make_case(acuity_code="P4_NON_URGENT", confidence=0.9)) is FULL_PLAN


def test_every_planned_shape_passes_its_own_validator():
    for state in (
        make_case(acuity_code="P1_RESUSCITATION", safety_triggered=True),
        make_case(acuity_code="P2_EMERGENT", safety_triggered=True),
        make_case(acuity_code="P4_NON_URGENT", confidence=0.4, clarification={"gain": 1.0}),
        make_case(acuity_code="P4_NON_URGENT", confidence=0.3),
        make_case(acuity_code="P5_SELF_CARE", confidence=0.9),
    ):
        assert validate(plan_case(state), state) == []


@pytest.mark.parametrize("steps, acuity, expected", [
    (("routing", "hitl", "handoff"), "P4_NON_URGENT", "missing mandatory"),       # Reflection planned away
    (("routing", "hitl", "reflection"), "P4_NON_URGENT", "missing mandatory"),    # handoff dropped
    (("emergency_guidance", "hitl", "reflection", "handoff"), "P4_NON_URGENT", "not allowed at"),
    (("reflection", "routing", "hitl", "handoff"), "P4_NON_URGENT", "transition start -> reflection"),
    (("routing", "hitl", "routing", "reflection", "handoff"), "P4_NON_URGENT", "repeated"),
    (("hitl", "reflection", "handoff"), "P4_NON_URGENT", "exactly one of routing"),
    (("teleport", "hitl", "reflection", "handoff"), "P4_NON_URGENT", "transition start -> teleport"),
])
def test_validator_rejects_unsafe_plans(steps, acuity, expected):
    plan = Plan("bad", tuple(PlanStep(s, "x") for s in steps))
    issues = validate(plan, make_case(acuity_code=acuity))
    assert any(expected in i for i in issues), issues


def test_rejected_or_crashing_planner_falls_back_to_the_fixed_sequence():
    def crash(_state):
        raise RuntimeError("boom")

    def skip_reflection(_state):
        return Plan("sneaky", (PlanStep("routing", "x"), PlanStep("hitl", "x"), PlanStep("handoff", "x")))

    for bad in (crash, skip_reflection):
        plan = make_plan(make_case(acuity_code="P4_NON_URGENT"), planner=bad)
        assert plan.shape == "fallback"
        assert plan.names == FULL_PLAN.names
        assert plan.fallback_reason


# ---------------------------------------------------------------- pipeline
@pytest.fixture(autouse=True)
def _no_ui_delay(monkeypatch):
    import app.main as main_mod

    async def _no_delay(*_a, **_k):
        return None

    monkeypatch.setattr(main_mod, "_step_delay", _no_delay)


def _run(text: str) -> list[dict]:
    from app.main import _triage_event_stream

    async def collect() -> list[dict]:
        out = []
        async for chunk in _triage_event_stream(TriageRequest(text=text, language="en", isVoice=False)):
            line = chunk.strip()
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
        return out

    return asyncio.run(collect())


def _events(events, name):
    return [e for e in events if e.get("event") == name]


def _plan_count(shape: str) -> float:
    if metrics.PLAN_SHAPES is None:
        return 0.0
    return metrics.PLAN_SHAPES.labels(shape=shape)._value.get()


def test_p1_stream_emits_plan_skips_clinic_search_and_still_reflects():
    before = _plan_count("emergency")
    events = _run(CHEST_PAIN)

    [plan] = _events(events, "plan")
    assert plan["shape"] == "emergency"
    assert [s["step"] for s in plan["steps"]] == ["emergency_guidance", "hitl", "reflection", "handoff"]
    assert all(s["rationale"] for s in plan["steps"])
    # The plan is announced before any post-Safety step runs.
    order = [e["event"] + ":" + e.get("agent", "") for e in events]
    assert order.index("plan:") < order.index("agent_active:routing")

    routing = next(e for e in _events(events, "agent_result") if e["agent"] == "routing")
    assert routing["data"]["source"] == "plan"   # Care-Routing agent not called

    final = _events(events, "final")[0]
    assert final["acuity"]["code"] == "P1_RESUSCITATION"     # red-flag recall holds
    assert final["escalated"] is True
    assert final["careTier"] == "Emergency Department"
    assert "995" in final["clinic"]
    assert final["plan"]["shape"] == "emergency"
    assert "reflection" in [e["agent"] for e in _events(events, "agent_result")]
    # Reflection judged the plan's care.routed announcement as sound.
    assert not any("never announced" in i for i in final["reflection"]["issues"])

    trail = audit_log.for_case(final["caseId"])
    assert any(e["action"] == "plan" and "shape=emergency" in e["detail"] for e in trail)
    if metrics.enabled():
        assert _plan_count("emergency") == before + 1


def test_mild_case_runs_the_full_plan_in_the_fixed_order():
    events = _run(MILD)
    assert _events(events, "plan")[0]["shape"] == "full"
    assert [e["agent"] for e in _events(events, "agent_active")] == [
        "intake", "classifier", "safety", "routing", "hitl", "reflection"]


def test_vague_case_asks_before_routing():
    """The clarify-first plan puts HITL before Care-Routing, and since the
    2026-09-26 interview an ask ENDS the turn there: routing, reflection and
    handoff wait for the answer, so a question costs the patient no OneMap
    round-trip."""
    events = _run(VAGUE)
    assert _events(events, "plan")[0]["shape"] == "clarify"
    active = [e["agent"] for e in _events(events, "agent_active")]
    assert active[-1] == "hitl"
    assert "routing" not in active and "reflection" not in active
    final = _events(events, "final")[0]
    assert final["clarification"]["question"].endswith("?")
    assert final["interview"]["done"] is False
    assert final["careTier"]   # the patient still has a care tier while answering


def test_an_invalid_plan_runs_the_fixed_sequence(monkeypatch):
    # A buggy planner that tries to skip Care-Routing on a mild case.
    monkeypatch.setattr(planner, "plan_case", lambda _s: Plan("bad", (
        PlanStep("emergency_guidance", "x"), PlanStep("hitl", "x"),
        PlanStep("reflection", "x"), PlanStep("handoff", "x"))))
    events = _run(MILD)
    plan = _events(events, "plan")[0]
    assert plan["shape"] == "fallback" and "not allowed" in plan["fallback_reason"]
    assert [e["agent"] for e in _events(events, "agent_active")] == [
        "intake", "classifier", "safety", "routing", "hitl", "reflection"]
    routing = next(e for e in _events(events, "agent_result") if e["agent"] == "routing")
    assert routing["data"].get("source") != "plan"
