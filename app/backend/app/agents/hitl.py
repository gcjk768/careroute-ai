"""Human-in-the-Loop worker.

OWNER: Heriz Yusoff

Decides whether a case must be routed to a clinician — the worker that enforces
"AI assists, clinician decides". The escalation RECORD itself is created by the
platform (main.py -> store.py) only when THIS worker sets `escalated`, gated by
this agent's "escalation.create" allow-list.

STATUS
------
IMPLEMENTED. The escalation policy is the any-of rule documented on `run()`
below (safety trigger / low confidence / cross-visit recurrence, plus the
upstream-escalation floor), together with the clarification ask/proceed
handshake. Tests live in tests/agents/test_hitl.py.

2026-09-26 — THE INTERVIEW (docs/design/specs/2026-09-26-clarifying-chat-
interview-design.md). "Ask" is no longer a one-shot replacement for a
below-0.5 escalation: while no floor has fired, the acuity is not P1/P2, fewer
than INTERVIEW_MAX_QUESTIONS answers are on the request and confidence is below
INTERVIEW_CONFIDENCE_TARGET, this worker asks the classifier's next question and
the turn ends there (the Supervisor skips Reflection and Handoff). Every floor
still wins outright, and once the budget is spent the 0.5 floor decides exactly
as before. The one new state field is `clarification_asked`, which the API keys
the turn's shape on. References below to "the one round" / "never twice"
predate this and now mean "never the same question twice, within the budget".

This file began as a scaffold whose policy was left to its owner; that policy
now exists, so the former "TEMPLATE — IMPLEMENTATION REMOVED" banner has been
removed. It is still classified POLICY_NODE rather than AGENT: the action space
(escalate / ask / proceed) is implemented, but each choice is a deterministic
threshold comparison, not per-case reasoning. `CAPABILITY.justification` states
why that is the honest classification today, and `CAPABILITY.upgrade_path`
records exactly what would have to change for it to become an AGENT.

⚠ THIS IS THE SAFETY-CRITICAL ONE
---------------------------------
Every other agent can be wrong and be caught downstream. This one is the last
gate before a patient is handled without a clinician looking at the case, so it
is worth being deliberate about the two directions of error:

- A MISSED escalation (false negative) is a patient harm.
- An UNNECESSARY escalation (false positive) floods the clinician queue. Escalate
  everything and you have technically achieved perfect recall while making the
  human review meaningless — which is the same failure, one step removed.

So do NOT evaluate this agent on recall alone. Evaluation E5 in
docs/vault/Evaluation Plan.md gates on red-flag recall AND specificity >= 0.80
for exactly this reason, over a fixture with a deliberate must-NOT-escalate
half. Read it before you design the policy, not after.

WHAT YOU MUST SATISFY
---------------------
1. CONTRACT — you may mutate ONLY `escalated` and `escalation_reason`.
   You may NOT revise the acuity or the routing to force an escalation; this
   agent observes the pipeline's conclusion, it does not rewrite it.
2. `escalation_reason` must be human-readable and must say WHY. A clinician
   opens the queue and reads this string to triage their own work — "escalated:
   true" tells them nothing. Set it to None when not escalating.
3. TOOL_ALLOWLIST — `escalation.create` only. Note you do not call it here:
   main.py calls `enforce_tool_access(supervisor.hitl, "escalation.create")`
   immediately before `store.create_escalation_from_case`, so the allow-list
   entry is what authorises the platform to act on your decision.
4. `run()` is SYNCHRONOUS — no `async`.
5. Escalating must be monotone with safety. If Safety-Override fired
   (`state.safety_triggered`), a human sees the case. That is a floor, not a
   heuristic — do not let any other signal cancel it. The same applies to any
   floor set BEFORE this agent runs (see `state.escalated` below) — `run()`
   must never clear an escalation it did not itself decide against.

SIGNALS AVAILABLE TO YOU
------------------------
    state.safety_triggered / safety_rule   deterministic red-flag override fired
    state.confidence                       classifier confidence; compare against
                                           CONFIDENCE_THRESHOLD from base.py —
                                           import the constant, don't hardcode 0.5
    state.acuity_code                      final acuity, post safety-override
    state.cross_visit_escalation / _reason [Agentic] episodic memory: set by the
                                           Supervisor when a returning patient
                                           presents at equal-or-higher acuity
    state.escalated                        starts False; yours to set. CAN
                                           already be True when `run()` is
                                           called — e.g. Supervisor's Safety
                                           A2A-protocol fail-safe
                                           (`_apply_safety_failsafe`) forces
                                           review before this agent runs at
                                           all. Treat an incoming True as a
                                           floor, same as `safety_triggered`:
                                           fold it into your reasons, never
                                           silently overwrite it back to False.
    received_payload("acuity.classified")  [Agentic][A2A] carries the
    ["clarification"]                      classifier's clarification proposal
                                           (`{feature, label, question, gain}`
                                           or `None`) when a bus delivered an
                                           inbox — see `_clarification_proposal`
                                           below, guarded by `_consumed` so a
                                           standalone unit-test `run()` call is
                                           unaffected.

DESIGN QUESTIONS — ANSWERED (docs/vault/HITL Clarification Handshake.md, 2026-08-18)
-------------------------------------------------------------------------------------
Any-of composition, and no independent acuity floor beyond the two below, both
stand as originally designed. The clarifying-question widening (`upgrade_path`,
below) is answered as of this pass:

- **Gain threshold: 1.0.** Above the classifier's own floor
  (`MIN_INFORMATION_GAIN = 0.15` in classifier.py — everything reaching this
  agent already cleared that); this is the separate, stricter bar for whether
  the interruption is worth it FOR THE PATIENT, a value-of-information call
  James (classifier owner) explicitly declined to make on my behalf.
- **Off-limits acuity: P1_RESUSCITATION, P2_EMERGENT.** Too acute to delay
  with a follow-up question, matching the two codes this file already treats
  as expected-confident emergencies (see the acuity note in `run()`).
- **A declined proposal is recorded**, not dropped — `declinedQuestion` in the
  result dict, so the audit trail shows a question existed and was refused,
  and why (gain too low / off-limits acuity / already asked once).
- **Stays POLICY_NODE.** Three outcomes now instead of two, but the choice
  between them is still a threshold on a number a peer (the classifier)
  computed — not inference this agent performs itself.

PRECEDENT MEMORY (2026-09-24): clinician rulings are read back as long-term
memory — see `_precedents` / app/memory/precedents.py. A quorum of similar past
cases up-triaged by clinicians is a fifth floor; otherwise the summary is only
surfaced in the escalation reason. Reasoning over precedents (rather than a
quorum rule) is the remaining agency upgrade, per `CAPABILITY.upgrade_path`.
"""

