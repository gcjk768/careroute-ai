"""Handoff harness — drive YOUR agent on realistic upstream data. Owner: Sham Goh.

FOR JAMES, AARON, MARCUS AND HERIZ. Import from here; you do not need to run the
whole pipeline, and you do not need the Supervisor.

    from tests.agents.intake_handoff import stage_case, drive

    setup = stage_case("routing")          # the case as it ARRIVES at routing
    result, state = drive("routing")       # ...or just run your agent on it

This is a plain module, not a test file, so importing it collects nothing and
runs nothing. The assertions that keep it honest live next door in
`test_intake_handoff.py`.

WHY THIS EXISTS
---------------
`case_from_intake()` gives you the state as *I* leave it. That is the whole
story if you are the classifier, because you sit directly downstream of me. It
is NOT the whole story for safety, routing, hitl or reflection: those agents
receive a case that has already been through the stages in between, and
`case_from_intake()` would hand them `acuity_code="P3_URGENT"`,
`confidence=0.5`, `care_tier="GP"` — the dataclass defaults, not a classified
case. A test built on that is testing your agent against fiction.

`stage_case(slug)` fixes that. It replays the real pipeline from intake up to —
but NOT including — your agent, and hands you both halves of what the
Supervisor would give you:

  * `state`  the CaseState with every upstream lane genuinely populated
  * `inbox`  the A2A messages your COMMS.subscribes actually entitles you to

The inbox half matters as much as the state. `CaseState` holds only the LATEST
value of a field; the bus holds what each agent CLAIMED when it acted. If your
agent reads `received_payload(...)`, that path is untested without an inbox, and
`stage_case` is the only way to get one outside a full pipeline run.

SCOPE — THIS IS STILL AN INTAKE FILE
------------------------------------
It builds test data by RUNNING the upstream agents; it never asserts anything
about what they decided. Whether the classifier picked the right acuity is E4
(James); whether routing picked a good clinic is E3 (Marcus). Nothing here has
an opinion on either. It answers one question, which is mine: is the handoff out
of intake usable by the people downstream of it?

If an upstream agent is still a template on your branch, its stage is skipped
and `setup.skipped` names it, so this keeps working while the team is mid-build.

THE LLM IS OFF
--------------
Every builder here forces the deterministic path, so results are reproducible
and nothing touches the network. That also means you are testing the shape
downstream agents see when the ASI10 kill switch is engaged — which is the shape
most likely to break you. See `zero-keyword-untranslated` below.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
from contextlib import contextmanager
from dataclasses import dataclass, field, fields

from app import llm
from app.agents.capability import ORCHESTRATOR_SLUG
from app.agents import (
    AgentMessage,
    CareRoutingAgent,
    CaseState,
    HumanInTheLoopAgent,
    MessageBus,
    ReflectionAgent,
    SafetyOverrideAgent,
    SeverityClassifierAgent,
    SymptomIntakeAgent,
)

__all__ = [
    "MOCK_INTAKE_OUTPUTS",
    "DOWNSTREAM_AGENTS",
    "PIPELINE",
    "StageSetup",
    "case_from_intake",
    "case_from_real_intake",
    "message_from_intake",
    "stage_case",
    "drive",
    "agent_for",
    "no_llm",
]


#: Realistic intake outputs to hand downstream. Deliberately spans the cases the
#: E1 gold set showed behave differently — an English emergency the deterministic
#: path reads fully, a translated one where `normalised_symptoms` is English but
#: `raw_text` is not, a mild case, and a zero-keyword case (the shape produced
#: when the LLM is unavailable on non-English input, which is what downstream
#: agents actually see when the kill switch is engaged).
MOCK_INTAKE_OUTPUTS: dict[str, dict] = {
    "english-emergency": {
        "raw_text": "I have crushing chest pain and I cannot breathe properly.",
        "normalised_symptoms": "I have crushing chest pain and I cannot breathe properly.",
        "detected_language": "en",
        "intake_keywords": ["chest pain", "breathlessness"],
    },
    "translated-emergency": {
        # What the LLM path produces: English normalised text, non-English raw.
        "raw_text": "எனக்கு நெஞ்சு வலி, மூச்சு விட முடியவில்லை",
        "normalised_symptoms": "I have chest pain and I cannot breathe.",
        "detected_language": "ta",
        "intake_keywords": ["chest pain", "breathlessness"],
    },
    "mild-case": {
        "raw_text": "I have had a bad cough and a fever for two days.",
        "normalised_symptoms": "I have had a bad cough and a fever for two days.",
        "detected_language": "en",
        "intake_keywords": ["fever", "cough"],
    },
    "zero-keyword-untranslated": {
        # The honest worst case: LLM down, non-English in. Downstream gets a
        # sentence it cannot read and an EMPTY keyword list. Every downstream
        # agent must cope with this rather than assume keywords exist.
        "raw_text": "Saya sakit dada dan susah bernafas",
        "normalised_symptoms": "Saya sakit dada dan susah bernafas.",
        "detected_language": "ms",
        "intake_keywords": [],
    },
}

#: Downstream consumers, in pipeline order. Reflection is included because it
#: reads the assembled decision and can be driven standalone too.
DOWNSTREAM_AGENTS: dict[str, type] = {
    "classifier": SeverityClassifierAgent,
    "safety": SafetyOverrideAgent,
    "routing": CareRoutingAgent,
    "hitl": HumanInTheLoopAgent,
    "reflection": ReflectionAgent,
}

#: The safety-gated order the Supervisor runs. `stage_case(slug)` replays every
#: stage BEFORE `slug` and stops.
PIPELINE: tuple[str, ...] = ("intake", "classifier", "safety", "routing", "hitl", "reflection")

_ALL_AGENTS: dict[str, type] = {"intake": SymptomIntakeAgent, **DOWNSTREAM_AGENTS}

#: Sender of the two orchestrator-issued messages (`case.opened` and the Phase 2
#: `safety.assessment.requested`). They are part of the conversation but come
#: from no worker's `emit()`, so they are replayed here rather than emitted.
#:
#: Taken from `capability.ORCHESTRATOR_SLUG` rather than hard-coded: the role
#: moved from the platform-owned Supervisor onto Symptom-Intake, and a literal
#: "supervisor" here would have silently staged messages from an agent that no
#: longer exists — every downstream test would still pass, against a
#: conversation the real pipeline never produces.
_SUPERVISOR = ORCHESTRATOR_SLUG


def agent_for(slug: str):
    """A fresh instance of the worker owning `slug`."""
    try:
        return _ALL_AGENTS[slug]()
    except KeyError:
        raise KeyError(f"unknown agent {slug!r}; known: {sorted(_ALL_AGENTS)}") from None


@contextmanager
def no_llm():
    """Force every agent down its deterministic path for the duration.

    The root `conftest.py` already does this for the whole test suite. This is
    for when you import these helpers OUTSIDE pytest (a script, a notebook, the
    REPL), where that conftest never runs and a live provider would make your
    results non-reproducible."""
    original = llm.complete

    async def _fail(*_a, **_kw):
        raise llm.LLMUnavailableError("LLM disabled by the intake handoff harness")

    llm.complete = _fail
    try:
        yield
    finally:
        llm.complete = original


def _run(agent, state: CaseState):
    """Run a worker whether it is sync or async. Returns its result dict."""
    result = agent.run(state)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _changed(before: CaseState, after: CaseState) -> list[str]:
    """CaseState fields that differ between two snapshots, sorted.

    Used to record what each replayed stage genuinely wrote. `messages` is
    excluded because it is Supervisor bookkeeping, not an agent's lane."""
    return sorted(
        f.name for f in fields(after)
        if f.name != "messages" and getattr(before, f.name) != getattr(after, f.name)
    )


