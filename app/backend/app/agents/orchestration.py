"""Pipeline orchestration — the engine the Symptom-Intake agent drives.

OWNER: Sham Goh (Symptom-Intake). MOVED FROM `supervisor.py`, which was
platform-owned (James), when intake took over the orchestrator role.

This module holds only the MACHINERY: the fixed, safety-gated step sequence, the
A2A publish/deliver plumbing, the safety gate, and the bounded Reflection loop.
It deliberately does NOT declare `SLUG`, `CAPABILITY` or `COMMS` — those belong
to the agent that mixes it in, which is `SymptomIntakeAgent`. That is what makes
intake *be* the orchestrator rather than something the orchestrator calls.

`PipelineOrchestrator` never imports intake, so there is no import cycle: it
treats `self` as the intake worker (`self.intake = self`) and instantiates the
other five. A class mixing it in must therefore BE the intake agent.

Every worker follows the same pattern: try an LLM-backed reasoning step first
(structured JSON prompt -> parse), and on ANY failure (LLM down, timeout,
unparsable output) fall back to deterministic keyword/rule logic. This
guarantees the pipeline produces a sensible result even with the LLM completely
off, which is a hard requirement for demo reliability.

`orchestrate` is a hand-rolled async generator that mutates a shared
`CaseState`. Intake -> Classifier -> Safety run in a fixed order; the rest of
the case follows a per-case plan from `planner.py` (Plan-and-Execute), which is
validated against an allowed-transition graph and falls back to the old fixed
sequence when rejected. It is intentionally simple. A framework
such as LangGraph could replace it with an explicit state graph (nodes =
workers, edges = routing conditions) without changing any worker logic.

PUBLIC API — what you may call from your own agent or tests
-----------------------------------------------------------
These are supported. They will not be renamed without telling you:

    orchestrate(state, *, audit, log, delay)   drive the whole pipeline
    new_session()                              a private worker set for ONE request
    deliver_inbox(bus, agent)                  hand an agent its filtered inbox
    publish(bus, state, message, audit)        record a message (bus + state + audit)
    open_message(state)                        the `case.opened` message
    safety_request(state, classifier_seq)      the Phase 2 request to Safety
    verify_safety_response(bus, state, seq)    the route gate; returns issues
    apply_safety_failsafe(state, issues)       force review, never lower acuity
    build_rationale(state) / build_citations(state)

Everything else is internal. In particular `_timed_async`, `_timed_sync`,
`_safety_pregate`, `_apply_cross_visit_rule`, `_reflection_rerun` and
`_bump_more_urgent` are mechanics, not contract — they may change shape.

Why this list exists: before it, twelve underscore-prefixed methods were being
called from four teammates' test files. Private by naming convention, public in
practice — which is the worst of both, because there was no stability promise
but real code depended on them. The six most-used are now public names, with the
old underscore spellings kept as aliases so nothing had to change on a deadline.

If you need something that is not on this list, ask rather than reaching for the
underscore — if it is genuinely part of the contract it should be promoted, and
if it is not, depending on it will hurt you later.
"""

# NOTE for Sham Goh (orchestration owner) — added 2026-09-25 by James.
# Two fixes in this file, both from the live scenario suite:
# - `_caution_nudge` (used by `_reflection_rerun`): a persistently uncertain case
#   is still nudged one level more urgent, but the nudge never CREATES a P1
#   ("call 995") without a safety rule, and never bumps unreadable input. Live
#   MAP4 (65+ week-long cough, "no fever, no breathlessness") went P2 -> P1.
# - `_apply_unreadable_input_floor` sets `state.unreadable_input` (new field in
#   base.py) so the nudge can tell "no translation" from "no evidence".
# Result: live suite 32/32 on release 3494a3c. Tests: tests/test_pipeline.py
# (test for `_caution_nudge`, Malay/Tamil unreadable cases).
from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .. import config, metrics, rag, redflags, tracing
from ..models import ACUITY_ORDER, AcuityCode, acuity_rank, more_severe
from . import planner
from .base import (
    AGENT_LABELS,
    CONFIDENCE_THRESHOLD,
    AgentUnavailableError,
    CaseState,
    enforce_tool_access,
)
from .classifier import SeverityClassifierAgent
from .handoff import ClinicianHandoffAgent
from .hitl import HumanInTheLoopAgent
from .messaging import AgentMessage, MessageBus, enforce_comms
from .reflection import REFLECTION_BUDGET_MS, REFLECTION_MAX_ITERS, ReflectionAgent
from .routing import CareRoutingAgent, reroute, tier_for_acuity, tier_rank
from .safety import SafetyOverrideAgent

#: Intents the orchestrating agent must add to its own `COMMS.publishes` /
#: `COMMS.subscribes` to be allowed to run this engine. Declared here, beside the
#: code that emits them, so the two cannot drift; `SymptomIntakeAgent` unions
#: them into its declaration and `test_capability.py` checks it did.
ORCHESTRATOR_PUBLISHES = frozenset({"case.opened", "safety.assessment.requested"})
ORCHESTRATOR_SUBSCRIBES = frozenset({"decision.reviewed", "safety.override"})

#: The `source` of a step whose agent container did not answer.
UNAVAILABLE = "unavailable"

#: The agents whose silence alone must put a case in front of a clinician.
DECISION_AGENTS = frozenset({"classifier", "safety", "routing", "hitl"})

#: Pseudo-tools the orchestration role needs in its least-privilege allow-list.
#: Routing between workers and aggregating their results are the only two things
#: this engine does that are not delegated.
ORCHESTRATOR_TOOLS = ("route", "aggregate")


# --------------------------------------------------------------------------
# Patient-facing wording
#
# `build_rationale` output is rendered on the PATIENT's result card, not just in
# the clinician packet. Acuity codes, two-decimal confidences and agent-prefixed
# escalation reasons are all precise and all unreadable to a member of the
# public, so they are translated here rather than leaking to the screen.
# --------------------------------------------------------------------------

#: Plain-English gloss per acuity code. The code is kept as a short parenthetical
#: so a clinician reading the same sentence still sees the exact tier.
_ACUITY_PHRASE: dict[str, str] = {
    AcuityCode.P1.value: "a life-threatening emergency (P1)",
    AcuityCode.P2.value: "an emergency (P2)",
    AcuityCode.P3.value: "urgent, but not an emergency (P3)",
    AcuityCode.P4.value: "not urgent (P4)",
    AcuityCode.P5.value: "something you can look after at home (P5)",
}

#: Agents stamp their name onto escalation reasons for the audit trail
#: ("Reflection: P1_RESUSCITATION requires clinician confirmation."). Useful in a
#: log, noise on a result card. Matched as a fixed set rather than a generic
#: `^\w+:` so a reason like "Low confidence: 0.45" keeps its number.
_REASON_PREFIX = re.compile(
    # The optional qualifier catches "Reflection re-run:" as well as
    # "Reflection:" — both are produced by reflection.py.
    r"^(?:Reflection|HITL|Human-in-the-Loop|Safety|Classifier|Routing)"
    r"(?:\s+re-run)?\s*:\s*",
    re.IGNORECASE,
)

#: The two commonest escalation reasons are written for an auditor, not a
#: patient. Rewritten HERE, at the point of display, rather than in `hitl.py` /
#: `safety.py` — those strings are owned by other agents and the audit trail
#: should keep the precise numbers. (pattern, replacement) applied in order.
_REASON_REWRITES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"Classifier confidence (\d*\.?\d+) is below the (\d*\.?\d+) "
            r"escalation threshold\.?",
            re.IGNORECASE,
        ),
        r"we were not confident enough in this assessment.",
    ),
    (
        re.compile(
            r"Safety-override triggered rule '([A-Z0-9_]+)'(?::\s*([^.]+))?\.?",
            re.IGNORECASE,
        ),
        r"a safety rule flagged it.",
    ),
]


def _is_emergency(code: str) -> bool:
    """True for the tiers where the patient must act now (P1/P2).

    Derived from the shared acuity ordering rather than another hard-coded set —
    `reflection._SEVERE_CODES` and `hitl._NO_ASK_ACUITY_CODES` are already two
    copies of this idea and a third would be one more thing to keep in sync.
    """
    return acuity_rank(code) <= acuity_rank(AcuityCode.P2.value)