# NOTE for Heriz Yusoff (HITL owner) — added 2026-09-25 by James.
# This file is unchanged; what improved around it:
# - The staff portal needs a real login now: a server-side, signed httpOnly
#   session cookie (frontend/lib/staffSession.js, /api/staff/session). The
#   escalation proxy returns 401 without it, and the password lives only in AWS
#   Secrets Manager. Clinicians sign in once; the backend key never reaches a browser.
# - Cases that must reach you still do after retrains: zero-symptom input and
#   untranslated input are forced below the confidence threshold / floored at P3.
# - /openapi.json now documents the 401/404 the escalation endpoints return.
from __future__ import annotations

from typing import ClassVar

from .base import (
    CONFIDENCE_THRESHOLD,
    GAIN_THRESHOLD,
    INTERVIEW_CONFIDENCE_TARGET,
    INTERVIEW_MAX_QUESTIONS,
    AgentContract,
    CaseState,
)
from .capability import POLICY_NODE, AgentCapability
from .messaging import AgentComms, AgentMessage, ConsumesMessages, enforce_comms

OWNER = "Heriz Yusoff"

# [Design decision — Heriz, docs/vault/HITL Clarification Handshake.md §5]
# Minimum classifier-computed information gain (see
# SeverityClassifierAgent._propose_clarification) above which asking a TEMPLATE
# question is worth the interruption. On the same scale regardless of which
# classifier path produced it (trained model or keyword table), so this one
# number holds in both deployments. Strictly stricter than the classifier's
# own floor (MIN_INFORMATION_GAIN = 0.15 in classifier.py) — anything reaching
# this agent already cleared that; this is the separate, harder bar for
# whether the interruption is worth it FOR THE PATIENT, which is this agent's
# call, not the classifier's. The value itself lives in base.py since the
# 2026-09-26 interview design (the classifier reads it too); re-exported here
# so `hitl.GAIN_THRESHOLD` keeps working for every existing test and reader.
#
# Dimension questions (source "llm" / "fallback" — onset, duration, severity,
# associated symptoms) carry no model-measured gain, so the bar does not apply
# to them; they are asked only while the interview is open (see run()).
__all__ = ["GAIN_THRESHOLD", "HumanInTheLoopAgent"]

