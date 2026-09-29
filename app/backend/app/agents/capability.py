"""[Platform] Agent capability declarations — the machine-checked answer to the
reviewer's Points 1 and 3.

WHY THIS MODULE EXISTS
----------------------
Proposal review (Junhua, 2026-07-24) asked two questions the code could not
answer for itself:

  Point 1  "For Safety-Override and Human-in-the-Loop, clarify whether they are
            genuinely agents or whether they should be implemented as
            deterministic tools or workflow nodes. Please think through their
            required reasoning, autonomy, memory, and tool use."
  Point 3  "Clarify whether the project involves training a separate ML model...
            it is unclear which agent uses the trained model."

Prose in a proposal cannot be verified and drifts away from the code. So every
worker now DECLARES an `AgentCapability` naming exactly the four properties the
reviewer asked about — reasoning, autonomy (action space), memory, tool use —
plus `uses_trained_model` for Point 3. `tests/agents/test_capability.py` then
checks each declaration against the code, so a claim of agency that the
implementation does not support FAILS THE BUILD.

HOW TO USE THIS AS A TEMPLATE (for the 5 agent owners)
------------------------------------------------------
Your agent's `CAPABILITY` is the honest description of what it does TODAY. If it
classifies as a POLICY_NODE, that is not a bug and you do not have to hide it —
but you MUST fill in `upgrade_path` describing what would make it a genuine
agent. `enforce_capability()` rejects a POLICY_NODE with an empty upgrade path,
so the roadmap for every non-agent lives next to its code instead of in someone's
notes.

To turn your policy node into an agent, implement the `upgrade_path` using the
`ReasoningLayer` template in `agents/reasoning.py` (worked example:
`safety.SemanticRedFlagLayer`), then update `CAPABILITY` — `reasoning` becomes
non-empty and `action_space` gains real alternatives, so `classify()` starts
returning AGENT and the test suite will hold you to it.

THE DISTINCTION THIS MODULE ENCODES
-----------------------------------
An **agent** decides. It performs an inference step whose output is not fixed by
its input, and it chooses between genuine alternatives. A **policy node** is a
pure function of its input: same input, same output, forever. Both are legitimate
architecture — a red-flag interlock that could be reasoned around would not be an
interlock — but they must be labelled honestly, which is precisely what the
reviewer asked for.
"""
from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# Classifications. Deliberately only three: the reviewer's question was binary
# ("agent or deterministic node?") and ORCHESTRATOR exists because sequencing is
# neither — it routes between workers rather than deciding about the case. The
# agent holding that role also happens to be a worker (Symptom-Intake), which is
# exactly why the classification has to be declared rather than inferred.
# --------------------------------------------------------------------------
AGENT = "agent"
POLICY_NODE = "policy_node"
ORCHESTRATOR = "orchestrator"

VALID_CLASSIFICATIONS = frozenset({AGENT, POLICY_NODE, ORCHESTRATOR})

#: The SLUG of the one agent permitted to declare ORCHESTRATOR.
#:
#: Was "supervisor" (platform-owned). The team moved orchestration into the
#: Symptom-Intake agent so every worker is called through it, which is why this
#: is a named constant rather than a literal: the rule being enforced is "exactly
#: one agent orchestrates", and that survives the role moving. Changing this
#: value is how the role is handed over — deliberately a one-line, reviewable
#: edit rather than something a new declaration can quietly claim.
ORCHESTRATOR_SLUG = "intake"


class CapabilityError(RuntimeError):
    """Raised when an agent's declared CAPABILITY contradicts its implementation."""


@dataclass(frozen=True)
class AgentCapability:
    """One worker's honest self-description, in the reviewer's own four terms.

    reasoning
        The inference step this worker performs, in one line. EMPTY STRING means
        "none — this worker is a pure function of its input". If non-empty the
        worker MUST actually have an LLM/model call (checked against
        PROMPT_PATTERN and TOOL_ALLOWLIST).
    action_space
        The distinct outcomes this worker may CHOOSE BETWEEN. A single-element
        tuple means there is no choice to make — the hallmark of a policy node.
        This is "autonomy" made countable rather than asserted.
    memory
        What this worker remembers ACROSS cases. Empty string means none. Note
        that reading `CaseState` is NOT memory — that is just its input.
    tools
        The tools this worker actually invokes at runtime. Must be a subset of
        its `TOOL_ALLOWLIST` (the allow-list is the permission; this is the use).
    uses_trained_model
        [Point 3] True for the ONE worker that consumes the project-trained
        RandomForestClassifier in `app/ml/`. Pretrained foundation/NLP models
        are declared separately through each worker's tools and model
        provenance. Exactly one worker may set this, so "which agent uses the
        project-trained acuity model?" is answered by grep, not by prose.
    classification
        AGENT | POLICY_NODE | ORCHESTRATOR. Must equal `classify(self)` — you
        cannot declare yourself an agent without the implementation to back it.
    justification
        Why this classification is the RIGHT design here (not an apology for it).
    upgrade_path
        For a POLICY_NODE: what would make it a genuine agent. Required — an
        empty upgrade path on a policy node is a build failure. For an AGENT or
        ORCHESTRATOR: leave empty.
    """

    reasoning: str
    action_space: tuple[str, ...]
    memory: str
    tools: tuple[str, ...]
    uses_trained_model: bool
    classification: str
    justification: str
    upgrade_path: str = ""


