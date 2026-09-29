"""Clinician-Handoff agent — owner: Heriz Yusoff.   Run with: pytest -m handoff

The agent is now merged into the shared package and the pipeline, so this
imports it from `app.agents` like every other worker's test does. The `handoff`
marker is registered in pytest.ini.
"""
import pytest

from app.agents import ClinicianHandoffAgent
from app.agents.capability import ORCHESTRATOR_SLUG
from harness import emit_and_check, make_case, run_and_check

pytestmark = pytest.mark.handoff


def test_handoff_is_a_noop_when_not_escalated():
    """The common case: most cases never reach HITL, so this agent must not do
    (LLM/RAG) work, and must not write anything, when escalated is False."""
    result, state = run_and_check(ClinicianHandoffAgent(), make_case(escalated=False))
    assert result["source"] == "not_escalated"
    assert state.handoff_summary == ""
    assert state.handoff_citations == []
    assert state.handoff_questions == []


def test_handoff_produces_a_summary_when_escalated():
    result, state = run_and_check(
        ClinicianHandoffAgent(),
        make_case(
            escalated=True,
            escalation_reason="Safety-override triggered: possible acute coronary syndrome.",
            safety_triggered=True,
            safety_rule="cardiac_chest_pain",
            safety_reason="chest pain radiating to left arm",
            acuity_code="P1_RESUSCITATION",
            confidence=0.9,
        ),
    )
    assert result["source"] in {"llm", "fallback"}  # LLM disabled in tests -> fallback
    assert state.handoff_summary  # non-empty
    assert "chest pain" in state.handoff_summary.lower() or "cardiac_chest_pain" in state.handoff_summary
    # Deterministic fallback must never invent a follow-up question.
    assert state.handoff_questions == []


def test_handoff_grounds_only_when_a_safety_rule_fired():
    """[Agentic] The one retrieval decision this agent makes itself: ground a
    safety-triggered escalation, skip retrieval for a confidence-only one."""
    _grounded_result, grounded_state = run_and_check(
        ClinicianHandoffAgent(),
        make_case(
            escalated=True, safety_triggered=True, safety_rule="cardiac_chest_pain",
            safety_reason="chest pain", escalation_reason="Safety-override triggered.",
        ),
    )
    assert grounded_state.handoff_citations, "a fired safety rule should retrieve grounding citations"

    _ungrounded_result, ungrounded_state = run_and_check(
        ClinicianHandoffAgent(),
        make_case(
            escalated=True, safety_triggered=False, confidence=0.3,
            escalation_reason="Low classifier confidence.",
        ),
    )
    assert ungrounded_state.handoff_citations == [], (
        "a confidence-only escalation has no rule-table entry to ground — retrieval should be skipped"
    )


def test_handoff_summary_never_exceeds_the_length_cap():
    from app.agents.handoff import _MAX_SUMMARY_CHARS

    _result, state = run_and_check(
        ClinicianHandoffAgent(),
        make_case(
            escalated=True, escalation_reason="x" * 1000,
            normalised_symptoms="y" * 1000,
        ),
    )
    assert len(state.handoff_summary) <= _MAX_SUMMARY_CHARS


def test_handoff_emits_handoff_ready_message():
    msg = emit_and_check(
        ClinicianHandoffAgent(),
        make_case(escalated=True, escalation_reason="Safety-override triggered."),
    )
    assert msg.intent == "handoff.ready"
    # Addressed to whoever holds the orchestrator role, not a hard-coded name.
    # This asserted "supervisor" — an agent deleted by the orchestrator
    # takeover — and passed only because handoff.py hard-coded the same dead
    # name. Both sides now read the constant.
    assert msg.recipient == ORCHESTRATOR_SLUG
    assert "summary" in msg.payload


def test_handoff_never_writes_outside_its_lane():
    """Redundant with the generic contract test, but explicit here because this
    agent reads (and must never touch) acuity/escalated/escalation_reason —
    the exact fields HITL and Reflection own."""
    _result, state = run_and_check(
        ClinicianHandoffAgent(),
        make_case(escalated=True, acuity_code="P2_EMERGENT", escalation_reason="Safety-override triggered."),
    )
    assert state.acuity_code == "P2_EMERGENT"  # unchanged
    assert state.escalated is True  # unchanged


def test_grounding_retrieval_runs_off_the_event_loop(monkeypatch):
    """`rag.retrieve` is synchronous (urllib + a TF-IDF fit), and this agent
    calls it from an async `run()`. Blocking the loop here stalls every other
    in-flight triage and the SSE heartbeat, so the call belongs on a thread —
    the same treatment Care-Routing already gives its blocking work."""
    import asyncio
    import threading

    from app import rag

    seen: dict[str, int] = {}

    def _record(_query, top_k=2):
        seen["thread"] = threading.get_ident()
        return []

    monkeypatch.setattr(rag, "retrieve", _record)

    async def _drive() -> int:
        await ClinicianHandoffAgent().run(make_case(
            escalated=True, safety_triggered=True, safety_rule="chest_pain",
            safety_reason="crushing chest pain", escalation_reason="Safety-override triggered.",
        ))
        return threading.get_ident()

    loop_thread = asyncio.run(_drive())
    assert seen.get("thread") is not None, "rag.retrieve was never called"
    assert seen["thread"] != loop_thread, "rag.retrieve ran ON the event loop thread"


# ---------------------------------------------------------------------------
# Review fixes 2026-09-23
# ---------------------------------------------------------------------------
import asyncio as _asyncio
import json as _json

from app import llm as _llm
from app.agents import ClinicianHandoffAgent as _Handoff
from app.agents.base import CaseState as _CaseState


def _escalated_state():
    return _CaseState(raw_text="chest pain", normalised_symptoms="chest pain", acuity_code="P2_EMERGENT",
                      confidence=0.8, evidence=["chest pain"], escalated=True,
                      escalation_reason="test")


def test_a_prompt_build_failure_falls_back_instead_of_raising(monkeypatch):
    agent = _Handoff()
    monkeypatch.setattr(agent, "_build_prompt", lambda *_a, **_k: (_ for _ in ()).throw(TypeError("bad wire value")))
    result = _asyncio.run(agent.run(_escalated_state()))
    assert result["source"] == "fallback"
    assert result["summary"]


def test_follow_up_questions_are_length_capped(monkeypatch):
    async def _long(*_a, **_k):
        return _json.dumps({"summary": "ok", "follow_up_questions": ["x" * 5000]})

    monkeypatch.setattr(_llm, "complete", _long)
    state = _escalated_state()
    result = _asyncio.run(_Handoff().run(state))
    assert result["source"] == "llm"
    assert all(len(q) <= 300 for q in state.handoff_questions)
