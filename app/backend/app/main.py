"""FastAPI app: CareRoute AI backend.

Wires together the guardrail layer, the Symptom-Intake orchestrator + five worker agents,
the RAG citation step, and the in-memory store, and exposes the API
contract consumed by the frontend.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from . import config, correlation, guardrail, llm, metrics, patient_language, redact
from .agents import (
    CaseState,
    SymptomIntakeAgent,
    acuity_from_code,
    enforce_tool_access,
    onemap_status,
)
from .agents import registry as agent_registry
from .agents.base import INTERVIEW_MAX_QUESTIONS
from .audit import audit_log
from .models import (
    CaseRecord,
    Citation,
    DecisionRequest,
    EscalationDetail,
    EscalationSummary,
    FairnessResponse,
    FeedbackRequest,
    HealthResponse,
    TriageRequest,
)
from .ratelimit import AbuseMonitor, RateLimiter
from .store import build_fairness_response, new_id, now_iso, store

# [MLOps] Standard Python logging alongside the structured per-case
# AuditLog: the AuditLog is queryable per-case via the API (FR-13), while
# this logger gives an operator-facing, real-time INFO trail of every
# pipeline step (e.g. for `docker logs` / log aggregation in production).
# Reuse Uvicorn's configured handler so application diagnostics appear beside
# server lifecycle messages in both local runs and container logs.
logger = logging.getLogger("uvicorn.error")


def _backend_path(configured_path: str) -> Path:
    path = Path(configured_path)
    return path if path.is_absolute() else Path(__file__).resolve().parents[1] / path


async def _configure_safety_nlp_runtime(app: FastAPI) -> None:
    """Build, warm and inject the pinned live Safety NLP adapter bundle."""
    orchestrator.safety.set_nlp_adapters(None)
    app.state.safety_nlp_runtime = None
    app.state.safety_nlp_status = "disabled"
    if not config.SAFETY_NLP_ENABLED or config.KILL_SWITCH:
        return

    try:
        from .safety_nlp import SafetyNlpRuntime

        backend_root = Path(__file__).resolve().parents[1]
        runtime = SafetyNlpRuntime.from_manifest_file(
            _backend_path(config.SAFETY_NLP_MODEL_MANIFEST),
            enabled=True,
            timeout_ms=config.SAFETY_NLP_TIMEOUT_MS,
            device=config.SAFETY_NLP_DEVICE,
            max_input_chars=config.SAFETY_NLP_MAX_INPUT_CHARS,
            cache_dir=str(_backend_path(config.SAFETY_NLP_MODEL_CACHE)),
            project_root=backend_root,
            nllb_enabled=config.SAFETY_NLP_NLLB_ENABLED,
            madlad_enabled=config.SAFETY_NLP_MADLAD_ENABLED,
            direct_nli_enabled=config.SAFETY_NLP_DIRECT_NLI_ENABLED,
        )
        await runtime.awarmup()
        orchestrator.safety.set_nlp_adapters(runtime.adapters)
        app.state.safety_nlp_runtime = runtime
        app.state.safety_nlp_status = dict(runtime.stage_statuses)
        logger.info("Safety NLP runtime warmed: stages=%s", runtime.stage_statuses)
    except Exception:  # noqa: BLE001 - optional NLP models must never block startup; deterministic Safety stays active
        app.state.safety_nlp_status = "unavailable"
        logger.warning("Safety NLP startup unavailable; deterministic Safety remains active", exc_info=False)

# [Agentic] AAS Day 3 AM slide 17: every log line gets a correlation ID without
# any call site asking for one. Installed at import so records emitted during
# startup are stamped too (as "-", which is honest: they belong to no request).
correlation.install()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # [MLOps] Pre-train/cache the severity model off the request path, in a
    # worker thread, so the first triage doesn't pay the (few-second) training
    # cost. Best-effort — if the ML deps are missing, agents fall back anyway.
    async def _warm() -> None:
        try:
            from .ml.model import get_model

            await asyncio.to_thread(get_model)
            logger.info("severity model warmed up")
        except Exception:  # noqa: BLE001 - best-effort startup/telemetry path; logged, never fatal to triage
            logger.warning("model warmup skipped (agents will fall back)", exc_info=False)

    # [Agentic][RAG] The embedder is warmed SYNCHRONOUSLY, on this thread, and
    # deliberately not inside `_warm()` above: importing onnxruntime for the
    # first time on a worker thread kills the process with an access violation
    # rather than an exception. See `rag_embed.warm`. Costs a second of startup,
    # before any request is served, and never raises.
    # [Microservices] With AGENT_TRANSPORT=http the model and the embedder live
    # in the classifier and rag-service containers; this process loads neither.
    in_process = config.AGENT_TRANSPORT != "http"
    if in_process:
        from . import rag_embed

        rag_embed.warm()

    # The reference must be held for the task's lifetime: the event loop keeps
    # only a weak reference, so a bare `create_task(...)` can be garbage
    # collected mid-flight and the warmup silently cancelled.
    warmup_task = asyncio.create_task(_warm()) if in_process else None

    # [Safety] Same in-process condition: the NLP adapters are injected into
    # THIS process's Safety agent, which only exists when the workers run here.
    # With AGENT_TRANSPORT=http the safety container warms its own runtime.
    if in_process:
        await _configure_safety_nlp_runtime(app)

    try:
        yield
    finally:
        if warmup_task is not None:
            warmup_task.cancel()


app = FastAPI(title="CareRoute AI Backend", lifespan=lifespan)

# [API contract] Error responses the handlers really return, so /openapi.json
# documents them (FastAPI only lists 200 and 422 by itself; Schemathesis fails
# every status code the spec does not declare).
_BAD_BODY = {400: {"description": "Request body is not valid JSON"}}
_NOT_FOUND = {404: {"description": "No record with that id"}}
_STAFF = {401: {"description": "Staff key missing or wrong (only when CAREROUTE_STAFF_API_KEY is set)"}}

@app.middleware("http")
async def _correlate(request: Request, call_next):
    """[Agentic] Slide 17: "stamp every request with a correlation ID; propagate
    it to all downstream spans and logs."

    An inbound `X-Correlation-ID` (or `X-Request-ID`) is honoured so a request
    keeps one identity across a front end or gateway; anything unusable is
    REPLACED rather than rejected, because failing a triage over a malformed
    debugging header would be the wrong trade. The value is sanitised before it
    is ever logged — it is attacker-controlled data heading for a log file. See
    `correlation.sanitise`.

    Echoed on the response so a caller can quote the ID in a bug report without
    having to find it in our logs.
    """
    incoming = request.headers.get(correlation.HEADER) or request.headers.get(correlation.ALT_HEADER)
    cid = correlation.sanitise(incoming) or correlation.new_correlation_id()
    token = correlation.bind(cid)
    try:
        response = await call_next(request)
    finally:
        correlation.reset(token)
    response.headers[correlation.HEADER] = cid
    # [AI-Security] ZAP API scan (2026-09-25) flagged both as missing: no MIME
    # sniffing of API output, and no embedding of it by another origin (CORP
    # governs no-cors loads only, so CORS fetches from the frontend still work).
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    return response


app.add_middleware(
    CORSMiddleware,
    # Deployment-specific, so it comes from CAREROUTE_CORS_ORIGINS (defaulting to
    # the two local dev servers). `allow_credentials=True` below means "*" is
    # both rejected by browsers and genuinely unsafe — see config.CORS_ORIGINS.
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# [Orchestrator] Symptom-Intake holds the orchestrator role: it is both the first
# worker and the agent that sequences the other five. See agents/orchestration.py.
#
# [Concurrency] This module-level instance is a TEMPLATE, not the thing that runs
# a triage. It exists to build the expensive, case-independent dependencies once
# per process — the CHAS clinic dataset, the GoWhere hours snapshot, the OneMap
# client and its auth token, the route cache, Safety's semantic layer — and to
# answer static questions about the workers (the tool-access check below).
#
# Every request gets its own worker set via `orchestrator.new_session()`, because
# the workers keep PER-CASE state on themselves (each agent's delivered inbox;
# Safety's `_last_result`, which is what its `safety.override` message is built
# from) and `orchestrate()` suspends at every yield and await. Two triages in
# flight therefore interleave, and a shared worker set let one patient's safety
# message be assembled from another patient's run — into the audit trail, the SSE
# stream and the API response.
if config.AGENT_TRANSPORT == "http":
    # [Microservices] Every other worker is its own container; this process is
    # the intake agent + orchestrator + public API. See app/microservices/.
    from .microservices.remote import remote_workers

    orchestrator = SymptomIntakeAgent(**remote_workers())
else:
    orchestrator = SymptomIntakeAgent()

# [AI-Security] LLM10 rate limiter, shared across requests (see ratelimit.py).
rate_limiter = RateLimiter(limit=config.RATE_LIMIT_PER_MIN)
abuse_monitor = AbuseMonitor(
    threshold=config.ABUSE_BLOCK_THRESHOLD,
    window_seconds=config.ABUSE_WINDOW_S,
    cooldown_seconds=config.ABUSE_COOLDOWN_S,
)

# AGENT_LABELS now lives with the orchestration in agents.py (the Supervisor
# owns SSE step-event emission), so main no longer needs its own copy.


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# SSE comment frame: no `data:` line, so every client parser skips it, but it
# is bytes on the wire and therefore resets idle timers in any proxy between
# the backend and the browser (see config.SSE_HEARTBEAT_SECONDS for why).
_SSE_KEEPALIVE = ": keepalive\n\n"


async def _with_heartbeat(source, interval: float):
    """Re-yield `source` unchanged, inserting a keepalive comment whenever the
    source has produced nothing for `interval` seconds.

    The next item is awaited in a task so we can wait on it with a timeout
    without consuming it; frames are therefore never split or reordered. An
    `interval` <= 0 disables the heartbeat and passes the source straight
    through. Source exceptions propagate unchanged.

    If the consumer stops early (the browser disconnects mid-triage, which is
    routine), teardown must be DETERMINISTIC: cancelling the pending task alone
    leaves the source generator suspended inside `__anext__`, so the
    orchestrator's own `finally` — where it releases the OneMap session and
    closes out the case — would run whenever the GC happened to finalise it,
    on an arbitrary task and possibly after the loop had closed. So the
    cancellation is awaited and the source is explicitly closed here.
    """
    if interval <= 0:
        async for item in source:
            yield item
        return

    iterator = source.__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield _SSE_KEEPALIVE
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            yield item
            pending = asyncio.ensure_future(iterator.__anext__())
    finally:
        if not pending.done():
            pending.cancel()
        # Await the cancellation so the task is actually finished (and its
        # result retrieved) before we return; an abandoned cancelled task
        # otherwise resurfaces later as "Task exception was never retrieved".
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pending
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


async def _step_delay(low: float = 0.5, high: float = 0.9) -> None:
    # Small pause between agent steps so the frontend can animate the pipeline.
    import random

    # UI pacing jitter, not a security context — a CSPRNG buys nothing here.
    # nosemgrep: bandit.B311
    await asyncio.sleep(random.uniform(low, high))  # noqa: S311  # nosec B311


async def _triage_event_stream(req: TriageRequest, client_key: str = "unknown"):
    case_id = new_id("case")
    # [Agentic] Bind the BUSINESS identifier next to the request one, so the
    # workers' own log lines — llm.py, the tool gateway, the guardrail — carry
    # the case without any of them being handed it. Not reset: this generator
    # outlives the middleware's scope (the response streams after `call_next`
    # returns), and it runs in its own task, so the binding dies with the task.
    correlation.bind_case(case_id)
    # Every LLM call made while serving this request (any agent) is logged here and
    # returned on the final event as `llm`: task, tier (fast/deep/max), model, latency.
    llm_calls = llm.begin_call_log()

    yield _sse({"event": "case_open", "caseId": case_id, "ts": now_iso()})
    await _step_delay()

    # --- Guardrail (runs before any agent), screens the ORIGINAL text and
    # every interview answer the request carries. An answer is patient text
    # arriving at the same door as the complaint and is vetted on the same
    # footing; the first blocked one blocks the turn.
    guard_result = guardrail.screen(req.text)
    for item in req.clarifications:
        if guard_result.status == "blocked":
            break
        guard_result = guardrail.screen(item.answer)
    # [MLOps][AI-Security] FR-13: every pipeline decision -- including a
    # guardrail block -- is recorded to the per-case audit trail AND logged,
    # so a blocked request still leaves a reviewable trace.
    audit_log.record(case_id, actor="guardrail", action=guard_result.status, detail=guard_result.detail)
    logger.info("case=%s step=guardrail status=%s", case_id, guard_result.status)
    yield _sse({"event": "guardrail", "status": guard_result.status, "detail": guard_result.detail})
    if guard_result.status == "blocked":
        metrics.inc(metrics.GUARDRAIL_BLOCKS)
        metrics.inc(metrics.TRIAGE_REQUESTS, outcome="blocked")
        if guard_result.category in ("injection", "structure") and abuse_monitor.record_block(client_key):
            # The client address is personal data; the audit records a stable,
            # non-reversible fingerprint of it instead.
            fingerprint = hashlib.sha256(client_key.encode("utf-8")).hexdigest()[:12]
            audit_log.record(
                case_id, actor="abuse_monitor", action="quarantined",
                detail=(f"client {fingerprint} reached {abuse_monitor.threshold} attack-category "
                        f"guardrail blocks; refused for {int(abuse_monitor.cooldown)} s"),
            )
            metrics.inc(metrics.ABUSE_EVENTS, kind="quarantined")
            logger.warning("case=%s step=abuse_monitor quarantined client=%s", case_id, fingerprint)
        yield _sse({"event": "error", "message": guard_result.detail})
        return
    await _step_delay()

    # --- PII/PHI redaction (LLM02): mask identifiers BEFORE the text reaches
    # any LLM provider or is persisted. Clinical symptom words are untouched. ---
    masked_text, pii_found = redact.redact(req.text)
    # The interview transcript gets the same masking: an answer is free text a
    # patient typed, and it is sent to the LLM and read by clinicians like the
    # complaint is.
    clarifications: list[dict] = []
    for item in req.clarifications:
        masked_answer, answer_pii = redact.redact(item.answer)
        pii_found = list(dict.fromkeys([*pii_found, *answer_pii])) if answer_pii else pii_found
        clarifications.append({**item.model_dump(), "answer": masked_answer})
    if pii_found:
        audit_log.record(
            case_id, actor="redaction", action="masked",
            detail=f"Masked identifier types: {pii_found}",
        )
        logger.info("case=%s step=redaction masked=%s", case_id, pii_found)

    state = CaseState(
        case_id=case_id,
        raw_text=masked_text, language=req.language or "en", is_voice=bool(req.isVoice),
        age_band=req.ageBand, sex=req.sex, latitude=req.latitude, longitude=req.longitude,
        transport_mode=req.transportMode, max_travel_time_min=req.maxTravelTimeMin,
        emergency_self_transport_confirmed=req.emergencySelfTransportConfirmed,
        preferred_clinic_id=req.preferredClinicId,
        accessibility_need=req.accessibilityNeed, preferred_language=req.preferredLanguage,
        affordability_preference=req.affordabilityPreference,
        clarifications=clarifications,
    )

    # [Agentic][AI-Security] Episodic-memory recall: thread a compact,
    # TTL-bounded, session-scoped prior-visit summary into the CaseState so the
    # Supervisor's cross-visit rule can reason about continuity. `recall_session`
    # rejects spoofed/expired sessions, so this is safe against ASI06 poisoning.
    if req.sessionId:
        _recall_prior_visit(state, case_id=case_id, session_id=req.sessionId)

    # [Agentic] Orchestration lives in the Supervisor now: it drives the five
    # workers (+ Reflection critic) in the fixed, safety-gated order and yields
    # SSE-ready step events. We inject the audit/log/delay side-effects as
    # callbacks so the orchestration stays decoupled from this HTTP/SSE
    # transport, and simply wrap each yielded event with `_sse`.
    def _audit(actor: str, action: str, detail: str, **kw) -> None:
        audit_log.record(case_id, actor=actor, action=action, detail=detail, **kw)

    def _log(msg: str, *args) -> None:
        logger.info("case=%s " + msg, case_id, *args)

    async def _delay() -> None:
        await _step_delay()

    # [Concurrency] This request's OWN worker set. The expensive dependencies
    # come down from the module-level template; only the agents that hold
    # per-case state are rebuilt. See PipelineOrchestrator.new_session.
    session = orchestrator.new_session()

    # Wrapped in a heartbeat so a quiet agent (Care Routing on OneMap, the
    # red-flag path on the LLM) cannot let the Next proxy's 30 s idle timeout
    # silently kill the stream. Keepalive frames are comments; `_sse` frames
    # pass through untouched and in order.
    orchestration = session.orchestrate(state, audit=_audit, log=_log, delay=_delay)
    async for event in _with_heartbeat(orchestration, config.SSE_HEARTBEAT_SECONDS):
        yield event if event is _SSE_KEEPALIVE else _sse(event)

    # --- Aggregate final response ---
    rationale = session.build_rationale(state)

    # [AI-Security] LLM05 output guardrail: screen the (LLM-influenced) rationale
    # before it is shown to a clinician; suppress it if it leaks a prompt or
    # injected instruction. "Never trust LLM output blindly."
    out_guard = guardrail.screen_output(rationale)
    if out_guard.status == "flagged":
        metrics.inc(metrics.OUTPUT_FLAGS)
        audit_log.record(case_id, actor="output_guardrail", action="flagged", detail=out_guard.detail)
        logger.warning("case=%s step=output_guardrail flagged", case_id)
        rationale = guardrail._SAFE_OUTPUT_FALLBACK
        yield _sse({"event": "output_guardrail", "status": "flagged", "detail": out_guard.detail})

    # [AI-Security] LLM05, again: the Clinician-Handoff summary is LLM-authored
    # and goes straight to a clinician making a time-pressured decision, so it
    # gets the same screening as the rationale above. handoff.py deliberately
    # does not screen its own output — that is this file's job.
    handoff_summary = state.handoff_summary
    if handoff_summary:
        handoff_guard = guardrail.screen_output(handoff_summary)
        if handoff_guard.status == "flagged":
            metrics.inc(metrics.OUTPUT_FLAGS)
            audit_log.record(case_id, actor="output_guardrail", action="flagged",
                             detail=handoff_guard.detail)
            logger.warning("case=%s step=output_guardrail flagged field=handoffSummary", case_id)
            handoff_summary = guardrail._SAFE_OUTPUT_FALLBACK
            yield _sse({"event": "output_guardrail", "status": "flagged",
                        "detail": handoff_guard.detail})

    citations_raw = await asyncio.to_thread(session.build_citations, state)
    citations = [Citation(**c) for c in citations_raw]
    acuity = acuity_from_code(state.acuity_code)

    # [MLOps] FR-13: log the final aggregation step itself, so the audit
    # trail's last entry is always the orchestrator's aggregated decision.
    audit_log.record(
        case_id, actor="intake", action="aggregate",
        detail=f"rationale={rationale}", confidence=state.confidence, acuity=state.acuity_code,
    )
    logger.info("case=%s step=aggregate acuity=%s escalated=%s asked=%s",
                case_id, state.acuity_code, state.escalated, state.clarification_asked)

    # [Safety] The shadow Safety-NLP pass leaves only an in-memory summary, so its
    # behaviour in production was invisible (timeouts included). The summary is
    # aggregate and text-free (safety_nlp/telemetry.py): counts, statuses and
    # per-stage latency, safe for the audit trail and the final event.
    safety_nlp_summary = (state.safety_nlp_telemetry or {}).get("summary") or None
    if safety_nlp_summary:
        audit_log.record(case_id, actor="safety", action="nlp_shadow",
                         detail=json.dumps(safety_nlp_summary, sort_keys=True))

    # The patient reads the question/rationale in the language they wrote in,
    # with the English kept alongside (app/patient_language.py). {} for English.
    asked_q = (state.clarification or {}).get("question") if state.clarification_asked else None
    translations = await patient_language.translate({
        "question": asked_q,
        "rationale": None if state.clarification_asked else rationale,
        "routingReason": None if state.clarification_asked else state.routing_reason,
        "routingQuestion": None if state.clarification_asked else (state.routing_clarification or {}).get("question"),
    }, state.detected_language)

    # [Agentic] An interview turn is a QUESTION, not a decision: nothing is
    # persisted, no escalation is opened and it is not a completed triage. The
    # final turn — the one that decides — does all of that, exactly as before.
    interview = {
        "round": len(state.clarifications),
        "budget": INTERVIEW_MAX_QUESTIONS,
        "done": not state.clarification_asked,
    }
    if state.clarification_asked:
        metrics.inc(metrics.TRIAGE_REQUESTS, outcome="asked")
        yield _sse({
            "event": "final",
            "caseId": case_id,
            "acuity": acuity.model_dump(),
            "careTier": state.care_tier,
            "confidence": state.confidence,
            "escalated": False,
            "escalationReason": None,
            "rationale": rationale,
            # The guidance that grounded the PROVISIONAL acuity — retrieved by the
            # classifier this turn — so even a question is never ungrounded.
            "citations": [c.model_dump() for c in citations],
            "clarification": state.clarification,
            "interview": interview,
            "language": state.detected_language,
            "translations": translations,
            "safetyNlp": safety_nlp_summary,
            "llm": llm_calls,
            "explanation": state.explanation,
            "explanationSource": state.explanation_source or None,
            "evidence": state.evidence,
            "normalisedSymptoms": state.normalised_symptoms,
        })
        return

    case_record = CaseRecord(
        caseId=case_id,
        sessionId=req.sessionId,
        # [AI-Security] LLM02: the MASKED text, never `req.text`. redact.py's
        # contract is that identifiers are gone before the text is both sent to
        # a provider AND persisted — only the first half was true, so the store
        # (read by the history endpoint, the escalation queue and the clinician
        # handoff) held unmasked NRICs and phone numbers. This is also the text
        # the agents actually reasoned over, so the record is more faithful.
        rawText=masked_text,
        language=state.detected_language,
        isVoice=state.is_voice,
        normalisedSymptoms=state.normalised_symptoms,
        acuity=acuity,
        confidence=state.confidence,
        careTier=state.care_tier,
        clinic=state.clinic,
        waitTimeMin=state.wait_time_min,
        travelEstimateSource=state.travel_estimate_source,
        routeInstructions=state.route_instructions,
        routeAvailable=state.route_available,
        escalated=state.escalated,
        rationale=rationale,
        citations=citations,
        evidence=state.evidence,
        safetyTriggered=state.safety_triggered,
        safetyRule=state.safety_rule,
        createdAt=now_iso(),
        explanation=state.explanation,
        explanationSource=state.explanation_source or None,
        reflection=state.reflection,
        redactedPii=pii_found,
        handoffSummary=handoff_summary,
        handoffCitations=[Citation(**c) for c in state.handoff_citations],
        handoffQuestions=state.handoff_questions,
    )
    store.save_case(case_record)

    if state.escalated:
        # [AI-Security] FR-12 least-privilege enforcement point: only the
        # Human-in-the-Loop worker's declared "escalation.create" tool may
        # actually create an escalation record.
        enforce_tool_access(session.hitl, "escalation.create")
        store.create_escalation_from_case(case_record, state.escalation_reason or "Escalated for review.")
        metrics.inc(metrics.ESCALATIONS)

    metrics.inc(metrics.TRIAGE_REQUESTS, outcome="escalated" if state.escalated else "completed")
    # [MLOps] Lecture 03 availability: this case was SERVED (yield), and this is
    # how much of it was actually there (harvest). Every degradation in this app
    # is caught and answered around — which means a degraded answer has, until
    # now, been counted as a success indistinguishable from a whole one.
    harvest_score = metrics.observe_harvest(state)
    if harvest_score is not None and harvest_score < 1.0:
        logger.info("case=%s step=harvest score=%.2f", case_id, harvest_score)

    yield _sse({
        "event": "final",
        "caseId": case_id,
        "acuity": acuity.model_dump(),
        "careTier": state.care_tier,
        "confidence": state.confidence,
        "escalated": state.escalated,
        # [Responsible-AI] WHY the case was escalated, not just that it was. A
        # clinician opening the review queue triages on this string, and the E5
        # evaluation asserts every escalation carries one — an unattributable
        # escalation cannot be reviewed or audited back to the rule that fired.
        "escalationReason": state.escalation_reason,
        "rationale": rationale,
        "citations": [c.model_dump() for c in citations],
        "waitTimeMin": state.wait_time_min,
        "clinic": state.clinic,
        "travelEstimateSource": state.travel_estimate_source,
        "routeInstructions": state.route_instructions,
        "routeAvailable": state.route_available,
        "routeGeometry": state.route_geometry,
        "transportMode": state.effective_transport_mode or state.transport_mode,
        "requestedTransportMode": state.transport_mode,
        "routingReason": state.routing_reason,
        "routingClarification": state.routing_clarification,
        # On a DECIDED turn there is no open question: the classifier may still
        # have proposed one that HITL declined, and that must not render as a
        # question next to a finished recommendation. Asked turns return above.
        "clarification": None,
        "interview": interview,
        "language": state.detected_language,
        "translations": translations,
        "safetyNlp": safety_nlp_summary,
        "llm": llm_calls,
        "routingPlan": state.routing_plan,
        "alternativeClinics": state.alternative_clinics,
        "clinicLatitude": state.clinic_latitude,
        "clinicLongitude": state.clinic_longitude,
        "oneMapUrl": (
            f"https://www.onemap.gov.sg/?lat={state.clinic_latitude:.6f}&lng={state.clinic_longitude:.6f}&zoom=17"
            if state.clinic_latitude is not None and state.clinic_longitude is not None else None
        ),
        # [Responsible-AI] Explainability: signed feature contributions
        # behind the acuity decision (see SeverityClassifierAgent.explain()).
        "explanation": state.explanation,
        # [Responsible-AI] Provenance of those contributions — "shap", "llm" or
        # "keyword" — so the UI can label them honestly (see CaseState).
        "explanationSource": state.explanation_source or None,
        # [XRAI] Counterfactual ("would move to P2 if ... were also reported") —
        # the why-not / how-to-be-that explanation for the patient.
        "counterfactual": state.counterfactual,
        # [Agentic] Reflection/Critic verdict; [AI-Security] any masked PII kinds.
        "reflection": state.reflection,
        "redactedPii": pii_found,
        # [Agentic][A2A] The full ordered agent-to-agent conversation for this case.
        "messages": state.messages,
        # [Agentic] Plan-and-Execute: the validated per-case plan the
        # orchestrator ran (also streamed as the `plan` event and audited).
        "plan": session.plan,
    })


@app.get("/api/agents")
async def agents_discovery() -> dict:
    """[Agentic] Agent Registry discovery (AAS Day 3 AM slide 18).

    Serves each worker's own `AgentCapability` — what it reasons about, the
    outcomes it may choose between, what it remembers, which tools it uses, and
    whether it is an AGENT or a POLICY_NODE. Exposed for the same reason the
    routing table is on `/api/health`: "we have seven agents and here is what
    each may decide" should be checkable at runtime, not taken from a diagram.

    Read-only, and deliberately carries no endpoint or transport: these are
    in-process pipeline stages, not addressable services. See
    `agents/registry.py` for why both of slide 18's exposure modes are declined.
    """
    return {"agents": agent_registry.discover(), "summary": agent_registry.summary()}


@app.get("/api/agents/cards")
async def agent_cards() -> dict:
    """[Agentic][A2A] Every agent's A2A-style Agent Card — skills with input/output
    JSON schema, version, endpoint/transport, auth — generated from its declared
    capability, contract and comms. The per-container equivalent is
    GET /.well-known/agent.json on each agent service. See agents/registry.py."""
    return {"cards": agent_registry.agent_cards()}


def safety_nlp_mode() -> str:
    """[Safety] "model" | "rules" | "remote" (http transport: the safety container's own)."""
    if config.AGENT_TRANSPORT == "http":
        return "remote"
    from .safety_nlp.runtime import active_backend

    return active_backend(getattr(app.state, "safety_nlp_runtime", None))


def retrieval_mode() -> str:
    """[RAG] "hybrid" | "tfidf" | "remote" (http transport: rag-service's own)."""
    if config.AGENT_TRANSPORT == "http":
        return "remote"
    from . import rag_embed

    return rag_embed.retrieval_mode()


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    # Whether the LLM (the hosted OpenAI API, the only provider) is available;
    # `model` names it (e.g. "openai:gpt-4o-mini") or "rules" when the
    # deterministic fallback is in effect. The field was called `ollama` until
    # the local providers were removed.
    status = await llm.health()
    agents: dict[str, dict] = {}
    if config.AGENT_TRANSPORT == "http":
        from .microservices.remote import agent_health

        agents = agent_health()
    return HealthResponse(
        status="ok",
        llm=status["available"],
        model=status["active"] or "rules",
        tiers=llm.tier_models(status["active"]),
        killSwitch=config.KILL_SWITCH,
        rateLimitPerMin=config.RATE_LIMIT_PER_MIN,
        metrics=metrics.enabled(),
        breakers=status.get("breakers", {}),
        agents=agents,
        staffAuth="api-key" if config.STAFF_API_KEY else "open",
        oneMap=onemap_status(),
        safetyNlp=safety_nlp_mode(),
        retrieval=retrieval_mode(),
    )


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> Response:
    """[MLOps] Prometheus scrape endpoint (inference latency, request/guardrail/
    escalation counters). Reports 'disabled' text if prometheus_client is absent."""
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)