def classify(capability: AgentCapability) -> str:
    """Derive the classification from the declared capabilities.

    The rule is deliberately strict and mechanical: an agent must BOTH perform an
    inference step AND have more than one outcome available to it. Reasoning with
    a single possible outcome is a computation, not a decision; a choice made by
    a lookup table is a branch, not agency. Only both together are agency.

    ORCHESTRATOR is never derived — it is declared, and only the agent named in
    `ORCHESTRATOR_SLUG` may declare it (enforced in `enforce_capability`).
    """
    if capability.classification == ORCHESTRATOR:
        return ORCHESTRATOR
    reasons = bool(capability.reasoning.strip())
    chooses = len(capability.action_space) > 1
    return AGENT if (reasons and chooses) else POLICY_NODE


def enforce_capability(agent: object) -> AgentCapability:
    """Validate `agent.CAPABILITY` against the agent's actual implementation.

    Called by `tests/agents/test_capability.py` for every worker, so the
    declarations cannot rot. Raises `CapabilityError` with a message aimed at the
    agent's OWNER (they are the one who has to fix it).
    """
    name = type(agent).__name__
    capability = getattr(agent, "CAPABILITY", None)
    if capability is None:
        raise CapabilityError(
            f"{name} does not declare a CAPABILITY. Every worker must — see "
            f"agents/capability.py for the template and safety.py for a worked example."
        )
    if not isinstance(capability, AgentCapability):
        raise CapabilityError(f"{name}.CAPABILITY must be an AgentCapability, got {type(capability).__name__}.")

    if capability.classification not in VALID_CLASSIFICATIONS:
        raise CapabilityError(
            f"{name} declares unknown classification {capability.classification!r}; "
            f"expected one of {sorted(VALID_CLASSIFICATIONS)}."
        )

    # Exactly ONE agent orchestrates. Anything else claiming it is a mistake.
    # The role moved from the platform-owned Supervisor to Symptom-Intake; the
    # constant is the single place that records which agent holds it, so the rule
    # stays "only one may" rather than becoming "whoever asks".
    if capability.classification == ORCHESTRATOR and getattr(agent, "SLUG", None) != ORCHESTRATOR_SLUG:
        raise CapabilityError(
            f"{name} declares ORCHESTRATOR but only {ORCHESTRATOR_SLUG!r} may. Two orchestrators "
            f"means two conductors, and the safety-gated step order stops being guaranteed."
        )

    derived = classify(capability)
    if capability.classification != derived:
        raise CapabilityError(
            f"{name} declares classification={capability.classification!r} but its declared "
            f"capabilities derive {derived!r} (reasoning={'yes' if capability.reasoning.strip() else 'no'}, "
            f"action_space={len(capability.action_space)} option(s)). An agent needs BOTH an "
            f"inference step AND more than one available outcome. Either implement the missing "
            f"half or correct the declaration."
        )

    # A claim of reasoning must be backed by an actual model/LLM call.
    prompt_pattern = str(getattr(agent, "PROMPT_PATTERN", "")).strip()
    declares_no_prompt = prompt_pattern.lower().startswith("none")
    if capability.reasoning.strip() and declares_no_prompt and not capability.uses_trained_model:
        raise CapabilityError(
            f"{name} declares reasoning={capability.reasoning!r} but its PROMPT_PATTERN says "
            f"{prompt_pattern!r} and it uses no trained model. Reasoning must be implemented, "
            f"not asserted — see agents/reasoning.py."
        )
    if not capability.reasoning.strip() and not declares_no_prompt:
        raise CapabilityError(
            f"{name} declares no reasoning but its PROMPT_PATTERN ({prompt_pattern!r}) describes "
            f"one. Update CAPABILITY.reasoning to describe the inference step it performs."
        )

    # Tools USED must be a subset of tools PERMITTED. The allow-list grants; this
    # records what is actually exercised. A tool used but not granted would be
    # caught at runtime by enforce_tool_access — this catches the declaration.
    allowlist = set(getattr(agent, "TOOL_ALLOWLIST", []))
    undeclared = set(capability.tools) - allowlist
    if undeclared:
        raise CapabilityError(
            f"{name} declares it uses tools {sorted(undeclared)} that are not in its "
            f"TOOL_ALLOWLIST {sorted(allowlist)}. Add them to the allow-list or remove the claim."
        )

    if not capability.justification.strip():
        raise CapabilityError(f"{name}.CAPABILITY.justification is empty — say why this design is right.")

    # THE TEMPLATE OBLIGATION: a policy node must carry its own upgrade path, so
    # the route to genuine agency lives beside the code rather than in a doc.
    if capability.classification == POLICY_NODE and not capability.upgrade_path.strip():
        raise CapabilityError(
            f"{name} is a POLICY_NODE and must declare an `upgrade_path` — one line naming what "
            f"would make it a genuine agent (the reasoning step and the alternatives it would "
            f"choose between). See agents/capability.py and the worked example in safety.py."
        )
    if capability.classification != POLICY_NODE and capability.upgrade_path.strip():
        raise CapabilityError(
            f"{name} is already {capability.classification} — leave `upgrade_path` empty."
        )

    return capability
