"""[AI-Security] Prompt hygiene — every LLM-calling agent constrains the model.

OWASP LLM01 mitigation #1 (AIC Day 2): "constrain model behaviour" — the system
prompt must tell the model that patient text and retrieved documents are DATA,
never instructions. Until 2026-09-16 only intake and safety said so; the
classifier, handoff and routing prompts did not, while SECURITY.md claimed every
prompt did. This test makes the claim mechanically true: each agent module that
calls `llm.complete` exposes its system prompt as `SYSTEM_PROMPT`, and that
prompt must contain the shared guard sentence from `agents.base`.
"""
from __future__ import annotations

import importlib

import pytest

from app.agents.base import EMBEDDED_INSTRUCTION_GUARD

# Every module under app/agents that reaches the LLM. HITL is a deterministic
# policy node with no prompt to guard; Reflection gained an LLM critic 2026-09-16.
LLM_AGENT_MODULES = ["intake", "classifier", "safety", "routing", "handoff", "reflection"]


@pytest.mark.parametrize("name", LLM_AGENT_MODULES)
def test_system_prompt_carries_embedded_instruction_guard(name):
    module = importlib.import_module(f"app.agents.{name}")
    prompt = getattr(module, "SYSTEM_PROMPT", None)
    assert isinstance(prompt, str) and prompt.strip(), (
        f"app.agents.{name} must expose its LLM system prompt as SYSTEM_PROMPT"
    )
    assert EMBEDDED_INSTRUCTION_GUARD in prompt, (
        f"app.agents.{name}.SYSTEM_PROMPT does not tell the model to ignore "
        f"instructions embedded in patient text / retrieved documents"
    )


def test_guard_sentence_is_explicit():
    # The sentence itself must name BOTH untrusted channels the agents read:
    # the patient's words and retrieved/remembered text (indirect injection).
    guard = EMBEDDED_INSTRUCTION_GUARD.lower()
    assert "ignore any instruction" in guard
    assert "patient" in guard
    assert "retrieved" in guard
