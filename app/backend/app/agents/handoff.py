"""Clinician-Handoff worker — turns an escalation into a reviewable packet.

OWNER: Heriz Yusoff

STATUS: MERGED into the pipeline (2026-08-28). Agent logic owned by Heriz;
wiring done by James per `docs/vault/Clinician Handoff Pipeline Integration.md`.
------------------------------------------------------------------------
This agent was developed in isolation while five people worked in parallel, so
none of the shared platform files were touched from here. That wiring has now
landed, and the notes below record what changed so the docstring does not go
stale:

  - It IS now exported by `agents/__init__.py` and instantiated by the
    orchestrator. It runs as the LAST step of `PipelineOrchestrator.orchestrate()`
    (`agents/orchestration.py` — NOT `supervisor.py`, which since the
    orchestrator takeover is only a deprecated alias for `SymptomIntakeAgent`),
    gated on `state.escalated`, after the Reflection block. Ordering rationale
    is below and is load-bearing, not incidental.
  - `handoff_summary` / `handoff_citations` / `handoff_questions` are now
    DECLARED dataclass fields on `CaseState` in `agents/base.py`, so
    `CONTRACT.writes` below is mechanically enforced by the parametrized
    boundary test in `tests/agents/test_contracts.py` rather than merely
    documented. This agent is registered in `tests/agents/harness.py`.
  - Tests (`tests/agents/test_handoff.py`, `tests/test_eval_handoff.py`) still
    build a `CaseState` directly with hand-set fields — the same pattern
    `tests/agents/test_hitl.py` uses — so nothing here depends on
    intake/classifier/safety/routing/reflection actually having run.
  - Output-guardrail screening (`guardrail.screen_output`, the same LLM05 check
    applied to `rationale`) now runs in `main.py` over `handoff_summary` before
    it reaches a clinician, and the packet is persisted onto
    `CaseRecord`/`EscalationDetail`. Screening stays a `main.py` concern, as
    this file's original design intended — it is not done here.

WHY THIS AGENT EXISTS
----------------------
HumanInTheLoopAgent (hitl.py) decides WHETHER a case needs a clinician. Nothing
in the pipeline previously owned WHAT the clinician actually sees when it does.
`main.py` handed the clinician the same fields every case already carried
(rationale, evidence, citations) with no summarisation step aimed at a human
about to make a time-pressured decision. This worker closes that gap: it reads
the fully-assembled case (post safety-override, post routing, post the
Reflection critic's final pass) and produces a short, plain-language handoff
packet — summary, grounding citations, optional follow-up questions — for the
clinician queue.

It runs ONLY when `state.escalated` is True, and only AFTER Reflection, because
Reflection can itself force an escalation that nothing upstream (including
HITL) originally flagged (see `ReflectionAgent.run`, the P1/P2-not-escalated
check). Summarising before that check runs would risk describing a case as
"routine" moments before it is escalated.

WHY IT IS A GENUINE AGENT, NOT ANOTHER POLICY NODE
----------------------------------------------------
Unlike HITL / Care-Routing / (pre-upgrade) Safety-Override, this worker's
output is not fixed by its input: the same case can be summarised in many
different but equally valid ways, and it makes a real choice about what to
foreground (the safety rule, the low-confidence signal, the cross-visit
history) and whether a follow-up question is worth asking at all. See
`CAPABILITY` below for the reasoning/action-space pair that makes it an AGENT
under `agents/capability.classify()`.

WHY THE RETRIEVAL "DECISION" IS A PYTHON BRANCH, NOT A MODEL-DRIVEN TOOL CALL
------------------------------------------------------------------------------
The original design note called this "ReAct-style: decide what's relevant,
optionally retrieve grounding context via RAG, then generate." Honestly:
`app/llm.py` exposes a single-shot `complete()` (no function-calling loop), so
there is no channel for the model itself to request a tool call mid-generation
the way a true ReAct agent would. The retrieval DECISION is therefore made in
Python (`_should_ground`) from the same signals a triage nurse would use —
a safety-override rule fired, or the escalation was confidence-driven — and the
retrieved citations are then handed to the LLM as CONTEXT it must stay inside,
never as something it can request more of. This mirrors how classifier.py
already uses `rag.retrieve()` (a deterministic call around the model, not one
the model drives) rather than inventing a new pattern.

FAITHFULNESS IS THE SAFETY PROPERTY HERE
------------------------------------------
There is no acuity/escalation decision left to get wrong by the time this
runs — that risk is HITL's and Reflection's, not this agent's. The risk here is
different: a fluent summary that quietly invents a symptom, a vital sign, or a
history detail the patient never gave. So the prompt is bounded to "use ONLY
the facts listed below" the same way `SemanticRedFlagLayer` is bounded to a
closed category vocabulary, and the deterministic fallback template is built
ONLY from fields already on the `state` object passed in — it cannot invent
anything either. Wiring the existing output guardrail (`guardrail.screen_output`,
same LLM05 check `main.py` already applies to `rationale`) in front of the
summary before it reaches a clinician is an integration step for merge time,
not implemented in this file. `tests/test_eval_handoff.py` evaluates the
faithfulness property directly against a hand-built `CaseState` per fixture
row — registered as `E8_HANDOFF_FAITHFULNESS` in `app/evals/plan.py` (E7 was
already taken by Aaron's safety-context benchmark).

Edit this file and your tests in tests/agents/test_handoff.py. Your lane over
CaseState is `ClinicianHandoffAgent.CONTRACT` below.
"""