def _peer_is_trusted_proxy(host: str | None) -> bool:
    """True only if the direct peer is one of config.TRUSTED_PROXIES."""
    if not host or not config.TRUSTED_PROXIES:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # A non-IP peer (a unix socket, a test transport) can never be a
        # configured proxy, so it fails closed.
        return False
    return any(addr in network for network in config.TRUSTED_PROXIES)


def _client_key(request: Request) -> str:
    """Rate-limit key: the client IP, honouring X-Forwarded-For ONLY from a
    trusted proxy.

    [AI-Security] LLM10: X-Forwarded-For is an ordinary request header that any
    caller can set — and vary per request. Trusting it unconditionally gave every
    request its own bucket, so the rate limiter was bypassable with a single
    extra header. It is only meaningful when the request genuinely arrived
    through a proxy we operate, hence the peer check; with no trusted proxies
    configured (the default) the direct peer address is always used.
    """
    peer = request.client.host if request.client else None
    if _peer_is_trusted_proxy(peer):
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            # Left-most entry is the original client, appended by our own proxy.
            client = fwd.split(",")[0].strip()
            if client:
                return client
    return peer or "unknown"


async def _counted_stream(stream):
    """[MLOps] Count the triage requests that FAIL, so yield can be computed.

    `careroute_triage_requests_total` only ever recorded blocked / completed /
    escalated — three flavours of success. Yield derived from it was 1.0 by
    construction: a run that raised half way through simply never reached a
    counter, and vanished from the denominator along with the numerator. The
    "availability metric" would then have been a constant that could not report
    the outage it exists to report.

    `CancelledError` is deliberately not caught: it is a BaseException, and a
    patient closing the tab is not a service failure.
    """
    try:
        async for chunk in stream:
            yield chunk
    except Exception:
        metrics.inc(metrics.TRIAGE_REQUESTS, outcome="error")
        logger.exception("triage stream failed")
        raise