def _acuity_phrase(code: str) -> str:
    return _ACUITY_PHRASE.get(code, f"priority {code}")


def _plain_reason(reason: str) -> str:
    """Turn an internal escalation reason into something a patient can read.

    Kept as a STANDALONE SENTENCE rather than folded into a "because ..."
    clause. Every reason in the codebase is already a full sentence
    ("Classifier confidence 0.45 is below the 0.60 escalation threshold.",
    "Safety protocol issue: ... — forced clinician review."), so subordinating
    them produced garbage like "because Low confidence: 0.45, and may update".
    """
    text = _REASON_PREFIX.sub("", (reason or "").strip())
    for pattern, replacement in _REASON_REWRITES:
        text = pattern.sub(replacement, text)
    for code, phrase in _ACUITY_PHRASE.items():
        text = text.replace(code, phrase)
    # Reasons are assembled by concatenation upstream, and several of the parts
    # already end in a full stop — hence "...flagged it..". Collapse rather than
    # chase every producer.
    text = re.sub(r"\.{2,}", ".", text).strip()
    # Reasons are concatenated from several agents, and a rewritten fragment can
    # land mid-string ("...escalated for caution. we were not confident..."").
    # The FIRST character is left alone on purpose — it follows a colon.
    text = re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    if text and not text.endswith((".", "!", "?")):
        text += "."
    return text


def _clean_clause(text: str) -> str:
    """Make a standalone fragment safe to drop into the middle of a sentence.

    Evidence strings and safety reasons are written as little sentences by the
    agents that produce them ("Possible acute coronary syndrome (chest pain
    pattern)."), so inlining them raw gives a capital letter and a full stop
    mid-clause. Acronyms keep their case — "ECG" must not become "eCG".
    """
    clause = (text or "").strip().rstrip(".").strip()
    if not clause:
        return ""
    first = clause.split(maxsplit=1)[0]
    if first.isupper() and len(first) > 1:
        return clause
    return clause[0].lower() + clause[1:]


def _join_evidence(evidence: list[str]) -> str:
    """"a, b and c" — a comma-separated list reads as machine output."""
    items = [c for c in (_clean_clause(e) for e in evidence) if c]
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _destination(care_tier: str, clinic: str) -> str:
    """Name the place to go once, not twice.

    For P1 the tier is "Emergency Department" and the clinic is "Call 995 /
    nearest Emergency Department", so joining them blindly says it twice.
    """
    tier, place = (care_tier or "").strip(), (clinic or "").strip()
    if not place:
        return tier
    if not tier or tier.lower() in place.lower():
        return place
    return f"{tier} — {place}"


async def _no_delay() -> None:
    """The pacing callback an urgent plan uses instead of the caller's."""


def _accepts_timeout(agent: object) -> bool:
    """Does this agent's `areason` take the budget argument? Older doubles,
    remote proxies and third-party agents may not; they are called as before."""
    try:
        params = inspect.signature(agent.areason).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "timeout_s" or p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)


def _caution_nudge(code: str, *, safety_triggered: bool, unreadable_input: bool,
                   interviewed: bool = False) -> str:
    """The acuity a persistently low-confidence case is nudged to.

    One level more urgent, with three exceptions:
    - unreadable input (untranslated, LLM down) is already floored at P3 and
      with a clinician; its low confidence is the missing translation;
    - an interviewed case: the patient has answered the questions, so what
      remains uncertain is what a clinician resolves, and the case goes to one
      either way. The bump only added over-triage — itchy scalp, calf cramps
      and a stopped nosebleed at P2 -> Emergency Department (AWS soak,
      2026-09-27) — and drove most of the acuity differences between LLMs in
      the model head-to-head, since whether it ran depended on the critic;
    - uncertainty never CREATES a resuscitation case. P1 means "call 995" and
      comes from a red flag or a confident classification; the nudge stops at
      P2 and the case goes to a clinician. Live 2026-09-25: a 65+ week-long
      cough with "no fever, no breathlessness" went P2 (confidence 0.45) -> P1.
    """
    if unreadable_input or interviewed:
        return code
    bumped = PipelineOrchestrator._bump_more_urgent(code)
    if bumped == "P1_RESUSCITATION" and not safety_triggered:
        return code
    return bumped