# ---------------------------------------------------------------------------
# What intake leaves behind.
# ---------------------------------------------------------------------------
def case_from_intake(scenario: str, **overrides) -> CaseState:
    """Build the CaseState a downstream agent would receive FROM INTAKE.

    Only the three intake fields are populated; everything else is at its
    dataclass default. That is correct if you are the classifier and misleading
    if you are not — use `stage_case()` instead."""
    try:
        mock = dict(MOCK_INTAKE_OUTPUTS[scenario])
    except KeyError:
        raise KeyError(
            f"unknown scenario {scenario!r}; available: {sorted(MOCK_INTAKE_OUTPUTS)}"
        ) from None
    state = CaseState(raw_text=mock.pop("raw_text"))
    for field_name, value in mock.items():
        setattr(state, field_name, value)
    for field_name, value in overrides.items():
        setattr(state, field_name, value)
    return state


def case_from_real_intake(raw_text: str, **kwargs) -> CaseState:
    """Same thing, but by actually RUNNING intake rather than using a mock.

    Use this when you want to be sure the mock has not drifted from what the
    agent really emits. The LLM is disabled here so it stays reproducible."""
    with no_llm():
        state = CaseState(raw_text=raw_text, **kwargs)
        asyncio.run(SymptomIntakeAgent().run(state))
        return state


