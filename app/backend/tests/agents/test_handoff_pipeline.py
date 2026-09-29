"""Clinician-Handoff PIPELINE WIRING — owner: platform (James).

    pytest -m handoff

`tests/agents/test_handoff.py` (Heriz) tests the agent in isolation: given a
CaseState, does it build a faithful packet. This file tests the thing that file
deliberately could not — that the orchestrator actually CALLS it, and calls it
in the one position where its output is correct.

WHY THIS FILE EXISTS
--------------------
`docs/vault/Clinician Handoff Pipeline Integration.md` §4.1 names the ordering
as the first non-negotiable constraint of the integration:

    "Ordering: after Reflection, not after HITL. Summarising before
     Reflection's severity backstop can run produces a summary for a decision
     that's about to change."

That is a real hazard with a silent failure mode. `ReflectionAgent.run()` sets
`state.escalated = True` on any P1/P2 case nothing upstream flagged
(`reflection.py`, the `_SEVERE_CODES` check). A handoff step placed after HITL
would observe `escalated is False` on exactly those cases, no-op, and hand the
clinician queue an escalation with an EMPTY summary — no error, no test
failure, just a missing packet on the most severe cases in the system.

Nothing else in the suite covers this: no other test drives `orchestrate()`
directly, and the agent's own tests build a CaseState by hand, so they cannot
observe call order.
"""
from __future__ import annotations

import pytest

from app.agents import AGENT_LABELS, CaseState, SymptomIntakeAgent

pytestmark = pytest.mark.handoff


async def _no_delay() -> None:
    return None


async def _drive(orchestrator: SymptomIntakeAgent, state: CaseState) -> list[dict]:
    """Run the whole pipeline, collecting the SSE events in order."""
    return [
        event
        async for event in orchestrator.orchestrate(
            state, audit=lambda **_kw: None, log=lambda *_a, **_kw: None, delay=_no_delay
        )
    ]


def _results(events: list[dict]) -> list[str]:
    """Agent slugs in the order they produced a result, i.e. actual call order."""
    return [e["agent"] for e in events if e.get("event") == "agent_result"]


@pytest.mark.asyncio
async def test_handoff_runs_last_and_produces_a_packet_for_an_escalated_case():
    orchestrator = SymptomIntakeAgent()
    state = CaseState(raw_text="crushing chest pain radiating to my left arm, sweating")

    events = await _drive(orchestrator, state)
    order = _results(events)

    assert state.escalated, "a crushing-chest-pain case must escalate"
    assert order[-1] == "handoff", f"handoff must be the LAST worker to run, got {order}"
    assert order.index("handoff") > order.index("reflection")
    assert state.handoff_summary, "an escalated case must leave a non-empty packet"


@pytest.mark.asyncio
async def test_handoff_is_skipped_entirely_when_no_escalation():
    """The common case. No packet, and no wasted agent_active/agent_result pair
    advertising a step that did nothing."""
    orchestrator = SymptomIntakeAgent()
    state = CaseState(raw_text="mild sore throat for two days, no fever")

    events = await _drive(orchestrator, state)

    if state.escalated:  # pragma: no cover - defensive; this input should not escalate
        pytest.skip("input escalated; this test needs a non-escalating case")

    assert "handoff" not in _results(events)
    assert state.handoff_summary == ""
    assert state.handoff_citations == []
    assert state.handoff_questions == []


@pytest.mark.asyncio
async def test_handoff_still_runs_when_reflection_escalates_a_case_hitl_did_not(monkeypatch):
    """THE REGRESSION GUARD for the ordering constraint.

    Force the exact situation the constraint exists for: HITL declines to
    escalate a P1 case, and Reflection's severity backstop then forces the
    escalation. A handoff step placed after HITL would see `escalated is False`
    and silently produce nothing. Placed after Reflection, it must produce a
    packet.
    """
    orchestrator = SymptomIntakeAgent()

    real_hitl_run = orchestrator.hitl.run

    def _hitl_declines_to_escalate(state: CaseState) -> dict:
        result = real_hitl_run(state)
        # Undo whatever HITL decided — we need a P1 arriving at Reflection
        # unescalated, which is precisely what the backstop is there to catch.
        state.escalated = False
        state.escalation_reason = None
        result["escalated"] = False
        result["reason"] = None
        return result

    monkeypatch.setattr(orchestrator.hitl, "run", _hitl_declines_to_escalate)

    state = CaseState(raw_text="crushing chest pain radiating to my left arm, sweating")
    events = await _drive(orchestrator, state)

    assert state.acuity_code in {"P1_RESUSCITATION", "P2_EMERGENT"}, state.acuity_code
    # Reflection, not HITL, is what escalated this case.
    assert state.escalated, "Reflection's severity backstop must force the escalation"
    assert "Reflection:" in (state.escalation_reason or "")
    # ...and the handoff packet was still built, which is the whole point.
    assert "handoff" in _results(events)
    assert state.handoff_summary, "late escalation must still produce a handoff packet"


@pytest.mark.asyncio
async def test_handoff_announces_its_packet_to_the_orchestrator():
    """The packet must reach the bus, addressed to whoever holds the
    orchestrator role — not to the deleted `supervisor` agent."""
    from app.agents.capability import ORCHESTRATOR_SLUG

    orchestrator = SymptomIntakeAgent()
    state = CaseState(raw_text="crushing chest pain radiating to my left arm, sweating")

    await _drive(orchestrator, state)

    handoff_messages = [m for m in state.messages if m["intent"] == "handoff.ready"]
    assert len(handoff_messages) == 1, state.messages
    assert handoff_messages[0]["sender"] == "handoff"
    assert handoff_messages[0]["recipient"] == ORCHESTRATOR_SLUG


def test_handoff_has_a_label_for_the_sse_step_event():
    """`orchestrate()` reads AGENT_LABELS["handoff"] on every escalated case; a
    missing entry would be a KeyError in production, not a test failure."""
    assert AGENT_LABELS["handoff"] == "Clinician Handoff"