# [Design decision — Heriz, docs/vault/HITL Clarification Handshake.md §5]
# Acuity codes too acute to delay with a follow-up question: escalate outright,
# never ask. Reflection's severity backstop (reflection._SEVERE_CODES) covers
# independently if a case at one of these codes ever slips through anyway.
_NO_ASK_ACUITY_CODES = frozenset({"P1_RESUSCITATION", "P2_EMERGENT"})


def _already_asked(state: CaseState, proposal: dict) -> bool:
    """True when this question (or its gap) is already in the transcript.

    Asking the same thing twice burns a turn on a known answer. Matched on the
    feature/dimension name AND on the question text, because a dimension
    question from the LLM may be re-worded between turns while probing the same
    gap, and a template question carries a stable feature name.
    """
    feature = str(proposal.get("feature") or "").strip().lower()
    question = " ".join(str(proposal.get("question") or "").lower().split())
    for prior in state.clarifications:
        if not isinstance(prior, dict):
            continue
        if feature and str(prior.get("feature") or "").strip().lower() == feature:
            return True
        if question and " ".join(str(prior.get("question") or "").lower().split()) == question:
            return True
    return False


class HumanInTheLoopAgent(ConsumesMessages):
    """Decides whether this case must be escalated to a clinician."""

    SLUG = "hitl"

    # [AI-Security][Agentic] FR-12 least-privilege allow-list. This worker
    # decides IF a case needs a human, and (via main.py) is the only one
    # permitted to actually create an escalation record -- see the
    # `enforce_tool_access(supervisor.hitl, "escalation.create")` call in
    # main.py immediately before `store.create_escalation_from_case`.
    TOOL_ALLOWLIST: ClassVar[list[str]] = ["escalation.create"]
    AUTONOMY_LEVEL: int = 2  # L2: deterministic gate, but the gate that hands off to a human
    PROMPT_PATTERN: str = "None -- deterministic threshold/rule check (no LLM prompt)."

    # [Point 1] Machine-checked capability declaration — see agents/capability.py.
    # This is one of the two workers the reviewer named. The honest answer is
    # below; the route to genuine agency is in `upgrade_path`, and it is the
    # highest-value upgrade available because it also unblocks the clarifying-
    # question evaluation (app/evals/plan.E2_CLARIFYING_QUESTIONS).
    CAPABILITY = AgentCapability(
        reasoning="",
        action_space=(
            "escalate to a clinician",
            ("ask the patient ONE clarifying question this turn (the interview: up to "
             "INTERVIEW_MAX_QUESTIONS turns while confidence is below "
             "INTERVIEW_CONFIDENCE_TARGET and no floor fired, per "
             "docs/design/specs/2026-09-26-clarifying-chat-interview-design.md)"),
            "do not escalate",
        ),
        memory=(
            "Precedent memory (app/memory/precedents.py): every labelled clinician decision is "
            "stored as a de-identified symptom vector + model/clinician acuity; run() retrieves "
            "the k nearest by cosine and escalates when a quorum were up-triaged by clinicians "
            "(can add an escalation, never remove one)."
        ),
        tools=("escalation.create",),
        uses_trained_model=False,
        classification=POLICY_NODE,
        justification=(
            "Floor rules ORed together — safety_triggered, the Safety semantic review flag, "
            "an escalation already set upstream, "
            "the Supervisor-supplied cross-visit flag — plus, when none of those fire and "
            "confidence alone is below threshold, a threshold comparison against a number a PEER "
            "computed (the classifier's information-gain estimate) to decide escalate vs ask. "
            "Three outcomes instead of two, but still no inference performed here: the same case "
            "always yields the same verdict, and the judgement call (the gain threshold itself) "
            "was made once, by a human, not learned or reasoned per-case."
        ),
        upgrade_path=(
            "Precedents are now retrieved (app/memory/precedents.py), but applied through a fixed "
            "quorum rule. The remaining gap to genuine agency is REASONING over them: weigh the "
            "retrieved rulings against this case's signals to choose escalate / ask / proceed per "
            "case, rather than a fixed threshold. Evaluate with app/evals/plan.E5_HITL_TRIGGER "
            "(already implemented) and E2_CLARIFYING_QUESTIONS."
        ),
    )

    # [Platform] Isolation lane — see AgentContract in base.py. `question`/
    # `clarification`/`declinedQuestion` live only in the returned dict — the
    # classifier's own `clarification` field (its lane, not this agent's)
    # already holds the proposal. `clarification_asked` is the one thing this
    # agent records on the state about it: whether it chose to ask. The API
    # keys the turn's shape (question vs decision) on that flag.
    CONTRACT = AgentContract(
        writes=frozenset({"escalated", "escalation_reason", "clarification_asked"}),
        returns=frozenset({"source", "escalated", "reason", "action"}),
    )

    # [A2A] Listens to the acuity, the safety override and the routing decision;
    # announces whether a clinician must review (Reflection consumes it).
    COMMS = AgentComms(
        publishes=frozenset({"review.decision"}),
        subscribes=frozenset({"acuity.classified", "safety.override", "care.routed"}),
    )

    def emit(self, state: CaseState) -> AgentMessage:
        """Already implemented — plumbing, not logic. Reads exactly the two
        fields `run()` is responsible for setting."""
        intent = "review.decision"
        enforce_comms(self, intent)
        return AgentMessage(
            sender=self.SLUG, recipient="reflection", intent=intent,
            payload={"escalated": state.escalated, "reason": state.escalation_reason},
        )

    def _clarification_proposal(self, state: CaseState) -> dict | None:
        """[A2A] The classifier's clarification proposal, if one was delivered.

        `received_payload("acuity.classified")["clarification"]` is either a
        `{feature, label, question, gain}` dict or an explicit `None` — the
        classifier publishes `None` rather than omitting the key when it
        decided no question is worth asking (docs/vault/HITL Clarification
        Handshake.md §2), so treat an absent PAYLOAD (old classifier, or no
        bus at all) differently from a considered `None`. Guarded by
        `_consumed` exactly like `ReflectionAgent.verify_announcements`
        (reflection.py:127): a standalone `run()` call in a unit test has no
        bus and no inbox, so this returns None and pre-widening behaviour is
        unchanged.
        """
        if not getattr(self, "_consumed", False):
            return None
        payload = self.received_payload("acuity.classified") or {}
        return payload.get("clarification")

    def run(self, state: CaseState) -> dict:
        """Decide whether this case needs a clinician — or, when nothing else
        forces review and the classifier proposed a good enough question,
        whether to ask the patient instead.

        Policy: any-of. A case is escalated when ONE OR MORE of the four
        signals fire — they are independent floors, not a weighted score, so a
        case can never argue its way past a single triggered rule by looking
        good on the other two:

          1. `state.safety_triggered`      — the deterministic red-flag override
             fired (chest pain, suicidal ideation, etc). This is a hard floor
             (module docstring, point 5): nothing may cancel it.
          2. `state.confidence < CONFIDENCE_THRESHOLD` — the classifier itself
             is not confident enough in the acuity it assigned. A confident
             wrong answer is one thing; an UNCONFIDENT one should not be acted
             on without a human, regardless of which acuity it landed on.
          3. `state.cross_visit_escalation` — the Supervisor's episodic-memory
             rule already decided this is a recurring presentation at
             equal-or-worse acuity than last visit.
          4. `state.safety_requires_human_review` — the semantic safety gate
             found a context conflict or uncertain positive that requires
             clinician review without autonomously changing acuity.

        Acuity itself does NOT independently force review here: P1/P2 cases
        that are neither a safety trigger nor low-confidence are expected to
        be confident, correctly-classified emergencies already routed to the
        Emergency Department — Reflection's high-acuity check (see
        `reflection.run`, `_SEVERE_CODES`) is the backstop for the rare case
        that slips through the other four, which is where that check earns
        its keep rather than duplicating it here.

        Reasons are ACCUMULATED, not short-circuited: if more than one signal
        fires, the string names all of them, because a clinician opening the
        queue benefits from knowing it was both a red flag AND low confidence,
        not just the first one checked.

        A fourth "signal" comes first and is not really a signal at all: if
        `state.escalated` is ALREADY True when this runs, something upstream
        of HITL decided the case needs a human before HITL even had an
        opinion — today that is only Supervisor's Safety A2A-protocol
        fail-safe (`_apply_safety_failsafe`, fired when the Safety-Override
        response is missing, stale, malformed, or de-escalating). That is a
        floor exactly like `safety_triggered` (module docstring, point 5):
        folded into `reasons` up front so it survives the unconditional
        reassignment below instead of being silently reset to False.

        ASKING (docs/vault/HITL Clarification Handshake.md). When confidence
        alone is the reason for review — no upstream floor, no safety trigger,
        no cross-visit recurrence — the classifier's clarification proposal
        (`_clarification_proposal`) gets a chance to replace the escalation
        with a question instead, but ONLY when every one of these holds:
        gain >= GAIN_THRESHOLD, acuity not in _NO_ASK_ACUITY_CODES, and this
        round has not already asked one (`state.clarifications` empty — the
        one-round cap rides on the REQUEST, not server state, so it cannot run
        away). Asking never applies over a floor and never invents an
        interruption on what would otherwise be a `proceed` — it can only
        REPLACE the one escalation reason it is allowed to replace, so
        red-flag recall (E5) is unaffected by construction. A proposal that
        exists but fails one of those checks is not silently dropped: it comes
        back as `declinedQuestion` so the audit trail shows a question existed
        and was refused, and can say why.
        """
        reasons: list[str] = []

        if state.escalated:
            reasons.append(
                state.escalation_reason
                or "Escalated upstream of Human-in-the-Loop review (no reason recorded)."
            )
        if state.safety_triggered:
            reasons.append(
                f"Safety-override triggered rule '{state.safety_rule}'"
                + (f": {state.safety_reason}" if state.safety_reason else "")
                + "."
            )
        if state.safety_requires_human_review:
            reasons.append(
                state.safety_review_reason
                or "Safety semantic finding requires clinician review."
            )
        if state.cross_visit_escalation:
            reasons.append(state.cross_visit_reason or "Cross-visit recurrence rule triggered.")
        # [Agentic] Precedent memory: a quorum of similar past cases that
        # clinicians ruled MORE urgent than this acuity is one more floor. It
        # can add a reason, never remove one (monotone with safety).
        precedent = self._precedents(state)
        if precedent and precedent["escalate"]:
            reasons.append(
                f"{precedent['text']} Clinicians up-triaged {precedent['upTriaged']}/"
                f"{precedent['k']} similar presentations above {state.acuity_code}."
            )

        floors_triggered = bool(reasons)
        low_confidence = state.confidence < CONFIDENCE_THRESHOLD
        confidence_reason = (
            f"Classifier confidence {state.confidence:.2f} is below the "
            f"{CONFIDENCE_THRESHOLD:.2f} escalation threshold."
        )

        action = "proceed"
        question: str | None = None
        clarification: dict | None = None
        declined_question: dict | None = None

        # [Agentic] THE INTERVIEW (2026-09-26 design). While no floor has fired,
        # the acuity is not off-limits, the question budget is not spent and the
        # classifier is still short of INTERVIEW_CONFIDENCE_TARGET, this agent
        # asks the classifier's proposed question instead of deciding. This
        # widens the old "ask only below the 0.5 floor, once" rule in two ways —
        # more turns, and a higher bar — and narrows it in none: every floor
        # still wins outright, and when the budget is spent the 0.5 floor below
        # decides exactly as it always did.
        interview_open = (
            len(state.clarifications) < INTERVIEW_MAX_QUESTIONS
            and state.confidence < INTERVIEW_CONFIDENCE_TARGET
        )

        if floors_triggered:
            # A floor already forces escalation; still name low confidence if
            # it also applies, same accumulation behaviour as before asking
            # existed. Asking is never considered here (module docstring,
            # design note constraint 2).
            if low_confidence:
                reasons.append(confidence_reason)
        else:
            proposal = self._clarification_proposal(state) if interview_open else None
            # A proposal that is not a dict, or whose gain is not a number, is
            # no usable question: it came over the bus from a remote or older
            # classifier and must degrade to "no question", never raise.
            if not isinstance(proposal, dict) or not isinstance(proposal.get("gain"), int | float):
                proposal = None
            # A template question must clear the gain bar (it is a model-measured
            # number). A dimension question (source "llm"/"fallback") has no such
            # number and is asked on the interview's own terms. A proposal with no
            # `source` came from an older classifier: treat it as a template.
            source = str(proposal.get("source") or "template") if proposal is not None else ""
            eligible = (
                proposal is not None
                and (source != "template" or proposal["gain"] >= GAIN_THRESHOLD)
                and state.acuity_code not in _NO_ASK_ACUITY_CODES
                and not _already_asked(state, proposal)
            )
            if eligible:
                action = "ask"
                question = proposal.get("question")
                clarification = proposal
            else:
                if proposal is not None:
                    declined_question = proposal
                if low_confidence:
                    reasons.append(confidence_reason)

        state.clarification_asked = action == "ask"
        if action == "ask":
            state.escalated = False
            state.escalation_reason = None
        else:
            state.escalated = bool(reasons)
            state.escalation_reason = " ".join(reasons) if reasons else None
            action = "escalate" if state.escalated else "proceed"
            # Surface the precedent in the clinician's packet even when it did
            # not itself force the escalation — context, not a verdict.
            if state.escalated and precedent and not precedent["escalate"]:
                state.escalation_reason += f" {precedent['text']}"

        result = {
            "source": "deterministic",
            "escalated": state.escalated,
            "reason": state.escalation_reason,
            "action": action,
        }
        if action == "ask":
            result["question"] = question
            result["clarification"] = clarification
        if declined_question is not None:
            result["declinedQuestion"] = declined_question
        if precedent:
            result["precedents"] = precedent
        return result

    def _precedents(self, state: CaseState) -> dict | None:
        """k-nearest clinician rulings on similar past presentations (app/memory).
        Best-effort: no memory, no ML layer or any fault -> None, i.e. the
        policy is exactly what it was without memory."""
        try:
            from ..memory import precedents

            return precedents.consult(state.normalised_symptoms or state.raw_text, state.acuity_code)
        except Exception:  # noqa: BLE001 - memory is advisory; the safety gate must not fail on it
            return None