@app.post(
    "/api/triage/stream",
    response_class=StreamingResponse,
    responses={
        200: {"description": "Server-sent events, one JSON object per `data:` line",
              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
        **_BAD_BODY,
        429: {"description": "Rate limited, or the client is quarantined for repeated injection attempts"},
    },
)
async def triage_stream(req: TriageRequest, request: Request):
    client_key = _client_key(request)
    # [AI-Security] Behavioural anomaly control: a client quarantined for
    # repeated injection attempts is refused before any work runs.
    if abuse_monitor.is_blocked(client_key):
        metrics.inc(metrics.ABUSE_EVENTS, kind="refused")
        raise HTTPException(
            status_code=429,
            detail="Temporarily blocked after repeated unsafe requests — please try again later.",
        )
    # [AI-Security] LLM10: reject over-limit callers BEFORE any expensive work.
    if not rate_limiter.allow(client_key):
        metrics.inc(metrics.RATE_LIMITED)
        raise HTTPException(status_code=429, detail="Rate limit exceeded — please wait and retry.")
    return StreamingResponse(
        _counted_stream(_triage_event_stream(req, client_key)),
        media_type="text/event-stream",
        headers={
            # `no-transform` is what keeps this a STREAM rather than a download.
            #
            # The frontend reaches this endpoint through the Next.js server's
            # /api rewrite, and Next compresses proxied responses by default.
            # Compressing an SSE body BUFFERS it: measured from inside Chrome,
            # the browser saw `content-encoding: gzip` and then exactly ONE chunk
            # containing the whole response, delivered only when the run
            # finished -- no incremental events at all, so the pipeline UI sat on
            # "Triaging..." for the entire run. Served directly from :8000 the
            # same response is `transfer-encoding: chunked` with no encoding, and
            # streams correctly.
            #
            # `no-transform` (RFC 9111 §5.2.2.6) tells any intermediary it must
            # not re-encode the payload, and Next's compression layer honours it
            # -- so static assets keep their gzip and only this stream opts out.
            # X-Accel-Buffering covers the same hazard for nginx-style proxies.
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _recall_prior_visit(state: CaseState, *, case_id: str, session_id: str) -> None:
    """[Agentic][AI-Security] Episodic-memory recall, SCREENED.

    Threads a compact, TTL-bounded, session-scoped prior-visit summary into the
    CaseState so the Supervisor's cross-visit rule can reason about continuity.
    `recall_session` rejects spoofed/expired sessions (ASI06 digest check), but
    the recalled TEXT is still an indirect-injection channel: it was produced
    by an earlier run (intake normalisation is LLM-assisted) and it lands
    verbatim in the handoff prompt and the clinician rationale. So the free
    text is sanitised and run through the LLM05 output screen; if it is
    flagged, only the validated acuity enum is kept and the drop is audited.
    Best-effort throughout — never fatal to triage.
    """
    try:
        prior_cases = store.recall_session(session_id, limit=5)
    except Exception:  # noqa: BLE001 - best-effort startup/telemetry path; logged, never fatal to triage
        prior_cases = []
    if not prior_cases:
        return
    last = prior_cases[0]  # most recent first
    state.prior_visit_acuity = last.acuity.code
    symptoms = guardrail.sanitize_untrusted(last.normalisedSymptoms)
    keywords = [guardrail.sanitize_untrusted(k, max_chars=80) for k in (last.evidence or [])]
    verdict = guardrail.screen_output(" ".join([symptoms, *keywords]))
    if verdict.status != "pass":
        metrics.inc(metrics.UNTRUSTED_CONTENT_DROPS, channel="memory")
        audit_log.record(
            case_id, actor="memory", action="flagged",
            detail=f"prior-visit text dropped by untrusted-content screen: {verdict.detail}",
        )
        logger.warning("case=%s step=memory prior-visit text flagged and dropped", case_id)
    else:
        state.prior_visit_keywords = [k for k in keywords if k]
        state.prior_visit_summary = (
            f"{last.createdAt[:10]}: {symptoms} "
            f"(assessed {last.acuity.code}{', escalated' if last.escalated else ''})"
        )
    audit_log.record(
        case_id, actor="memory", action="recall",
        detail=f"prior_cases={len(prior_cases)} last_acuity={last.acuity.code}",
    )
    logger.info("case=%s step=memory recalled=%d", case_id, len(prior_cases))


@app.post("/api/cases/{case_id}/feedback", responses={**_BAD_BODY, **_NOT_FOUND})
async def submit_feedback(case_id: str, body: FeedbackRequest):
    """[Responsible-AI] PDPC Stakeholder-Interaction pillar: record a patient's
    rating / challenge of an AI decision into the case's (hash-chained) audit
    trail, so feedback is itself traceable. Unknown case_id -> 404."""
    if not audit_log.has_case(case_id):
        raise HTTPException(status_code=404, detail="No case found for that id")
    # [AI-Security] LLM02: free text a patient types here is persisted in the
    # audit trail, so it gets the same identifier masking as the symptom text.
    comment, _ = redact.redact((body.comment or "").strip()[:500])
    audit_log.record(
        case_id, actor="patient_feedback",
        action="helpful" if body.helpful else ("not_helpful" if body.helpful is False else "comment"),
        detail=comment or "(no comment)",
    )
    return {"ok": True}


def _require_staff(request: Request) -> None:
    """[AI-Security] LLM02 inference-time access control for the STAFF surfaces.

    Enforced only when `CAREROUTE_STAFF_API_KEY` is configured; otherwise the
    endpoints stay open and /api/health says so (`staffAuth: "open"`). The
    compare is constant-time so the key cannot be recovered byte-by-byte from
    response timing. `config.STAFF_API_KEY` is read per request (not captured at
    import) so tests and operators can flip it without a restart.
    """
    expected = config.STAFF_API_KEY
    if not expected:
        return
    supplied = request.headers.get("X-Staff-Key", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(
            status_code=401,
            detail="This is a staff endpoint. Send a valid X-Staff-Key header.",
        )


# NOTE for Heriz (HITL / clinician handoff owner) and Marcus (routing) — added
# 2026-09-15 by James: the four staff endpoints below now carry
# `dependencies=[Depends(_require_staff)]`. With no key configured (local dev,
# CI, the Playwright suite) nothing changes. The frontend sends the key from a
# server-side Route Handler (frontend/app/api/escalations/[[...path]]/route.js),
# never from the browser. Your handlers' bodies are untouched.
@app.get(
    "/api/escalations",
    response_model=list[EscalationSummary],
    dependencies=[Depends(_require_staff)],
    responses=_STAFF,
)
async def list_escalations() -> list[EscalationSummary]:
    return [
        EscalationSummary(
            id=e.id, caseId=e.caseId, reason=e.reason, confidence=e.confidence,
            acuity=e.acuity, createdAt=e.createdAt, status=e.status, patientSummary=e.patientSummary,
            slaDueAt=e.slaDueAt, slaBreached=e.slaBreached,
        )
        for e in store.list_escalations()
    ]


@app.get(
    "/api/escalations/{escalation_id}",
    response_model=EscalationDetail,
    dependencies=[Depends(_require_staff)],
    responses={**_STAFF, **_NOT_FOUND},
)
async def get_escalation(escalation_id: str) -> EscalationDetail:
    escalation = store.get_escalation(escalation_id)
    if escalation is None:
        raise HTTPException(status_code=404, detail="Escalation not found")
    return escalation


@app.post(
    "/api/escalations/{escalation_id}/decision",
    dependencies=[Depends(_require_staff)],
    responses={**_BAD_BODY, **_STAFF, **_NOT_FOUND},
)
async def decide_escalation(escalation_id: str, body: DecisionRequest):
    # [MLOps][HITL] `finalAcuity` (optional) is the clinician's ground-truth
    # label — recorded against the model's prediction (see store.py).
    escalation = store.decide_escalation(
        escalation_id, body.decision, body.note, body.clinician, body.finalAcuity
    )
    if escalation is None:
        raise HTTPException(status_code=404, detail="Escalation not found")
    return {"ok": True, "escalation": escalation}


@app.get("/api/cases/{case_id}/status", responses=_NOT_FOUND)
async def case_review_status(case_id: str):
    """[HITL] Patient-facing review status: pending/decided + SLA, and 995
    safety-netting advice while a P1/P2 review is overdue. Not staff-gated (the
    patient holds only their own unguessable caseId) and carries no clinician
    note or decision detail."""
    status = store.patient_status(case_id)
    if status is None:
        raise HTTPException(status_code=404, detail="No case found for that id")
    return status


@app.get("/api/sessions/{session_id}/history", dependencies=[Depends(_require_staff)], responses=_STAFF)
async def session_history(session_id: str):
    """[Agentic] Episodic-memory recall: prior cases for a patient session
    (most recent first), giving continuity across visits."""
    cases = store.recall_session(session_id)
    return {
        "sessionId": session_id,
        "count": len(cases),
        "cases": [
            {
                "caseId": c.caseId,
                "createdAt": c.createdAt,
                "acuity": c.acuity.model_dump(),
                "careTier": c.careTier,
                "normalisedSymptoms": c.normalisedSymptoms,
                "escalated": c.escalated,
            }
            for c in cases
        ],
    }


@app.get("/api/tools")
async def tools_catalogue():
    """[Agentic] The central tool registry: every tool an agent can use, with its
    description, argument schema, allowed agents and executor. Read-only
    metadata — no tool is executed by this endpoint."""
    from .tools import registry

    return {"tools": registry.catalog()}


@app.get("/api/fairness", response_model=FairnessResponse)
async def fairness() -> FairnessResponse:
    # [Responsible-AI] Return the REAL fairness/drift audit computed from the
    # trained severity model (per-subgroup accuracy, before/after fairness gap,
    # red-flag recall, PSI drift). If the ML layer is unavailable, fall back to
    # the seeded snapshot so the dashboard still renders.
    try:
        from .ml.model import get_model

        return FairnessResponse(**get_model().fairness())
    except Exception:  # noqa: BLE001 - best-effort startup/telemetry path; logged, never fatal to triage
        logger.warning("fairness: ML audit unavailable, using seeded snapshot", exc_info=False)
        return build_fairness_response()


@app.get(
    "/api/cases/{case_id}/audit",
    dependencies=[Depends(_require_staff)],
    responses={**_STAFF, **_NOT_FOUND},
)
async def get_case_audit(case_id: str):
    """[MLOps] FR-13 traceability endpoint: return the ordered audit trail
    (every agent's reasoning/tool evidence/confidence/decision) for a case.

    A case_id gets an audit trail as soon as it enters `_triage_event_stream`
    (the guardrail step records first), so this covers both fully-processed
    cases AND cases blocked at the guardrail. Unknown case_id -> 404.
    """
    entries = audit_log.for_case(case_id)
    if not entries:
        raise HTTPException(status_code=404, detail="No audit trail found for that case id")
    # [AI-Security] Report whether the SHA-256 hash chain is intact (tamper-evident).
    return {"caseId": case_id, "entries": entries, "verified": audit_log.verify_chain(case_id)}