def message_from_intake(scenario: str = "english-emergency", **overrides) -> AgentMessage:
    """The `symptoms.normalised` message intake sends to the classifier.

    James: this is the payload half of our handoff. `emit()` is pure plumbing
    over the fields `run()` set, so it is faithful even on mock state."""
    return SymptomIntakeAgent().emit(case_from_intake(scenario, **overrides))


# ---------------------------------------------------------------------------
# What the WHOLE upstream chain leaves behind — the part Aaron, Marcus and
# Heriz need.
# ---------------------------------------------------------------------------
@dataclass
class StageSetup:
    """Everything the Supervisor would hand one worker before calling `run()`."""

    slug: str
    #: CaseState with every upstream lane populated for real.
    state: CaseState
    #: The messages this agent's COMMS.subscribes entitles it to receive.
    inbox: list[AgentMessage]
    #: The whole conversation so far, for when you want more than your inbox.
    bus: MessageBus
    #: Upstream agents that were still templates and got skipped.
    skipped: list[str] = field(default_factory=list)
    #: Which CaseState fields each upstream stage actually changed, in order.
    #: Proves the staged case was really processed rather than left at defaults,
    #: and is the quickest way to see what you are inheriting from whom.
    writes_by_stage: dict[str, list[str]] = field(default_factory=dict)

    def deliver(self, agent) -> None:
        """Hand `agent` its inbox, exactly as the Supervisor does before run()."""
        agent.consume(self.inbox)

    def intents(self) -> list[str]:
        """Intents in the inbox, in order — handy in an assertion message."""
        return [m.intent for m in self.inbox]


def _publish_supervisor_message(bus: MessageBus, state: CaseState, intent: str) -> AgentMessage:
    """Replay one of the Supervisor's own messages onto the bus.

    Built literally rather than by importing the Supervisor, because
    `stage_case` must keep working on a branch where Phase 2 does not exist yet.
    Payload shapes mirror `Supervisor._open_message` / `._safety_request`, and
    `test_intake_handoff.py` asserts they have not drifted apart."""
    if intent == "case.opened":
        return bus.publish(AgentMessage(
            sender=_SUPERVISOR, recipient="intake", intent=intent,
            payload={"language": state.language, "is_voice": state.is_voice},
        ))
    if intent == "safety.assessment.requested":
        classified = bus.messages_for_intent("acuity.classified")
        return bus.publish(AgentMessage(
            sender=_SUPERVISOR, recipient="safety", intent=intent,
            payload={
                "classifierAcuity": state.acuity_code,
                "classifierConfidence": round(float(state.confidence), 4),
                "classifierMessageSeq": classified[-1].seq if classified else -1,
                "fastPathDetected": bool(state.safety_fast_path),
            },
        ))
    raise ValueError(f"no Supervisor message replay defined for intent {intent!r}")


