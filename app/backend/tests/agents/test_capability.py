"""[Agentic][Responsible-AI] Capability declarations — Points 1 and 3, enforced.

Proposal review (Junhua, 2026-07-24) asked two things the code could not answer:
whether Safety-Override and HITL are genuinely agents, and which agent uses the
trained model. Both are now DECLARED in each worker's `CAPABILITY` and CHECKED
here, so the answers cannot drift away from the implementation between now and
submission.

What this file guarantees:
  * every worker declares a capability, and the declaration is consistent with
    its own implementation (a claim of reasoning needs a real model/LLM call),
  * exactly ONE worker consumes the trained model  -> answers Point 3,
  * every policy node carries an `upgrade_path`     -> the template obligation,
  * the two workers the reviewer named are classified explicitly, so if anyone
    changes their classification they must come here and change the assertion
    deliberately rather than by accident.

Run with: pytest -m capability
"""
from __future__ import annotations

import pytest
from harness import AGENT_CLASSES

from app.agents import Supervisor
from app.agents.capability import (
    AGENT,
    ORCHESTRATOR,
    ORCHESTRATOR_SLUG,
    POLICY_NODE,
    AgentCapability,
    CapabilityError,
    classify,
    enforce_capability,
)

pytestmark = pytest.mark.capability

# Every worker. There is no separate Supervisor entry any more: orchestration
# moved into Symptom-Intake, so `Supervisor` is an alias for
# `SymptomIntakeAgent` and adding it would count the same class twice — which is
# exactly how `test_exactly_one_agent_is_the_orchestrator` first failed.
ALL_AGENTS: dict[str, type] = dict(AGENT_CLASSES)
assert Supervisor is ALL_AGENTS["intake"], (
    "supervisor.Supervisor should be the deprecated alias for SymptomIntakeAgent; "
    "if that is no longer true, the orchestrator role moved again and this file needs updating."
)


@pytest.mark.parametrize("slug", sorted(ALL_AGENTS))
def test_every_agent_declares_a_valid_capability(slug):
    """The declaration must be present, complete, and consistent with the code."""
    enforce_capability(ALL_AGENTS[slug]())


@pytest.mark.parametrize("slug", sorted(ALL_AGENTS))
def test_declared_classification_matches_derived(slug):
    """You cannot call yourself an agent without the implementation to back it:
    agency requires BOTH an inference step AND more than one available outcome."""
    capability = ALL_AGENTS[slug]().CAPABILITY
    assert capability.classification == classify(capability), (
        f"{slug} declares {capability.classification!r} but its capabilities derive "
        f"{classify(capability)!r}"
    )


def test_exactly_one_agent_uses_the_project_trained_acuity_model():
    """The project-trained RandomForest acuity model has one consumer.

    Safety may use separately versioned pretrained NLP/LLM artifacts; those are
    declared by its tools and per-signal provenance rather than this flag.
    """
    users = [slug for slug, cls in ALL_AGENTS.items() if cls().CAPABILITY.uses_trained_model]
    assert users == ["classifier"], (
        f"expected the trained RandomForestClassifier to be consumed by exactly one agent "
        f"('classifier'), found {users}. If this changed, the MLOps pipeline's scope changed "
        f"with it — update MODEL_CARD.md and the proposal."
    )


def test_reviewer_named_agents_are_classified_explicitly():
    """[Point 1] The two workers the reviewer asked about, pinned.

    Safety-Override became an AGENT by gaining a bounded, ADDITIVE semantic layer
    (see safety.SemanticRedFlagLayer) — never by making its rule table
    overridable. HITL remains a POLICY_NODE today, honestly declared, with its
    route to agency recorded in its own `upgrade_path`.
    """
    safety = ALL_AGENTS["safety"]().CAPABILITY
    assert safety.classification == AGENT
    assert "llm.complete" in safety.tools

    hitl = ALL_AGENTS["hitl"]().CAPABILITY
    assert hitl.classification == POLICY_NODE
    assert hitl.upgrade_path.strip(), "HITL must carry the route to genuine agency"


def test_exactly_one_agent_is_the_orchestrator():
    """The role is singular. Two orchestrators means two conductors, and the
    safety-gated step order stops being guaranteed.

    Pinned against `capability.ORCHESTRATOR_SLUG` rather than a literal, because
    the role MOVED — it was the platform-owned Supervisor, it is now
    Symptom-Intake, and the invariant being protected ("exactly one") is the part
    that must survive that."""
    orchestrators = [s for s, c in ALL_AGENTS.items() if c().CAPABILITY.classification == ORCHESTRATOR]
    assert orchestrators == [ORCHESTRATOR_SLUG], (
        f"expected exactly one orchestrator ({ORCHESTRATOR_SLUG!r}), found {orchestrators}"
    )


@pytest.mark.parametrize("slug", sorted(ALL_AGENTS))
def test_policy_nodes_carry_an_upgrade_path(slug):
    """A policy node is allowed — an UNDOCUMENTED one is not. This is what makes
    the capability file a template the owners can act on rather than a label."""
    capability = ALL_AGENTS[slug]().CAPABILITY
    if capability.classification == POLICY_NODE:
        assert len(capability.upgrade_path.strip()) > 80, (
            f"{slug} is a POLICY_NODE; its upgrade_path must actually describe the reasoning step "
            f"and the alternatives it would choose between, not just gesture at one."
        )


@pytest.mark.parametrize("slug", sorted(ALL_AGENTS))
def test_declared_tools_are_within_the_allowlist(slug):
    """[E6 tool-selection] Tools USED must be a subset of tools PERMITTED."""
    agent = ALL_AGENTS[slug]()
    assert set(agent.CAPABILITY.tools) <= set(agent.TOOL_ALLOWLIST)


# --------------------------------------------------------------------------
# Negative tests: the enforcement itself must actually reject bad declarations,
# otherwise every test above passes vacuously.
# --------------------------------------------------------------------------
class _Fake:
    SLUG = "fake"
    TOOL_ALLOWLIST = ["llm.complete"]
    PROMPT_PATTERN = "None -- deterministic."


def _fake(**overrides):
    base = dict(
        reasoning="",
        action_space=("only one",),
        memory="",
        tools=(),
        uses_trained_model=False,
        classification=POLICY_NODE,
        justification="because",
        upgrade_path="x" * 100,
    )
    base.update(overrides)
    agent = _Fake()
    agent.CAPABILITY = AgentCapability(**base)
    return agent


def test_rejects_agency_claimed_without_an_inference_step():
    with pytest.raises(CapabilityError, match="derive"):
        enforce_capability(_fake(classification=AGENT, action_space=("a", "b"), upgrade_path=""))


def test_rejects_reasoning_claimed_without_a_prompt():
    with pytest.raises(CapabilityError, match="Reasoning must be implemented"):
        enforce_capability(
            _fake(reasoning="I think about things", action_space=("a", "b"),
                  classification=AGENT, upgrade_path="")
        )


def test_rejects_policy_node_with_no_upgrade_path():
    with pytest.raises(CapabilityError, match="upgrade_path"):
        enforce_capability(_fake(upgrade_path=""))


def test_rejects_tool_use_outside_the_allowlist():
    with pytest.raises(CapabilityError, match="TOOL_ALLOWLIST"):
        enforce_capability(_fake(tools=("clinic.lookup",)))


def test_rejects_missing_capability():
    with pytest.raises(CapabilityError, match="does not declare a CAPABILITY"):
        enforce_capability(_Fake())
