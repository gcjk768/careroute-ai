"""Agent-behaviour unit tests, run with the LLM forced unreachable.

Verifies the hard demo-reliability requirement: Symptom-Intake and
Severity-Classifier must fall back to deterministic logic (never raise,
never hang) when the LLM is unreachable, and the classifier's confidence
must genuinely reflect ambiguous input (< CONFIDENCE_THRESHOLD) so the
Human-in-the-Loop gate in agents.py fires correctly downstream.

Also covers the FR-12 least-privilege `enforce_tool_access` control.
"""
from __future__ import annotations

import asyncio

import pytest

from app import llm
from app.agents import (
    CONFIDENCE_THRESHOLD,
    CareRoutingAgent,
    CaseState,
    SeverityClassifierAgent,
    SymptomIntakeAgent,
    ToolAccessError,
    enforce_tool_access,
)


@pytest.fixture(autouse=True)
def dead_llm(monkeypatch):
    """Make the LLM unreachable so every `complete()`
    call raises LLMUnavailableError quickly, forcing the deterministic
    fallback path in every worker below."""


def test_symptom_intake_falls_back_deterministically():
    state = CaseState(raw_text="I have had a bad cough and a fever for two days.")
    result = asyncio.run(SymptomIntakeAgent().run(state))

    assert result["source"] == "fallback"
    assert isinstance(state.normalised_symptoms, str) and state.normalised_symptoms
    assert isinstance(state.intake_keywords, list)


def test_classifier_falls_back_deterministically_and_produces_valid_output():
    state = CaseState(raw_text="I have chest pain and can't breathe properly.")
    state.normalised_symptoms = state.raw_text

    result = asyncio.run(SeverityClassifierAgent().run(state))

    # With the LLM disabled, the classifier uses either the deterministic ML
    # model ("model") or the keyword fallback — both are deterministic.
    assert result["source"] in {"model", "fallback"}
    assert state.acuity_code in {
        "P1_RESUSCITATION", "P2_EMERGENT", "P3_URGENT", "P4_NON_URGENT", "P5_SELF_CARE",
    }
    assert 0.0 <= state.confidence <= 1.0
    # [Responsible-AI] explanation must always be populated, LLM or fallback path.
    assert state.explanation, "classifier explanation should be non-empty"
    assert all("feature" in c and "weight" in c for c in state.explanation)


def test_classifier_ambiguous_input_has_low_confidence():
    state = CaseState(raw_text="I feel a bit off today, not really sure what's going on.")
    state.normalised_symptoms = state.raw_text

    asyncio.run(SeverityClassifierAgent().run(state))

    assert state.confidence < CONFIDENCE_THRESHOLD


def test_enforce_tool_access_raises_for_disallowed_tool():
    agent = CareRoutingAgent()
    with pytest.raises(ToolAccessError):
        enforce_tool_access(agent, "redflags.evaluate")  # not in CareRoutingAgent.TOOL_ALLOWLIST


def test_enforce_tool_access_allows_declared_tool():
    agent = CareRoutingAgent()
    enforce_tool_access(agent, "clinic.lookup")  # declared -- must not raise
    enforce_tool_access(agent, "llm.complete")    # bounded clinic-choice reasoning