def stage_case(slug: str, scenario: str = "english-emergency", *,
               real_intake: bool = False, **overrides) -> StageSetup:
    """Build the case as it ARRIVES at `slug`, plus that agent's inbox.

    Replays the pipeline from intake up to — but not including — `slug`, with
    the LLM disabled throughout.

    Args:
        slug: the agent you are testing (`"safety"`, `"routing"`, `"hitl"`, ...).
        scenario: which `MOCK_INTAKE_OUTPUTS` case to start from.
        real_intake: run my agent for real instead of using the mock. Slower,
            but proves the mock has not drifted.
        **overrides: set any CaseState field after intake — e.g.
            `stage_case("routing", age_band="65+")`.

    Returns:
        A `StageSetup`. Run your agent with `setup.deliver(agent)` then
        `agent.run(setup.state)`, or let `drive()` do both.
    """
    if slug not in PIPELINE:
        raise KeyError(f"unknown agent {slug!r}; pipeline order is {list(PIPELINE)}")

    upstream = PIPELINE[:PIPELINE.index(slug)]
    bus = MessageBus()
    skipped: list[str] = []
    writes_by_stage: dict[str, list[str]] = {}

    with no_llm():
        blank = CaseState(raw_text=MOCK_INTAKE_OUTPUTS[scenario]["raw_text"])
        if real_intake and "intake" in upstream:
            state = CaseState(raw_text=MOCK_INTAKE_OUTPUTS[scenario]["raw_text"])
            _run(SymptomIntakeAgent(), state)
        else:
            state = case_from_intake(scenario)
        writes_by_stage["intake"] = _changed(blank, state)

        # The Supervisor opens the conversation before anyone runs.
        _publish_supervisor_message(bus, state, "case.opened")

        # Walk up to AND INCLUDING the target: the target's turn is where its own
        # inbound Supervisor message gets published. Only the upstream stages are
        # actually run — the target is left for the caller to drive.
        for stage in (*upstream, slug):
            if stage == "safety":
                # [Phase 2] Safety is asked, not merely told: the Supervisor's
                # directed request must be on the bus before Safety acts. This
                # has to happen even when safety IS the target, or Aaron's
                # correlation check has nothing to correlate against.
                _publish_supervisor_message(bus, state, "safety.assessment.requested")
            if stage == slug:
                break
            agent = agent_for(stage)
            try:
                if stage != "intake":  # intake already ran (or was mocked) above
                    agent.consume(bus.inbox(agent))
                    before = copy.deepcopy(state)
                    _run(agent, state)
                    writes_by_stage[stage] = _changed(before, state)
                bus.publish(agent.emit(state))
            except NotImplementedError:
                # That owner has not implemented run()/emit() on this branch.
                # Skip the stage rather than fail — the harness stays usable
                # while the team is mid-build.
                skipped.append(stage)

        for field_name, value in overrides.items():
            setattr(state, field_name, value)

        mine = agent_for(slug)
        return StageSetup(
            slug=slug, state=state, inbox=bus.inbox(mine), bus=bus,
            skipped=skipped, writes_by_stage=writes_by_stage,
        )


def drive(slug: str, scenario: str = "english-emergency", **kwargs) -> tuple[dict, CaseState]:
    """One call: build the upstream case, deliver the inbox, run YOUR agent.

        result, state = drive("hitl", "english-emergency")

    Returns `(result, state)` — the agent's result dict and the CaseState it
    mutated. The LLM is disabled, so this is reproducible."""
    setup = stage_case(slug, scenario, **kwargs)
    agent = agent_for(slug)
    setup.deliver(agent)
    with no_llm():
        return _run(agent, setup.state), setup.state