class PipelineOrchestrator:
    """The fixed, safety-gated step sequence — mixed into the agent that drives it.

    Order matters: intake -> classifier -> safety (can only escalate acuity,
    runs regardless of classifier confidence) -> routing (uses the
    post-override acuity) -> human-in-the-loop (uses post-override state).

    A MIXIN, NOT AN AGENT. It declares no `SLUG`, `CAPABILITY` or `COMMS`; the
    class that mixes it in supplies those, and `self.intake = self` means that
    class must BE the intake worker. This is the whole mechanism by which intake
    holds the orchestrator role instead of being called by a separate Supervisor.

    It never calls the LLM, RAG, the clinic directory or the red-flag rules
    itself — it only sequences workers and aggregates what they return.
    """

    def __init__(
        self,
        *,
        classifier: SeverityClassifierAgent | None = None,
        safety: SafetyOverrideAgent | None = None,
        routing: CareRoutingAgent | None = None,
        hitl: HumanInTheLoopAgent | None = None,
        reflection: ReflectionAgent | None = None,
        handoff: ClinicianHandoffAgent | None = None,
    ) -> None:
        """Build the worker set, or adopt one the caller already built.

        [Concurrency] The workers are PER-CASE, not per-process. Several of them
        keep per-case mutable state on `self` — `ConsumesMessages.received` /
        `_consumed`, and Safety's `_last_result` / `_last_protocol_issues`, which
        is exactly what `emit()` reads to build its message. `orchestrate()` is
        an async generator that suspends at every yield and every await, so two
        triages in flight on one event loop INTERLEAVE: with a single shared
        worker set, patient A's `safety.override` message can be built from
        patient B's run, and A's audit trail, SSE stream and API response then
        carry B's channel, correlation and review flag.

        So the caller passes a fresh set per request (see `new_session`, which
        is what `main._triage_event_stream` uses) while the EXPENSIVE things
        stay shared: the clinic dataset, the hours snapshot, the OneMap client
        and its auth token, the route cache and Safety's semantic layer are all
        stateless with respect to a case and are handed down rather than rebuilt.
        The no-argument form is unchanged, so every standalone construction —
        tests, evaluation harnesses, `Supervisor()` — keeps working.
        """
        # [Orchestrator] The intake step is THIS object. There is no separate
        # intake worker to construct, and constructing one would mean the
        # orchestrator ran a different instance than the one holding the role.
        self.intake = self
        self.classifier = classifier or SeverityClassifierAgent()
        self.safety = safety or SafetyOverrideAgent()
        self.routing = routing or CareRoutingAgent()
        self.hitl = hitl or HumanInTheLoopAgent()
        self.reflection = reflection or ReflectionAgent()
        # [Agentic] Runs LAST and only when the case is escalated — see the
        # handoff step at the end of `orchestrate()` for why the order is
        # load-bearing rather than incidental.
        self.handoff = handoff or ClinicianHandoffAgent()
        # [Loop Engineering] iteration-cap stop rule for the Reflection loop.
        self.reflection_max_iters = max(1, REFLECTION_MAX_ITERS)
        #: [Agentic] The last plan `orchestrate` ran (planner.Plan.to_dict()), read
        #: by main.py for the final payload. Per-case because the session is.
        self.plan: dict | None = None

    def new_session(self) -> PipelineOrchestrator:
        """[Concurrency] A private worker set for ONE request.

        Returns a new instance of THIS agent's class — the orchestrator role
        travels with the type, so this works without importing the agent that
        mixes the engine in (which is what keeps orchestration.py free of an
        import cycle with intake.py).

        What is rebuilt is exactly what holds per-case state; what is handed
        down is exactly what is expensive to build and safe to share:

            ClinicLookupTool       the CHAS dataset (a network download + parse)
            GPGoWhereHoursDirectory the reviewed hours snapshot (file + parse)
            OneMapClient           holds an auth TOKEN — re-creating it per
                                   request would re-authenticate per triage
            the route cache        public directory geometry, not case data
            SemanticRedFlagLayer   a stateless prompt template

        `use_onemap=False` because the shared client (or the deliberate absence
        of one, when credentials are missing) is passed in explicitly — the
        constructor must not try to build a second one behind our back.
        """
        if config.AGENT_TRANSPORT == "http":
            # [Microservices] Remote workers hold nothing expensive and no state
            # beyond one request: a fresh set per request, nothing handed down.
            from ..microservices.remote import remote_workers

            return type(self)(**remote_workers())
        return type(self)(
            classifier=SeverityClassifierAgent(),
            # The NLP bundle is handed down for the same reason the semantic
            # layer is: it is expensive to build (models loaded once at startup)
            # and holds no per-case state. Rebuilding it per request would mean
            # loading models per triage; dropping it would silently disable the
            # live NLP layer for every real request while the tests still pass.
            safety=SafetyOverrideAgent(
                semantic=self.safety.semantic,
                nlp_adapters=getattr(self.safety, "nlp_adapters", None),
            ),
            routing=CareRoutingAgent(
                lookup=self.routing.lookup, hours=self.routing.hours, maps=self.routing.maps,
                use_onemap=False, route_cache=self.routing._route_cache,
            ),
            hitl=HumanInTheLoopAgent(),
            reflection=ReflectionAgent(),
            handoff=ClinicianHandoffAgent(),
        )

    # ------------------------------------------------------------------
    # [Agentic] Orchestration — the orchestrating agent's second job.
    #
    # `orchestrate` is an async GENERATOR that drives the five workers (+ the
    # Reflection critic) in the fixed, safety-gated order and YIELDS SSE-ready
    # step-event dicts. It is deliberately decoupled from the SSE transport:
    # the caller (main._triage_event_stream) wraps each yielded dict with
    # `_sse(...)` and handles it, while side-effects (audit trail, operator log,
    # inter-step animation delay) are injected as callbacks. This keeps ALL the
    # sequencing/routing/looping logic in ONE testable place instead of being
    # interleaved with HTTP streaming code.
    # ------------------------------------------------------------------
    @staticmethod
    async def _timed_async(coro: Awaitable[dict]) -> tuple[dict, float]:
        start = time.perf_counter()
        result = await coro
        return result, time.perf_counter() - start

    @staticmethod
    def _timed_sync(fn: Callable[[], dict]) -> tuple[dict, float]:
        start = time.perf_counter()
        result = fn()
        return result, time.perf_counter() - start

    # ------------------------------------------------------------------
    # [Microservices] Calling a worker that may live in another container.
    # ------------------------------------------------------------------
    async def _call(self, agent: object, method: str, *args: Any) -> Any:
        """`agent.method(*args)`, awaited when it is async.

        In-process workers mix sync (`safety.run`, `hitl.run`, every `emit`)
        and async (`classifier.run`) methods; a RemoteAgent's are all async
        because each one is a network call. This is the one place that
        difference is absorbed, so the step sequence below reads the same for
        both transports.
        """
        out = getattr(agent, method)(*args)
        if inspect.isawaitable(out):
            out = await out
        return out

    async def _run_worker(self, state: CaseState, slug: str, agent: object) -> tuple[dict, float]:
        """Run one worker; a container that cannot answer degrades the step."""
        start = time.perf_counter()
        span = tracing.agent_start(slug)
        try:
            result = await self._call(agent, "run", state)
        except AgentUnavailableError as exc:
            result = self._degrade(state, slug, exc)
            tracing.end(span, error=str(exc)[:200])
        else:
            tracing.end(span)
        return result, time.perf_counter() - start

    async def _shadow_nlp(self, state: CaseState) -> None:
        """Safety's NLP pass, in SHADOW mode: it records telemetry and never
        changes acuity (safety.shadow_nlp).

        It is a step of the Safety agent, not of this process, so it runs on
        BOTH transports — skipping it when the workers are remote would leave
        `safety_nlp_telemetry` empty over HTTP and populated in-process, i.e.
        two different cases for the same patient. In-process the call is
        blocking model inference and goes to a thread; the remote proxy is
        already async (one more call to the safety container, which reads its
        own config because it owns the models).
        """
        shadow = getattr(self.safety, "shadow_nlp", None)
        if shadow is None:
            return
        with contextlib.suppress(AgentUnavailableError):
            if inspect.iscoroutinefunction(shadow):
                await shadow(state)
            else:
                await asyncio.to_thread(
                    shadow, state,
                    enabled=config.SAFETY_NLP_ENABLED and not config.KILL_SWITCH,
                )
        if config.SAFETY_NLP_TEST_FORCE_UNCERTAINTY:
            summary = state.safety_nlp_telemetry.setdefault("summary", {})
            summary["uncertaintyCount"] = max(1, int(summary.get("uncertaintyCount", 0)))
            summary["testOnlyForcedUncertainty"] = True

    async def _reason(self, agent: object, state: CaseState, timeout_s: float | None = None) -> None:
        """The additive LLM layers (Safety's semantic flags, Reflection's critic).
        Losing one leaves the deterministic step to decide alone — exactly what
        happens when the LLM itself is down — so it is not an error here.
        `timeout_s` is the caller's remaining budget for a layer that accepts one."""
        with contextlib.suppress(AgentUnavailableError):
            if timeout_s is None or not _accepts_timeout(agent):
                await self._call(agent, "areason", state)
            else:
                await self._call(agent, "areason", state, timeout_s)

    async def _announce(self, bus: MessageBus, state: CaseState, agent: object, result: dict,
                        audit: Callable[..., None]) -> dict | None:
        """Publish `agent`'s message for this step; None when it has none to give."""
        if result.get("source") == UNAVAILABLE:
            return None
        try:
            message = await self._call(agent, "emit", state)
        except AgentUnavailableError as exc:
            slug = getattr(agent, "SLUG", "agent")
            audit(actor=slug, action="announcement_unavailable", detail=str(exc))
            # A decision nobody announced cannot be attributed or checked
            # downstream, so it is treated like the agent being down.
            if slug in DECISION_AGENTS:
                self._escalate_unavailable(state, slug)
            return None
        return self.publish(bus, state, message, audit)

    @staticmethod
    def _escalate_unavailable(state: CaseState, slug: str) -> None:
        """Force clinician review because `slug` did not answer. The reason is
        appended once: a Reflection re-run can degrade the same agent again."""
        reason = f"{AGENT_LABELS.get(slug, slug)} unavailable — escalated for clinician review."
        state.escalated = True
        if reason in (state.escalation_reason or ""):
            return
        state.escalation_reason = (
            f"{state.escalation_reason} {reason}".strip() if state.escalation_reason else reason
        )

    def _degrade(self, state: CaseState, slug: str, exc: Exception) -> dict:
        """[Microservices][Responsible-AI] The step's agent did not answer.

        Never a less cautious outcome: any of the four decision agents missing
        puts the case in front of a clinician. Safety needs no special case
        here — its missing `safety.override` makes the route gate fail closed
        below, exactly as a malformed reply would. Acuity is never touched.

        Reflection is not a decision agent, but it carries two deterministic
        backstops (the care-tier floor, and escalating a P1/P2 nothing else
        escalated). Losing its container must not lose those, so a missing
        Reflection escalates too and applies the tier floor here — raising
        the tier only, never lowering one already more cautious.
        """
        label = AGENT_LABELS.get(slug, slug)
        result: dict = {"source": UNAVAILABLE, "error": type(exc).__name__}
        if slug == "reflection":
            result.update(passed=False, issues=[f"{label} unavailable"], corrections=[], rerun_suggested=False)
            if tier_rank(state.care_tier) < tier_rank(tier_for_acuity(state.acuity_code)):
                # The same network-free floor Reflection itself applies: it
                # also clears navigation that described the abandoned clinic.
                reroute(state)
                result["corrections"] = [f"Re-routed to {state.care_tier}."]
            self._escalate_unavailable(state, slug)
            return result
        if slug == "handoff":
            return result
        if slug == "safety":
            result.update(triggered=False, rule=None, reason=f"{label} unavailable",
                          prior_acuity=state.acuity_code, forced_acuity=None, channel=None)
        if slug == "routing":
            state.care_tier = tier_for_acuity(state.acuity_code)
            state.routing_reason = "Clinic routing unavailable; showing the care tier only."
        self._escalate_unavailable(state, slug)
        return result

    # ------------------------------------------------------------------
    # [A2A] Agent-to-agent messaging — see agents/messaging.py.
    # ------------------------------------------------------------------
    def publish(self, bus: MessageBus, state: CaseState, message: AgentMessage,
                audit: Callable[..., None]) -> dict:
        """Record `message` on the bus + the case's message log + the audit trail,
        and return the SSE-ready `agent_message` event to yield."""
        bus.publish(message)
        state.messages.append(message.to_dict())
        audit(actor=message.sender, action="message",
              detail=f"intent={message.intent} -> {message.recipient} payload={message.payload}")
        return {"event": "agent_message", **message.to_dict()}

    def deliver_inbox(self, bus: MessageBus, agent: object) -> list[AgentMessage]:
        """[A2A] Hand `agent` the messages it subscribed to, before it runs.

        This is what makes the bus load-bearing rather than decorative: an agent
        that overrides `consume()` reasons about what its peers ASSERTED, not
        just about the CaseState they mutated. Agents that do not consume are
        unaffected, so adding this cannot change existing behaviour.
        """
        inbox = bus.inbox(agent)
        consume = getattr(agent, "consume", None)
        if callable(consume):
            consume(inbox)
        return inbox

    def open_message(self, state: CaseState) -> AgentMessage:
        """The orchestrator's own opening message that starts the conversation.
        Deliberately carries NO raw patient text (kept out of the bus/audit)."""
        enforce_comms(self, "case.opened")
        return AgentMessage(
            sender=self.SLUG, recipient="intake", intent="case.opened",
            payload={"language": state.language, "is_voice": state.is_voice},
        )

    def safety_request(self, state: CaseState, classifier_seq: int) -> AgentMessage:
        """[A2A][Phase 2] The orchestrator's directed assessment request to Safety.

        Carries ONLY the classifier's verdict and the sequence number of the
        `acuity.classified` message it refers to. Deliberately no raw text, no
        normalised text and no evidence strings: evidence is quoted from the
        patient's own words, so including it would put PHI on the bus, into the
        audit trail and into the SSE stream.

        `classifierMessageSeq` is what lets Safety prove the request refers to
        the classification it actually saw, rather than a stale one.
        """
        enforce_comms(self, "safety.assessment.requested")
        return AgentMessage(
            sender=self.SLUG, recipient="safety", intent="safety.assessment.requested",
            payload={
                "classifierAcuity": state.acuity_code,
                "classifierConfidence": round(float(state.confidence), 4),
                "classifierMessageSeq": classifier_seq,
                "fastPathDetected": bool(state.safety_fast_path),
            },
        )

    def verify_safety_response(self, bus: MessageBus, state: CaseState,
                                request_seq: int) -> list[str]:
        """[Phase 2] Check Safety's reply before Care-Routing is allowed to run.

        Returns a list of protocol issues; empty means the response is sound.
        This is a READ-ONLY check — it never mutates acuity. The deterministic
        Safety result has already been applied to CaseState by `safety.run()`,
        and nothing here may undo it.
        """
        issues: list[str] = []
        responses = bus.messages_for_intent("safety.override")
        if not responses:
            return ["no safety.override response on the bus"]

        payload = responses[-1].payload
        if payload.get("requestSeq") != request_seq:
            issues.append(
                f"requestSeq mismatch (got {payload.get('requestSeq')!r}, expected {request_seq!r})"
            )

        prior = payload.get("priorAcuity") or payload.get("prior_acuity")
        if prior is not None and prior != state.prior_acuity_code:
            issues.append(f"priorAcuity mismatch (got {prior!r}, state {state.prior_acuity_code!r})")

        forced = payload.get("forcedAcuity") or payload.get("forced_acuity")
        if forced is not None:
            if forced not in ACUITY_ORDER:
                issues.append(f"forcedAcuity {forced!r} is not a known acuity code")
            elif prior in ACUITY_ORDER and acuity_rank(forced) > acuity_rank(prior):
                # Higher rank == less urgent. Safety is escalation-only, so a
                # response that de-escalates is a protocol violation, not a
                # decision to honour.
                issues.append(f"forcedAcuity {forced!r} is LESS urgent than priorAcuity {prior!r}")

        rule = payload.get("rule")
        if rule is not None and rule not in {r.name for r in redflags.RED_FLAG_RULES}:
            issues.append(f"rule {rule!r} is outside the closed RED_FLAG_RULES vocabulary")

        return issues

    def apply_safety_failsafe(self, state: CaseState, issues: list[str]) -> None:
        """[Phase 2] Fail closed when the Safety protocol is broken.

        Forces clinician review and NEVER lowers acuity. The deterministic
        result Safety already applied stays exactly as it is — a broken protocol
        means we trust the rules MORE, not less.
        """
        state.escalated = True
        reason = ("Safety protocol issue: " + "; ".join(issues) +
                  " — forced clinician review (acuity unchanged).")
        state.escalation_reason = (
            f"{state.escalation_reason} {reason}".strip() if state.escalation_reason else reason
        )

    async def _safety_pregate(self, state: CaseState, text: str | None = None) -> None:
        """[Agentic Routing][AI-Security] Safety-first conditional routing: run
        the deterministic red-flag check EARLY (before the classifier) as a
        cheap gate. On an obvious red flag we mark a fast-path so the classifier
        can skip redundant LLM work -- the authoritative Safety-Override worker
        still runs in-order and is the only thing that can force acuity, and it
        can only ever RAISE it (escalation-only invariant preserved). An
        unreachable Safety container leaves the fast path off: it only ever
        SKIPS work, so off is the cautious default.

        Called twice: on the raw complaint before intake, and on intake's
        normalised text after it. A red flag that only exists in English after
        translation ("我胸口剧痛…" -> chest pain) or in a folded interview answer
        ("I also have difficulty breathing") missed the first check, and the
        classifier then had the LLM word a question HITL would never ask (AWS,
        2026-09-27: ~1.4 s added to a P1 result)."""
        try:
            state.safety_fast_path = bool(
                await self._call(self.safety, "prescreen", state.raw_text if text is None else text))
        except AgentUnavailableError:
            state.safety_fast_path = False

    def _apply_cross_visit_rule(self, state: CaseState) -> None:
        """[Agentic] Deterministic episodic-memory cross-visit rule: if this
        session presents the SAME symptom family as its most recent prior visit
        at EQUAL-OR-HIGHER acuity, force a clinician review. Escalation-only --
        it can never de-escalate."""
        if not state.prior_visit_acuity:
            return
        current_kw = {str(k).lower() for k in (state.intake_keywords + state.evidence) if str(k).strip()}
        prior_kw = {str(k).lower() for k in state.prior_visit_keywords if str(k).strip()}
        same_family = bool(current_kw & prior_kw)
        # Lower rank == more severe, so <= means "same or worse than last time".
        worse_or_same = acuity_rank(state.acuity_code) <= acuity_rank(state.prior_visit_acuity)
        if same_family and worse_or_same:
            state.cross_visit_escalation = True
            state.cross_visit_reason = (
                f"Cross-visit rule: recurring symptoms (same family as prior visit, "
                f"{state.prior_visit_acuity}) presenting again at equal-or-higher acuity "
                f"({state.acuity_code}) — escalated for clinician review."
            )

    @staticmethod
    def _apply_unreadable_input_floor(state: CaseState, intake_result: dict) -> None:
        """Text the deterministic path cannot read is a clinician's case.

        A non-English complaint that intake could not translate (no LLM) and
        that matched no keyword has NOT been assessed: the model scored an
        empty feature vector. Left alone it came out P4, a GP tier and an
        English clarifying question — for "chest pain, cannot breathe" written
        in Chinese. Floor it at P3 and force review; never ask."""
        language = state.detected_language or state.language or "en"
        readable = language.startswith("en") or state.intake_keywords or intake_result.get("source") == "llm"
        if readable:
            return
        state.unreadable_input = True
        state.acuity_code = more_severe(state.acuity_code, "P3_URGENT")
        state.escalated = True
        reason = (
            f"The complaint (language: {language}) could not be read by the deterministic "
            "pipeline and no translation was available; a clinician must review it."
        )
        state.escalation_reason = f"{state.escalation_reason} {reason}".strip() if state.escalation_reason else reason

    async def _reflection_rerun(self, state: CaseState, audit, log, bus: MessageBus,
                                iteration: int = 1) -> list[str]:
        """[Agentic] One Evaluator-Optimizer pass (the loop body; the Supervisor
        bounds how many run). Feeds the critique back to the classifier, then can
        only make the outcome MORE cautious: acuity is never lowered, and a
        persistently thin/low-confidence result is nudged one level more urgent
        and forced to a human. `iteration` is the 1-based pass index for telemetry."""
        state.reflection_reran = True
        # NOTE for Sham — 2026-09-16, James: when the LLM critic asked for this
        # re-run, its critique is the hint the classifier re-assesses with.
        critique = ((state.reflection or {}).get("critic") or {}).get("critique")
        state.reflection_hint = critique or (
            "prior pass had thin evidence / very low confidence — re-assess conservatively"
        )
        corrections: list[str] = []
        before = state.acuity_code

        self.deliver_inbox(bus, self.classifier)
        rerun_result, _ = await self._run_worker(state, "classifier", self.classifier)
        # Re-apply the deterministic safety gate (re-adds its signed explanation
        # contribution and can only raise acuity) — and RE-ANNOUNCE it, so
        # Care-Routing reads this pass's override, not the first pass's.
        self.deliver_inbox(bus, self.safety)
        safety_rerun, _ = await self._run_worker(state, "safety", self.safety)
        announcements: list[dict] = []
        safety_event = await self._announce(bus, state, self.safety, safety_rerun, audit)
        if safety_event is not None:
            announcements.append(safety_event)
        # Caution floor: never less severe than before the re-run.
        state.acuity_code = more_severe(state.acuity_code, before)
        if state.confidence < CONFIDENCE_THRESHOLD:
            # Unreadable input is already floored at P3 and with a clinician;
            # its low confidence is the missing translation, so a further bump
            # only turned an untranslated mild cough into P2 (live test 2026-09-25).
            bumped = _caution_nudge(state.acuity_code, safety_triggered=state.safety_triggered,
                                    unreadable_input=state.unreadable_input,
                                    interviewed=bool(state.clarifications))
            if bumped != state.acuity_code:
                state.acuity_code = bumped
                corrections.append(f"Nudged acuity {before}->{bumped} (still low confidence).")
            state.escalated = True
            reason = "Reflection re-run: thin evidence persisted — escalated for caution."
            state.escalation_reason = (
                f"{state.escalation_reason} {reason}".strip() if state.escalation_reason else reason
            )
        # Keep routing/HITL consistent with the (possibly raised) acuity.
        #
        # [A2A] Both halves of the protocol have to happen here, not just the
        # `run()`. Care-Routing READS `safety.override` from its inbox to decide
        # the tier, so re-running it without delivering one first meant it acted
        # on whatever was in `received` from the first pass — or, with a shared
        # worker set, on another request's override entirely. And Reflection
        # re-verifies the tier against what Care-Routing ANNOUNCED, so a re-route
        # that never announced itself was reported as "changed downstream without
        # being announced" — the critic accusing the pipeline of a bug the loop
        # itself had just introduced.
        #
        # The two republished messages are recorded on the bus, in
        # `state.messages` and in the audit trail; they are not yielded as SSE
        # events because this method is not a generator — the re-run reports
        # itself through the single `reflection` step event instead.
        self.deliver_inbox(bus, self.routing)
        routing_rerun, routing_dur = await self._run_worker(state, "routing", self.routing)
        routing_event = await self._announce(bus, state, self.routing, routing_rerun, audit)
        self.deliver_inbox(bus, self.hitl)
        hitl_rerun, hitl_dur = await self._run_worker(state, "hitl", self.hitl)
        hitl_event = await self._announce(bus, state, self.hitl, hitl_rerun, audit)
        # The re-run's decisions are what `final` will carry; the stream shows
        # them too, so it never displays the first pass's routing or HITL
        # verdict against a different final answer.
        self._rerun_events = [
            *announcements,
            {
                "event": "agent_result", "agent": "routing", "label": AGENT_LABELS["routing"],
                "summary": f"Re-routed to {state.care_tier}: {state.clinic} (~{state.wait_time_min} min wait)",
                "data": routing_rerun, "durationMs": round(routing_dur * 1000, 2),
            },
            *([routing_event] if routing_event is not None else []),
            {
                "event": "agent_result", "agent": "hitl", "label": AGENT_LABELS["hitl"],
                "summary": "Escalated to clinician review." if state.escalated else "No escalation required.",
                "data": hitl_rerun, "durationMs": round(hitl_dur * 1000, 2),
            },
            *([hitl_event] if hitl_event is not None else []),
        ]
        corrections.append(f"Re-ran classifier ({rerun_result.get('source')}): {before}->{state.acuity_code}.")

        audit(
            actor="reflection", action="rerun",
            detail=f"iteration={iteration} before={before} after={state.acuity_code} "
                   f"source={rerun_result.get('source')} confidence={state.confidence:.2f}",
            confidence=state.confidence, acuity=state.acuity_code,
        )
        log("step=reflection_rerun iter=%d before=%s after=%s", iteration, before, state.acuity_code)
        return corrections

    @staticmethod
    def _bump_more_urgent(code: str) -> str:
        rank = acuity_rank(code)
        if rank <= 0:
            return code
        return ACUITY_ORDER[rank - 1]

    async def orchestrate(
        self,
        state: CaseState,
        *,
        audit: Callable[..., None],
        log: Callable[..., None],
        delay: Callable[[], Awaitable[None]],
    ):
        # [AI-Security] FR-12. `route` is one of the two orchestration
        # pseudo-tools this agent declares (see ORCHESTRATOR_TOOLS); sequencing
        # the workers IS the act of using it, so the allow-list is enforced here
        # rather than only declared. Same for `aggregate` in build_rationale /
        # build_citations, the other thing this engine does on its own behalf.
        enforce_tool_access(self, "route")

        # ---- Safety-first pre-gate (does not emit / does not force acuity) ----
        await self._safety_pregate(state)

        # ---- [A2A] Open the agent-to-agent conversation ----
        bus = MessageBus()
        yield self.publish(bus, state, self.open_message(state), audit)

        # ---- 1) Symptom-Intake ----
        yield {"event": "agent_active", "agent": "intake", "label": AGENT_LABELS["intake"]}
        await delay()
        self.deliver_inbox(bus, self.intake)
        span = tracing.agent_start("intake")
        intake_result, dur = await self._timed_async(self.intake.run(state))
        tracing.end(span)
        src = intake_result.get("source", "unknown")
        metrics.observe_agent("intake", src, dur)
        audit(actor="intake", action=src,
              detail=f"normalised_symptoms={state.normalised_symptoms!r} keywords={state.intake_keywords}")
        log("step=intake source=%s", src)
        yield {
            "event": "agent_result", "agent": "intake", "label": AGENT_LABELS["intake"],
            "summary": f"Normalised to: \"{state.normalised_symptoms}\" (lang: {state.detected_language})",
            "data": intake_result, "durationMs": round(dur * 1000, 2),
        }
        yield self.publish(bus, state, self.intake.emit(state), audit)
        await delay()
        if not state.safety_fast_path and state.normalised_symptoms and state.normalised_symptoms != state.raw_text:
            await self._safety_pregate(state, state.normalised_symptoms)

        # ---- 2) Severity-Classifier ----
        yield {"event": "agent_active", "agent": "classifier", "label": AGENT_LABELS["classifier"]}
        await delay()
        self.deliver_inbox(bus, self.classifier)
        classifier_result, dur = await self._run_worker(state, "classifier", self.classifier)
        src = classifier_result.get("source", "unknown")
        metrics.observe_agent("classifier", src, dur)
        audit(actor="classifier", action=src,
              detail=f"evidence={state.evidence} explanation={state.explanation}",
              confidence=state.confidence, acuity=state.acuity_code)
        log("step=classifier source=%s acuity=%s confidence=%.2f", src, state.acuity_code, state.confidence)
        yield {
            "event": "agent_result", "agent": "classifier", "label": AGENT_LABELS["classifier"],
            "summary": ("Severity Classifier unavailable — acuity not assessed; escalated for clinician review."
                        if classifier_result.get("source") == UNAVAILABLE
                        else f"Estimated {state.acuity_code} (confidence {state.confidence:.2f})"),
            "data": classifier_result, "durationMs": round(dur * 1000, 2),
        }
        classified_event = await self._announce(bus, state, self.classifier, classifier_result, audit)
        if classified_event is not None:
            yield classified_event

        # [Phase 2] Directed request to Safety, referencing the exact
        # acuity.classified message above. Safety validates the pair and reports
        # a protocol issue if they disagree or the request is missing entirely.
        # -1 when the classifier made no announcement: Safety then reports the
        # missing `acuity.classified` as a protocol issue and the gate fails closed.
        safety_request = self.safety_request(
            state, int(classified_event["seq"]) if classified_event is not None else -1)
        safety_request_event = self.publish(bus, state, safety_request, audit)
        yield safety_request_event
        safety_request_seq = int(safety_request_event["seq"])
        await delay()

        # ---- 3) Safety-Override (authoritative, escalation-only) ----
        yield {"event": "agent_active", "agent": "safety", "label": AGENT_LABELS["safety"]}
        await delay()
        # [Agentic] Additive semantic reasoning layer, run BEFORE the sync gate so
        # `run()` stays synchronous and the Reflection loop can re-apply the gate
        # without paying for another LLM call (semantic_flags persist on state).
        # Returns None — leaving the deterministic rules to decide alone — when
        # the layer is disabled or the LLM is unavailable.
        self.deliver_inbox(bus, self.safety)
        await self._shadow_nlp(state)
        await self._reason(self.safety, state)
        safety_result, dur = await self._run_worker(state, "safety", self.safety)
        metrics.observe_agent("safety", safety_result.get("source", "deterministic"), dur)
        audit(actor="safety", action="triggered" if safety_result["triggered"] else "not_triggered",
              detail=f"rule={safety_result['rule']} reason={safety_result['reason']} "
                     f"prior_acuity={safety_result['prior_acuity']} forced_acuity={safety_result['forced_acuity']} "
                     f"fast_path={state.safety_fast_path} channel={safety_result.get('channel')} "
                     f"prototype_only={safety_result.get('prototype_only')} "
                     f"semantic_intervention={safety_result.get('semantic_intervention')} "
                     f"semantic_channel={safety_result.get('semantic_channel')}",
              confidence=state.confidence, acuity=state.acuity_code)
        log("step=safety triggered=%s rule=%s", safety_result["triggered"], safety_result["rule"])
        yield {
            "event": "agent_result", "agent": "safety", "label": AGENT_LABELS["safety"],
            "summary": (f"Red flag triggered: {safety_result['rule']}" if safety_result["triggered"]
                        else "No red flags detected."),
            "data": safety_result, "durationMs": round(dur * 1000, 2),
        }
        yield {
            "event": "safety_override", "triggered": safety_result["triggered"],
            "rule": safety_result["rule"], "priorAcuity": safety_result["prior_acuity"],
            "forcedAcuity": safety_result["forced_acuity"],
            "prototypeOnly": safety_result.get("prototype_only", False),
            "semanticIntervention": safety_result.get("semantic_intervention", "none"),
            "requiresHumanReview": safety_result.get("requires_human_review", False),
        }
        safety_event = await self._announce(bus, state, self.safety, safety_result, audit)
        if safety_event is not None:
            yield safety_event

        # [Phase 2] ROUTE GATE. Care-Routing must not start until the Safety
        # response for THIS request has been received and verified. A missing,
        # stale, malformed or de-escalating response fails closed: force review,
        # keep the deterministic result, never lower acuity.
        protocol_issues = self.verify_safety_response(bus, state, safety_request_seq)
        if protocol_issues:
            self.apply_safety_failsafe(state, protocol_issues)
            audit(actor="supervisor", action="safety_protocol_issue",
                  detail="; ".join(protocol_issues), acuity=state.acuity_code)
            log("step=safety_protocol_issue issues=%s", protocol_issues)
            yield {
                "event": "safety_protocol_issue", "issues": protocol_issues,
                "acuity": state.acuity_code, "escalated": True,
            }

        # ---- [Agentic] Plan-and-Execute: choose THIS case's remaining steps ----
        # Intake, Classifier and Safety have run; their verdict is the planner's
        # input, so Safety can never be planned away. The plan is validated
        # against planner.ALLOWED_TRANSITIONS inside `make_plan`; a rejected plan
        # or a planner exception runs the old fixed sequence (shape "fallback").
        plan = self._plan(state, audit, log)
        yield {"event": "plan", **plan.to_dict()}
        if plan.urgent:
            delay = _no_delay   # a P1/red-flag answer is not held back for UI animation
        await delay()
        ctx: dict = {"intake_result": intake_result, "hitl_result": {}}
        for step in plan.names:
            async for event in getattr(self, f"_step_{step}")(state, bus, ctx, audit, log, delay):
                yield event
            # [Agentic] The interview: an "ask" ENDS THE TURN here. HITL chose to
            # put a question to the patient instead of deciding, and the answer
            # arrives on the next request, so there is no decision for Reflection
            # to critique or Handoff to summarise yet. Letting Reflection run on
            # the provisional state is exactly what hid the question on the live
            # deployment: the LLM critic escalated the low-confidence case in the
            # same turn, and the API hides the question on an escalated case.
            # Reflection reviews the final turn in full, once the interview is
            # over (docs/design/specs/2026-09-26-clarifying-chat-interview-design.md).
            if step == "hitl" and state.clarification_asked:
                remaining = list(plan.names)[list(plan.names).index(step) + 1:]
                audit(actor=self.SLUG, action="interview",
                      detail=f"asked question {len(state.clarifications) + 1}: "
                             f"{(state.clarification or {}).get('question')!r}; "
                             f"turn ends, skipped={remaining}",
                      confidence=state.confidence, acuity=state.acuity_code)
                log("step=interview asked=%d question=%r",
                    len(state.clarifications) + 1, (state.clarification or {}).get("question"))
                yield {
                    "event": "clarification_requested",
                    "question": (state.clarification or {}).get("question"),
                    "round": len(state.clarifications) + 1,
                }
                break

    def _plan(self, state: CaseState, audit: Callable[..., None], log: Callable[..., None]) -> planner.Plan:
        """Make, record and count the plan. Looked up through the module so a
        test (or a future LLM planner) can swap `planner.plan_case`."""
        plan = planner.make_plan(state, planner.plan_case)
        self.plan = plan.to_dict()
        metrics.inc(metrics.PLAN_SHAPES, shape=plan.shape)
        audit(actor=self.SLUG, action="plan",
              detail=f"shape={plan.shape} steps={list(plan.names)}"
                     + (f" fallback_reason={plan.fallback_reason}" if plan.fallback_reason else ""),
              confidence=state.confidence, acuity=state.acuity_code)
        log("step=plan shape=%s steps=%s", plan.shape, ",".join(plan.names))
        return plan

    # ------------------------------------------------------------------
    # [Agentic] Plan steps. Each is one executor named in planner.py's step
    # vocabulary; `orchestrate` runs the validated plan through them in order.
    # `ctx` carries the two results a later step reads (intake -> HITL's
    # unreadable-input floor, HITL -> Reflection's clarification stop).
    # ------------------------------------------------------------------
    async def _step_routing(self, state: CaseState, bus: MessageBus, ctx: dict,
                            audit: Callable[..., None], log: Callable[..., None],
                            delay: Callable[[], Awaitable[None]]):
        """Care-Routing: tier, clinic and wait (the full search)."""
        # ---- 4) Care-Routing ----
        yield {"event": "agent_active", "agent": "routing", "label": AGENT_LABELS["routing"]}
        await delay()
        self.deliver_inbox(bus, self.routing)
        routing_result, dur = await self._run_worker(state, "routing", self.routing)
        metrics.observe_agent("routing", routing_result.get("source", "deterministic"), dur)
        audit(actor="routing", action="routed",
              detail=f"care_tier={state.care_tier} clinic={state.clinic} wait_time_min={state.wait_time_min}",
              acuity=state.acuity_code)
        log("step=routing tier=%s clinic=%s", state.care_tier, state.clinic)
        yield {
            "event": "agent_result", "agent": "routing", "label": AGENT_LABELS["routing"],
            "summary": (f"Care Routing unavailable — care tier {state.care_tier} only; clinician review requested."
                        if routing_result.get("source") == UNAVAILABLE
                        else f"Routed to {state.care_tier}: {state.clinic} (~{state.wait_time_min} min wait)"),
            "data": routing_result, "durationMs": round(dur * 1000, 2),
        }
        routing_event = await self._announce(bus, state, self.routing, routing_result, audit)
        if routing_event is not None:
            yield routing_event
        await delay()

    async def _step_emergency_guidance(self, state: CaseState, bus: MessageBus, ctx: dict,
                                       audit: Callable[..., None], log: Callable[..., None],
                                       delay: Callable[[], Awaitable[None]]):
        """P1 only (planner.validate enforces it): 995 guidance without Care-Routing."""
        # [Agentic] Plan-and-Execute. Care-Routing's own P1 branch never searches
        # clinics (SCDF dispatch chooses the destination; see routing.run), so
        # calling it buys nothing on the one case where seconds matter — and on
        # the http transport it is one more container that can fail. The plan
        # replaces the call with the same network-free floor Reflection uses,
        # then has Care-Routing ANNOUNCE the result as usual, so the A2A
        # conversation and Reflection's attribution check are unchanged.
        yield {"event": "agent_active", "agent": "routing", "label": AGENT_LABELS["routing"]}
        reroute(state)
        state.clinic = "Call 995 for SCDF emergency dispatch"
        state.routing_reason = ("P1: call 995 now. SCDF dispatch chooses the destination, "
                                "so the clinic search was skipped by the plan.")
        result = {"source": "plan", "care_tier": state.care_tier, "clinic": state.clinic,
                  "skipped": "clinic_search"}
        audit(actor="routing", action="emergency_guidance",
              detail=f"care_tier={state.care_tier} clinic={state.clinic} (planned; clinic search skipped)",
              acuity=state.acuity_code)
        log("step=emergency_guidance tier=%s", state.care_tier)
        yield {
            "event": "agent_result", "agent": "routing", "label": AGENT_LABELS["routing"],
            "summary": f"Emergency plan: {state.clinic} (clinic search skipped)",
            "data": result, "durationMs": 0.0,
        }
        routing_event = await self._announce(bus, state, self.routing, result, audit)
        if routing_event is not None:
            yield routing_event
        await delay()

    async def _step_hitl(self, state: CaseState, bus: MessageBus, ctx: dict,
                         audit: Callable[..., None], log: Callable[..., None],
                         delay: Callable[[], Awaitable[None]]):
        """Human-in-the-Loop, after the episodic cross-visit rule and unreadable-input floor."""
        # ---- 5) Human-in-the-Loop (episodic cross-visit rule applied first) ----
        self._apply_cross_visit_rule(state)
        self._apply_unreadable_input_floor(state, ctx["intake_result"])
        yield {"event": "agent_active", "agent": "hitl", "label": AGENT_LABELS["hitl"]}
        await delay()
        self.deliver_inbox(bus, self.hitl)
        hitl_result, dur = await self._run_worker(state, "hitl", self.hitl)
        ctx["hitl_result"] = hitl_result
        metrics.observe_agent("hitl", hitl_result.get("source", "deterministic"), dur)
        audit(actor="hitl", action="escalated" if state.escalated else "not_escalated",
              detail=state.escalation_reason or "No escalation required.",
              confidence=state.confidence, acuity=state.acuity_code)
        log("step=hitl escalated=%s", state.escalated)
        yield {
            "event": "agent_result", "agent": "hitl", "label": AGENT_LABELS["hitl"],
            "summary": "Escalated to clinician review." if state.escalated else "No escalation required.",
            "data": hitl_result, "durationMs": round(dur * 1000, 2),
        }
        hitl_event = await self._announce(bus, state, self.hitl, hitl_result, audit)
        if hitl_event is not None:
            yield hitl_event
        await delay()

    async def _step_reflection(self, state: CaseState, bus: MessageBus, ctx: dict,
                               audit: Callable[..., None], log: Callable[..., None],
                               delay: Callable[[], Awaitable[None]]):
        """Reflection / Critic — mandatory in every plan."""
        # ---- 6) Reflection / Critic — bounded Evaluator-Optimizer LOOP ----
        # [Loop Engineering] Explicit loop anatomy:
        #   TRIGGER   : verifier reports thin evidence / very low confidence
        #   GOAL      : an internally-consistent, appropriately-cautious decision
        #   VERIFIER  : ReflectionAgent.run() — deterministic, monotone-escalating
        #   STOP RULES: verifier converged  |  iteration cap  |  wall-clock budget
        # Each iteration can only ESCALATE, so the loop is safe at any cap.
        yield {"event": "agent_active", "agent": "reflection", "label": AGENT_LABELS["reflection"]}
        await delay()
        # NOTE for Sham (Symptom-Intake / orchestrator owner) — added 2026-09-16 by James.
        # WHY: Reflection (platform-owned) gained an LLM critic that decides this loop's
        # next edge. Like `await self.safety.areason(state)` above, it is async, so it
        # is awaited here before the synchronous run(). With the LLM unavailable it
        # returns None and this step behaves exactly as before.
        start = time.perf_counter()
        budget_s = REFLECTION_BUDGET_MS / 1000
        # The timer starts BEFORE the first critique and the critic gets the
        # remaining budget, so one reflection step can no longer spend two
        # full critiques against a few-second budget.
        await self._reason(self.reflection, state, timeout_s=budget_s)   # LLM critic (additive)
        self.deliver_inbox(bus, self.reflection)
        reflection_result, _ = await self._run_worker(state, "reflection", self.reflection)  # initial verify pass
        iterations = 0
        stop_reason = "converged"
        all_corrections: list[str] = list(reflection_result.get("corrections") or [])
        # A clarifying question is a TERMINAL state for this turn: HITL chose
        # to ask the patient instead of escalating, and the answer comes back
        # on the next request. A re-run here would cap the confidence, bump
        # the acuity and escalate the very case it just decided to ask about,
        # while the stream still showed "ask".
        asked = ctx["hitl_result"].get("action") == "ask" and state.clarification is not None
        if asked and reflection_result.get("rerun_suggested"):
            stop_reason = "clarification_pending"
        while reflection_result.get("rerun_suggested") and not asked:
            elapsed_ms = (time.perf_counter() - start) * 1000
            if iterations >= self.reflection_max_iters:
                stop_reason = "iteration_cap"
                break
            if elapsed_ms >= REFLECTION_BUDGET_MS:
                stop_reason = "budget_exhausted"
                break
            iterations += 1
            self._rerun_events = []
            all_corrections += await self._reflection_rerun(state, audit, log, bus, iteration=iterations)
            for rerun_event in self._rerun_events:
                yield rerun_event
            # [A2A] Re-deliver before re-verifying: the pass above republished
            # `care.routed`, and the attribution check compares the tier against
            # the LATEST announcement. An inbox from before the re-run would make
            # the critic judge this pass on the previous pass's conversation.
            self.deliver_inbox(bus, self.reflection)
            remaining_s = max(0.0, budget_s - (time.perf_counter() - start))
            await self._reason(self.reflection, state, timeout_s=remaining_s)  # critic re-reviews the re-run
            reflection_result, _ = await self._run_worker(state, "reflection", self.reflection)  # re-verify
            all_corrections += list(reflection_result.get("corrections") or [])
        if reflection_result.get("source") == UNAVAILABLE:
            stop_reason = "agent_unavailable"
        reflection_result["corrections"] = all_corrections
        reflection_result["reran"] = iterations > 0
        # [Loop Engineering] Surface the loop's anatomy for observability / the demo.
        reflection_result["loop"] = {
            "trigger": "thin_evidence_or_low_confidence",
            "goal": "consistent, appropriately-cautious triage decision",
            "verifier": "deterministic consistency + confidence (monotone-escalating)",
            "iterations": iterations,
            "maxIterations": self.reflection_max_iters,
            "budgetMs": REFLECTION_BUDGET_MS,
            "elapsedMs": round((time.perf_counter() - start) * 1000, 2),
            "stopReason": stop_reason,
        }
        state.reflection = reflection_result
        dur = time.perf_counter() - start
        metrics.observe_agent("reflection", reflection_result.get("source", "deterministic"), dur)
        audit(actor="reflection",
              action="passed" if reflection_result["passed"] else "revised",
              detail=f"issues={reflection_result['issues']} corrections={reflection_result['corrections']} "
                     f"reran={reflection_result.get('reran', False)}",
              confidence=state.confidence, acuity=state.acuity_code)
        log("step=reflection passed=%s corrections=%s", reflection_result["passed"], reflection_result["corrections"])
        corrections = reflection_result.get("corrections") or []
        yield {
            "event": "agent_result", "agent": "reflection", "label": AGENT_LABELS["reflection"],
            "summary": (f"Corrected: {'; '.join(corrections)}" if corrections
                        else "Decision consistent — no correction needed."),
            "data": reflection_result, "durationMs": round(dur * 1000, 2),
        }
        reflection_event = await self._announce(bus, state, self.reflection, reflection_result, audit)
        if reflection_event is not None:
            yield reflection_event
        await delay()

    async def _step_handoff(self, state: CaseState, bus: MessageBus, ctx: dict,
                            audit: Callable[..., None], log: Callable[..., None],
                            delay: Callable[[], Awaitable[None]]):
        """Clinician Handoff — mandatory in every plan, but only acts on escalated cases."""
        # ---- 7) Clinician Handoff — only for cases a human will actually see ----
        # ORDERING IS LOAD-BEARING: this runs AFTER the Reflection loop, not
        # after HITL. Reflection's severity backstop can itself set
        # `escalated = True` on a case nothing upstream — including HITL —
        # flagged. Summarising before that settles risks handing a clinician a
        # packet describing as routine a case that is about to be escalated.
        #
        # Gating on `state.escalated` also gives the HITL "ask" outcome the
        # right behaviour for free: an `action == "ask"` case leaves `escalated`
        # False, so no packet is built for a case that is still awaiting the
        # patient's answer. No extra branch needed.
        if state.escalated:
            yield {"event": "agent_active", "agent": "handoff", "label": AGENT_LABELS["handoff"]}
            await delay()
            self.deliver_inbox(bus, self.handoff)
            handoff_result, dur = await self._run_worker(state, "handoff", self.handoff)
            metrics.observe_agent("handoff", handoff_result.get("source", "fallback"), dur)
            audit(actor="handoff", action=handoff_result.get("source", "fallback"),
                  detail=f"summary_len={len(state.handoff_summary)} "
                         f"citations={len(state.handoff_citations)} "
                         f"questions={state.handoff_questions}",
                  confidence=state.confidence, acuity=state.acuity_code)
            log("step=handoff source=%s summary_len=%d",
                handoff_result.get("source"), len(state.handoff_summary))
            yield {
                "event": "agent_result", "agent": "handoff", "label": AGENT_LABELS["handoff"],
                "summary": ("Clinician Handoff unavailable — no packet; case is in the review queue."
                            if handoff_result.get("source") == UNAVAILABLE
                            else "Handoff packet ready for clinician."),
                "data": handoff_result, "durationMs": round(dur * 1000, 2),
            }
            handoff_event = await self._announce(bus, state, self.handoff, handoff_result, audit)
            if handoff_event is not None:
                yield handoff_event
            await delay()

    # ------------------------------------------------------------------
    # [Compatibility] The old private names.
    #
    # Four teammates' test files already call these as `supervisor._deliver(...)`,
    # `._publish(...)`, `._safety_request(...)`, `._verify_safety_response(...)`.
    # They were private by naming convention and public in practice, which is the
    # worst of both: no stability promise, but real code depending on them.
    #
    # The methods above are the supported names. These aliases keep every
    # existing call site working so nobody has to change anything on a deadline;
    # they can be deleted once the team has migrated.
    # ------------------------------------------------------------------
    _deliver = deliver_inbox
    _publish = publish
    _open_message = open_message
    _safety_request = safety_request
    _verify_safety_response = verify_safety_response
    _apply_safety_failsafe = apply_safety_failsafe

    def build_rationale(self, state: CaseState) -> str:
        """Explain the decision in words a patient can act on.

        Shown on the patient's result card, so it is written for them: plain
        sentences, no acuity codes standing alone, no agent names. Every claim
        still comes from `state` — this is a translation, not a softening.
        """
        enforce_tool_access(self, "aggregate")
        # LEAD WITH THE FINAL ACUITY. This used to open with
        # `prior_acuity_code or acuity_code` — the classifier's FIRST guess —
        # and only explained an upgrade when Safety caused it. When Reflection's
        # low-confidence backstop did the raising instead, the badge said P2
        # while this paragraph said P3 and never mentioned the change.
        final = state.acuity_code
        prior = state.prior_acuity_code
        raised = bool(prior and prior != final)
        # ".rstrip('.')" because normalised_symptoms usually ends in a full stop
        # already; without this the sentence reads "shortness of breath..".
        symptoms = (state.normalised_symptoms or "").strip().rstrip(".")
        evidence = _join_evidence(state.evidence)

        parts: list[str] = []
        if symptoms:
            parts.append(f"You told us: {symptoms}.")
        because = f", mainly because of {evidence}" if evidence else ""
        parts.append(f"We assessed this as {_acuity_phrase(final)}{because}.")
        confidence_pct = round(state.confidence * 100)
        if raised:
            parts.append(
                f"The classifier first read this as {_acuity_phrase(prior)} and was "
                f"{confidence_pct}% confident."
            )
        else:
            parts.append(f"The classifier was {confidence_pct}% confident.")
        # [Agentic] Episodic-memory continuity: surface the prior visit and any
        # cross-visit escalation.
        if state.prior_visit_summary:
            parts.append(f"We also saw an earlier visit on record — {state.prior_visit_summary}.")
        if state.safety_triggered:
            why = _clean_clause(state.safety_reason or "")
            # No brackets — safety_reason frequently contains its own, and
            # "A safety rule (possible ACS (chest pain pattern).)" is a mess.
            trigger = f"A safety rule — {why} — " if why else "A safety rule "
            verb = "raised this to" if raised else "also flagged this as"
            parts.append(
                f"{trigger}{verb} {_acuity_phrase(final)}. "
                f"That rule cannot be overruled by the model."
            )
        elif raised:
            # Reflection's backstop, not Safety. Saying WHY matters: the level
            # went up because the system was unsure, not because it found
            # something worse.
            parts.append(
                "Our review step raised the level as a precaution, because that "
                "first read was not confident enough to rely on."
            )
        parts.append(f"Where to go: {_destination(state.care_tier, state.clinic)}.")
        # A wait time is useful when you are choosing when to leave, and actively
        # misleading on a call-995 case, where "estimated wait 0 min" reads like
        # a queue position for something that is not a queue.
        if state.wait_time_min and not _is_emergency(state.acuity_code):
            parts.append(f"The estimated wait is about {state.wait_time_min} minutes.")
        if state.escalated:
            parts.append(self._escalation_sentence(state))
        return " ".join(parts)

    @staticmethod
    def _escalation_sentence(state: CaseState) -> str:
        """Say what clinician review means WITHOUT implying the patient waits.

        On a P1 the old wording ("routed for human review; the final decision
        rests with the clinician") invites exactly the wrong behaviour — sitting
        by the phone instead of calling 995. Review runs alongside the patient
        acting, so for P1/P2 the sentence says so outright.
        """
        reason = _plain_reason(state.escalation_reason or "")
        lead = f"This case also goes to a clinician: {reason}" if reason else (
            "This case also goes to a clinician."
        )
        if _is_emergency(state.acuity_code):
            return f"{lead} Do not wait to hear back — get help now."
        return (
            f"{lead} They may update this advice. "
            f"If anything gets worse in the meantime, treat it as an emergency."
        )

    def build_citations(self, state: CaseState) -> list[dict]:
        # [Agentic] Reuse the citations the classifier already gathered via
        # its least-privilege-gated `rag.retrieve()` call (see
        # SeverityClassifierAgent._gather_supporting_evidence) instead of
        # querying RAG a second time; falls back to computing fresh if, for
        # any reason, the classifier didn't populate state.citations, so
        # behaviour/output is unchanged from before this feature was added.
        enforce_tool_access(self, "aggregate")
        if state.citations:
            return state.citations
        query = f"{state.normalised_symptoms} {' '.join(state.evidence)}"
        return rag.retrieve(query)
