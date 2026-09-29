"""Shared per-agent test harness — the isolation layer.

Each team member tests ONLY their own agent here (see tests/agents/test_<x>.py),
run with `pytest -m <agent>`. The key primitive is `run_and_check`, which
enforces the agent's declared CONTRACT: it snapshots the CaseState, runs the
agent, and FAILS if the agent mutated a CaseState field outside its declared
`writes` lane, or omitted a key from its declared `returns`. That is what lets
five people edit five agents in parallel without silently breaking each other.

The root conftest.py disables the LLM for every test, so `run()` always takes
the deterministic path here.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
from dataclasses import fields

from app.agents import (
    AgentMessage,
    CareRoutingAgent,
    CaseState,
    ClinicianHandoffAgent,
    HumanInTheLoopAgent,
    ReflectionAgent,
    SafetyOverrideAgent,
    SeverityClassifierAgent,
    SymptomIntakeAgent,
)
from app.agents import registry

# Registry used by the parametrized boundary test in test_contracts.py.
#
# CHANGED 2026-09-15 (James): this used to be a literal dict here, with a comment
# asking the reader to keep it "in lock-step" with the app by hand. It is now
# RE-EXPORTED from `app.agents.registry`, which is the runtime Agent Registry
# (AAS Day 3 AM slide 18). A worker added to the app is therefore covered by the
# parametrized contract, comms and capability tests automatically — under the old
# arrangement, forgetting this dict meant a new agent silently had no boundary
# test, and nothing would have failed to tell you.
AGENT_CLASSES = registry.AGENT_CLASSES


def make_case(**overrides) -> CaseState:
    """A reasonably-populated CaseState so any single agent has something to act
    on when run in isolation. Override any field via keyword."""
    state = CaseState(raw_text=overrides.pop("raw_text", "I have chest pain and a fever."))
    state.normalised_symptoms = overrides.pop("normalised_symptoms", "chest pain and fever")
    state.intake_keywords = overrides.pop("intake_keywords", ["chest pain", "fever"])
    state.evidence = overrides.pop("evidence", ["chest pain"])
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _changed_fields(before: CaseState, after: CaseState) -> set[str]:
    return {f.name for f in fields(after) if getattr(before, f.name) != getattr(after, f.name)}


def run_and_check(agent, state: CaseState | None = None) -> tuple[dict, CaseState]:
    """Run `agent` on `state`, enforcing its CONTRACT. Returns (result, state).

    Works for both async workers (intake, classifier) and sync ones.
    """
    state = state if state is not None else make_case()
    contract = getattr(agent, "CONTRACT", None)
    assert contract is not None, f"{type(agent).__name__} must declare a CONTRACT (see base.AgentContract)"

    before = copy.deepcopy(state)
    result = agent.run(state)
    if inspect.isawaitable(result):
        result = asyncio.run(result)

    # 1) Boundary: the agent may only mutate fields it declared in `writes`.
    illegal = _changed_fields(before, state) - set(contract.writes)
    assert not illegal, (
        f"{type(agent).__name__} wrote CaseState fields outside its lane: {sorted(illegal)}. "
        f"Declared writes = {sorted(contract.writes)}. Either you touched another agent's "
        f"field (fix your agent) or you genuinely need it (update this agent's CONTRACT)."
    )

    # 2) Output shape: the result dict must contain every declared `returns` key.
    missing = set(contract.returns) - set(result or {})
    assert not missing, (
        f"{type(agent).__name__}.run() result is missing declared keys {sorted(missing)}. "
        f"Declared returns = {sorted(contract.returns)}."
    )
    return result, state


def emit_and_check(agent, state: CaseState | None = None) -> AgentMessage:
    """[A2A] Run the agent, then check its outgoing message honours its COMMS
    interface: it is an AgentMessage, sent by this agent, carrying only an intent
    the agent DECLARED in COMMS.publishes. Returns the message."""
    state = state if state is not None else make_case()
    run_and_check(agent, state)  # first produce a valid post-run state
    message = agent.emit(state)
    assert isinstance(message, AgentMessage)
    assert message.sender == agent.SLUG, (
        f"{type(agent).__name__} emitted a message with sender={message.sender!r}, "
        f"expected its own SLUG {agent.SLUG!r}"
    )
    assert message.intent in agent.COMMS.publishes, (
        f"{type(agent).__name__} emitted undeclared intent {message.intent!r}; "
        f"declared publishes = {sorted(agent.COMMS.publishes)}"
    )
    assert isinstance(message.payload, dict)
    return message