# NOTE for Heriz Yusoff (Clinician-Handoff owner) — added 2026-09-25 by James.
# This file is unchanged; what improved around it:
# - The handoff packet is only reachable through the staff portal, which now
#   requires a server-side login (see the note in hitl.py).
# - Every API response now carries X-Content-Type-Options: nosniff and
#   Cross-Origin-Resource-Policy: same-origin (OWASP ZAP API scan: 0 warnings).
from __future__ import annotations

import asyncio
import json
from typing import ClassVar

from .. import llm, rag
from .base import EMBEDDED_INSTRUCTION_GUARD, AgentContract, CaseState, enforce_tool_access
from .capability import AGENT, ORCHESTRATOR_SLUG, AgentCapability
from .messaging import AgentComms, AgentMessage, ConsumesMessages, enforce_comms

OWNER = "Heriz Yusoff"

#: Bounds on the generated packet, enforced regardless of path (LLM or
#: fallback) so a verbose model response can't blow up the clinician queue UI.
_MAX_FOLLOW_UP_QUESTIONS = 2
_MAX_QUESTION_CHARS = 300
_MAX_SUMMARY_CHARS = 600


# NOTE for Heriz (Clinician-Handoff owner) — changed 2026-09-16 by James, courseware audit.
# WHY: OWASP LLM01 mitigation #1 needs the SAME embedded-instruction guard in every
# LLM prompt, asserted by tests/agents/test_prompt_hygiene.py. Only the guard
# sentence (agents.base.EMBEDDED_INSTRUCTION_GUARD) and the SYSTEM_PROMPT alias were
# added; nothing else in this prompt or agent changed. Reword freely as long as the
# shared sentence stays in SYSTEM_PROMPT.
# [AI-Security] LLM01: constrains the model and carries the shared embedded-
# instruction guard. The handoff prompt is the one path that feeds RETRIEVED
# text (citations) and REMEMBERED text (prior visit) to the model, so the
# indirect-injection half of the guard matters most here.
SYSTEM_PROMPT = (
    "You are writing a handoff note for a clinician who is about to review an AI-triaged "
    "case. Use ONLY the facts listed in the case data below. Do not invent symptoms, "
    "vital signs, history, or examination findings that are not present in the case data. "
    "If the case data is thin, say so plainly rather than filling the gap. Never give a "
    "diagnosis or a treatment instruction -- your job is to orient the clinician to what "
    "the patient reported and why the system escalated, not to practise medicine. "
    + EMBEDDED_INSTRUCTION_GUARD
)


