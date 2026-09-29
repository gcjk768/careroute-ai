"""Per-case plan for the post-Safety half of the pipeline.

[Agentic] Plan-and-Execute / orchestrator-workers (ArchAAS). The lecture gap
this closes: "the plan is fixed in code; subtasks are not chosen dynamically".
The orchestrator now asks `plan_case()` which steps THIS case needs, then
executes them — but only after `validate()` has checked the plan against an
explicit allowed-transition graph. Anything the graph rejects, and any planner
exception, falls back to `FULL_PLAN`, i.e. exactly the fixed sequence the
pipeline ran before this module existed (fail-safe, never fail-open).

WHAT IS NOT PLANNABLE, AND WHY
------------------------------
* Intake, Severity-Classifier and Safety-Override run BEFORE the plan is made.
  The plan is built from their output, so Safety can never be planned away —
  it has already run, and its verdict is one of the planner's inputs.
* Reflection is mandatory in every valid plan (`validate` rejects a plan
  without it). It is the monotone-escalation backstop; skipping it is exactly
  the kind of shortcut a planner must not be able to take.
* Clinician-Handoff is mandatory too, and still runs only when the case is
  escalated — the plan orders it, it does not decide it.

So the planner's discretion is deliberately narrow: WHICH routing step
(full Care-Routing vs 995 emergency guidance) and WHETHER the clarifying
question step goes before routing. Both choices can only make the pipeline
faster to reach the patient; neither can lower acuity or drop a review.

Pure and deterministic: no LLM, no I/O. CI runs with the LLM off, and a
safety-critical plan should not depend on a model's mood. An LLM planner could
propose plans later; `validate` is the gate it would have to pass.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..models import AcuityCode
from .base import CONFIDENCE_THRESHOLD, CaseState

#: Step vocabulary. Each maps to one executor in PipelineOrchestrator.
ROUTING = "routing"
EMERGENCY = "emergency_guidance"
HITL = "hitl"
REFLECTION = "reflection"
HANDOFF = "handoff"

START, END = "start", "end"

#: The allowed-transition graph. A plan is a path START -> ... -> END through it.
#: `hitl -> routing` exists only for the clarify-first shape; `emergency_guidance`
#: can only lead to HITL (a P1 still gets a review decision).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    START: frozenset({ROUTING, EMERGENCY, HITL}),
    ROUTING: frozenset({HITL, REFLECTION}),
    EMERGENCY: frozenset({HITL}),
    HITL: frozenset({ROUTING, REFLECTION}),
    REFLECTION: frozenset({HANDOFF}),
    HANDOFF: frozenset({END}),
}

#: Steps every plan must contain, whatever its shape.
MANDATORY = frozenset({HITL, REFLECTION, HANDOFF})


@dataclass(frozen=True)
class PlanStep:
    step: str
    rationale: str


@dataclass(frozen=True)
class Plan:
    shape: str
    steps: tuple[PlanStep, ...]
    #: Skip the UI pacing delays: a P1/red-flag patient should see "call 995"
    #: as fast as the pipeline can produce it, not after an animation.
    urgent: bool = False
    fallback_reason: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.step for s in self.steps)

    def to_dict(self) -> dict:
        return {**asdict(self), "steps": [asdict(s) for s in self.steps]}


_REFLECT = PlanStep(REFLECTION, "Critic always verifies the assembled decision (escalation-only).")
_HANDOFF = PlanStep(HANDOFF, "Clinician packet, built only if the case ends up escalated.")

#: Today's fixed sequence — the fail-safe every rejected plan falls back to.
FULL_PLAN = Plan("full", (
    PlanStep(ROUTING, "Normal case: find the right care tier and clinic."),
    PlanStep(HITL, "Decide whether a clinician must review."),
    _REFLECT, _HANDOFF,
))


def plan_case(state: CaseState) -> Plan:
    """Choose this case's steps from the post-Safety state. First match wins,
    most urgent first, so a red flag can never be out-ranked by a vague text."""
    if state.acuity_code == AcuityCode.P1.value:
        return Plan("emergency", (
            PlanStep(EMERGENCY, "P1: skip the clinic search — call 995 for SCDF dispatch."),
            PlanStep(HITL, "Review decision; Reflection escalates any P1 left unescalated."),
            _REFLECT, _HANDOFF,
        ), urgent=True)
    if state.safety_triggered:
        return Plan("red_flag", (
            PlanStep(ROUTING, f"Red flag at {state.acuity_code}: route to the nearest public ED."),
            PlanStep(HITL, "Red flag is a review floor — a clinician must see it."),
            _REFLECT, _HANDOFF,
        ), urgent=True)
    if state.clarification is not None:
        # Reuses the classifier -> HITL handshake: the classifier already
        # PROPOSED the question; HITL still decides ask vs escalate. The plan
        # only moves that decision ahead of routing.
        return Plan("clarify", (
            PlanStep(HITL, "Too little information: ask the proposed clarifying question first."),
            PlanStep(ROUTING, "Still route, so the patient has a care tier while they answer."),
            _REFLECT, _HANDOFF,
        ))
    if state.confidence < CONFIDENCE_THRESHOLD:
        return Plan("low_confidence", (
            FULL_PLAN.steps[0],
            PlanStep(HITL, f"Confidence {state.confidence:.2f} is below threshold: expect review."),
            PlanStep(REFLECTION, "Critic may re-run the classifier (bounded by iteration cap + budget)."),
            _HANDOFF,
        ))
    return FULL_PLAN


def validate(plan: Plan, state: CaseState) -> list[str]:
    """Why `plan` must not run; empty means it may. Checked against the STATE,
    not against what the plan claims about the case."""
    issues: list[str] = []
    names = plan.names
    if len(set(names)) != len(names):
        issues.append(f"repeated step in {names}")
    missing = MANDATORY - set(names)
    if missing:
        issues.append(f"missing mandatory steps {sorted(missing)}")
    if (ROUTING in names) == (EMERGENCY in names):
        issues.append("exactly one of routing / emergency_guidance is required")
    if EMERGENCY in names and state.acuity_code != AcuityCode.P1.value:
        # The one step that skips Care-Routing is only ever allowed on a P1.
        issues.append(f"emergency_guidance not allowed at {state.acuity_code}")
    for a, b in zip((START, *names), (*names, END), strict=True):
        if b not in ALLOWED_TRANSITIONS.get(a, frozenset()):
            issues.append(f"transition {a} -> {b} not allowed")
    return issues


def make_plan(state: CaseState, planner=plan_case) -> Plan:
    """Plan, validate, and fall back to FULL_PLAN on any problem. Never raises."""
    try:
        plan = planner(state)
        issues = validate(plan, state)
    except Exception as exc:  # noqa: BLE001 - a planner fault must degrade to the fixed sequence, never abort a triage
        issues = [f"planner error: {type(exc).__name__}"]
    if issues:
        return Plan("fallback", FULL_PLAN.steps, fallback_reason="; ".join(issues))
    return plan