class ClinicianHandoffAgent(ConsumesMessages):
    """Builds the structured packet a clinician sees when a case is escalated."""

    SLUG = "handoff"

    # [AI-Security][Agentic] FR-12 least-privilege allow-list. This worker only
    # ever needs to read grounding material and call the LLM to write the
    # summary — it has no business touching the clinic directory, the model,
    # or the escalation-creation tool (that stays HITL's alone).
    TOOL_ALLOWLIST: ClassVar[list[str]] = ["llm.complete", "rag.retrieve"]
    AUTONOMY_LEVEL: int = 2  # L2: bounded reasoning, deterministic fallback
    PROMPT_PATTERN: str = (
        "Structured-JSON LLM primary, grounded in case-log facts + optionally-retrieved RAG "
        "citations (retrieval decision is a deterministic branch on the escalation signal, not "
        "model-driven); deterministic template fallback built only from CaseState fields."
    )

    # [Point 1] Machine-checked capability declaration — see agents/capability.py.
    CAPABILITY = AgentCapability(
        reasoning=(
            "Synthesises the fully-assembled case (symptoms, acuity/evidence, safety flags, "
            "routing, escalation reason, prior-visit context) into a plain-language handoff "
            "summary for the reviewing clinician, and judges whether a follow-up question is "
            "worth asking. Not a lookup: the same case state can be faithfully summarised in many "
            "different ways, and the worthwhile output is chosen, not computed from a table."
        ),
        action_space=(
            "retrieve grounding citations when a safety rule fired, and cite them",
            ("skip retrieval when the escalation is confidence- or cross-visit-driven (nothing in "
            "the rule table to ground)"),
            ("suggest up to two follow-up questions when the case log leaves a clinically relevant "
            "gap"),
            "suggest no follow-up questions when the picture is already complete",
        ),
        memory=(
            "None held by the agent itself. Reads whatever prior-visit context the Supervisor "
            "already recalled onto CaseState (prior_visit_summary) — that is input, not memory "
            "this agent keeps."
        ),
        tools=("llm.complete", "rag.retrieve"),
        uses_trained_model=False,
        classification=AGENT,
        justification=(
            "An agent by both tests in agents/capability.py: it performs a real inference step "
            "(bounded generative summarisation, not extraction against a fixed schema of "
            "outcomes) and it chooses between genuine alternatives (ground or don't; ask or "
            "don't). It never revises the acuity, safety, routing or escalation decision — those "
            "remain the upstream workers' authoritative lanes; this worker only decides how the "
            "ALREADY-FINAL decision is presented to the human who must act on it."
        ),
    )

    # [Platform] Isolation lane — see AgentContract in base.py. This worker may
    # NOT touch acuity/escalated/escalation_reason/etc — it observes the
    # pipeline's conclusion the same way HITL does, and only ever writes its
    # own three handoff fields. NOTE: these three are not (yet) declared on
    # `CaseState` in base.py — see the module docstring's STATUS section. This
    # CONTRACT documents the intended lane; it is not mechanically checked
    # against a live pipeline until this agent is registered and merged.
    CONTRACT = AgentContract(
        writes=frozenset({"handoff_summary", "handoff_citations", "handoff_questions"}),
        returns=frozenset({"source", "summary", "citations", "follow_up_questions"}),
    )

    # [A2A] Runs last, after Reflection's final verdict. Consumes the
    # (broadcast) safety and routing announcements so it can foreground the
    # right context even when called standalone; publishes its packet back to
    # whichever agent holds the orchestrator role, for aggregation.
    COMMS = AgentComms(
        publishes=frozenset({"handoff.ready"}),
        subscribes=frozenset({"safety.override", "care.routed"}),
    )

    def emit(self, state: CaseState) -> AgentMessage:
        intent = "handoff.ready"
        enforce_comms(self, intent)
        return AgentMessage(
            # Addressed to whoever holds the orchestrator role, not a hard-coded
            # name. This was `"supervisor"` — an agent that no longer exists
            # since the takeover — so the packet was announced to nobody. Same
            # bug, same fix, as reflection.py's `decision.reviewed`.
            sender=self.SLUG, recipient=ORCHESTRATOR_SLUG, intent=intent,
            payload={
                "summary": state.handoff_summary,
                "citationCount": len(state.handoff_citations),
                "followUpQuestions": state.handoff_questions,
            },
        )

    # ------------------------------------------------------------------
    def _should_ground(self, state: CaseState) -> bool:
        """[Agentic] The one retrieval decision this agent makes itself (see
        module docstring for why this is a Python branch rather than a
        model-driven tool call). Ground only when there is an actual rule to
        ground — a fired safety override. A confidence-only or cross-visit-only
        escalation has nothing in `redflags.RED_FLAG_RULES` to cite."""
        return bool(state.safety_triggered and state.safety_rule)

    async def _gather_grounding(self, state: CaseState) -> list[dict]:
        """`rag.retrieve` is SYNCHRONOUS (urllib + a scikit-learn TF-IDF fit), so
        it goes on a thread: awaiting it inline would block the event loop — and
        therefore every other in-flight triage and the SSE heartbeat — for the
        whole retrieval. Same treatment Care-Routing gives its blocking work."""
        enforce_tool_access(self, "rag.retrieve")
        query = f"{state.safety_rule or ''} {state.safety_reason or ''} {state.normalised_symptoms}"
        try:
            return await asyncio.to_thread(rag.retrieve, query, top_k=2)
        except Exception:  # noqa: BLE001 - retrieval or LLM fault must still yield a reviewable handoff packet
            return []

    async def run(self, state: CaseState) -> dict:
        # This worker only has a job once a case is ACTUALLY escalated, and it
        # runs after Reflection so `state.escalated` reflects the FINAL verdict
        # (Reflection can force an escalation HITL never flagged). Called
        # standalone (unit tests, or a non-escalated case in the real
        # pipeline) it returns a cheap, explicit no-op rather than silently
        # summarising a case nobody will review.
        if not state.escalated:
            state.handoff_summary = ""
            state.handoff_citations = []
            state.handoff_questions = []
            return {"source": "not_escalated", "summary": "", "citations": [], "follow_up_questions": []}

        citations = await self._gather_grounding(state) if self._should_ground(state) else []

        system = SYSTEM_PROMPT

        try:
            # Inside the guard: a non-numeric confidence or a malformed field
            # off the wire used to raise out of the prompt build, past the
            # fallback that exists for exactly this.
            prompt = self._build_prompt(state, citations)
            enforce_tool_access(self, "llm.complete")
            raw = await llm.complete(system, prompt, json_mode=True, task="handoff.summary")
            data = json.loads(raw)
            summary = str(data.get("summary") or "").strip()
            if not summary:
                raise ValueError("empty summary")
            if len(summary) > _MAX_SUMMARY_CHARS:
                summary = summary[: _MAX_SUMMARY_CHARS - 3] + "..."
            questions = [
                str(q).strip()[:_MAX_QUESTION_CHARS]
                for q in (data.get("follow_up_questions") or []) if str(q).strip()
            ][:_MAX_FOLLOW_UP_QUESTIONS]

            state.handoff_summary = summary
            state.handoff_citations = citations
            state.handoff_questions = questions
            return {
                "source": "llm", "summary": summary, "citations": citations,
                "follow_up_questions": questions,
            }
        except Exception:  # noqa: BLE001 - retrieval or LLM fault must still yield a reviewable handoff packet
            return self._fallback(state, citations)

    def _build_prompt(self, state: CaseState, citations: list[dict]) -> str:
        citation_lines = "\n".join(
            f"- {c.get('title', '')}: {c.get('snippet', '')} ({c.get('source', '')})" for c in citations
        ) or "(none retrieved)"
        return (
            f"Presenting symptoms (normalised): \"{state.normalised_symptoms}\"\n"
            f"Original patient words: \"{state.raw_text}\"\n"
            f"Assessed acuity: {state.acuity_code} (confidence {state.confidence:.2f})\n"
            f"Supporting evidence phrases: {state.evidence}\n"
            f"Safety-override triggered: {state.safety_triggered}"
            + (f" (rule: {state.safety_rule}; reason: {state.safety_reason})" if state.safety_triggered else "")
            + f"\nCross-visit escalation: {state.cross_visit_escalation}"
            + (f" ({state.cross_visit_reason})" if state.cross_visit_reason else "")
            + f"\nPrior visit on record: {state.prior_visit_summary or 'none'}\n"
            f"Escalation reason (from HITL/Reflection): {state.escalation_reason}\n"
            f"Care tier / clinic: {state.care_tier} / {state.clinic}\n"
            f"Retrieved grounding citations:\n{citation_lines}\n\n"
            "Return JSON with keys: summary (string, 2-4 sentences, plain language, oriented to a "
            "clinician about to review this case), follow_up_questions (array of at most 2 short "
            "strings -- the single highest-value clarifying question(s) if the case data leaves a "
            "real gap, or an empty array if it does not)."
        )

    def _fallback(self, state: CaseState, citations: list[dict]) -> dict:
        """Deterministic template — built ONLY from fields already on CaseState,
        so it can invent nothing. Mirrors the rest of the pipeline's
        try-LLM-then-deterministic-fallback contract (see supervisor.py)."""
        parts = [f"Presenting complaint: {state.normalised_symptoms or state.raw_text}."]
        parts.append(
            f"Assessed acuity {state.acuity_code} (confidence {state.confidence:.2f}); "
            f"evidence: {', '.join(state.evidence) or 'none recorded'}."
        )
        if state.safety_triggered:
            parts.append(f"Safety-override rule '{state.safety_rule}' fired: {state.safety_reason}")
        if state.cross_visit_escalation:
            parts.append(state.cross_visit_reason or "Recurring presentation across visits.")
        if state.prior_visit_summary:
            parts.append(f"Prior visit on record: {state.prior_visit_summary}.")
        parts.append(f"Escalated because: {state.escalation_reason or 'see audit trail'}.")
        summary = " ".join(parts)
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[: _MAX_SUMMARY_CHARS - 3] + "..."

        state.handoff_summary = summary
        state.handoff_citations = citations
        state.handoff_questions = []  # deterministic path never invents a question
        return {
            "source": "fallback", "summary": summary, "citations": citations,
            "follow_up_questions": [],
        }
