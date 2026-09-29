---
tags: [handoff, active, careroute]
updated: 2026-08-25
---
# Clinician Handoff Pipeline Integration — for James

Back to [[Home]]. Related: [[Clinician Handoff UI]] · [[Clarification Resume API Handoff]] ·
[[Evaluation Plan]] · [[Agent Capability Audit]] · [[Changelog]]

**From:** Heriz (handoff.py owner) → **To:** James (platform: `base.py`, `supervisor.py`,
`models.py`, `store.py`, `main.py`, `agents/__init__.py`, `tests/agents/harness.py`)
**Status:** `handoff.py` is built, unit-tested, and now also scored against its own E7
faithfulness metrics on the real LLM path. It is not wired into the pipeline — I have not touched
any of the files above, same rule as [[Clarification Resume API Handoff]].

---

## 1. What is already true (verified, not assumed)

`backend/app/agents/handoff.py::ClinicianHandoffAgent` exists, follows the same
`TOOL_ALLOWLIST` / `CAPABILITY` / `CONTRACT` / `COMMS` shape every other worker declares, and:

- No-ops cheaply when `state.escalated` is `False` (the common case) — verified by
  `test_handoff_is_a_noop_when_not_escalated`.
- Produces a bounded, faithfulness-only summary (LLM primary, deterministic-template fallback)
  when `state.escalated` is `True`, reading `state.handoff_summary` / `_citations` / `_questions`
  as plain attributes today, because `CaseState` doesn't declare them (see §3.1).
- Grounds via `rag.retrieve()` only when a safety rule actually fired
  (`test_handoff_grounds_only_when_a_safety_rule_fired`); skips retrieval for a confidence- or
  cross-visit-only escalation.
- `emit()` publishes `handoff.ready`; `COMMS.subscribes` covers `safety.override` / `care.routed`.

It is **not** imported by `agents/__init__.py`, **not** instantiated by `Supervisor`, and does not
run as part of `/api/triage/stream`. `tests/agents/test_handoff.py` imports it directly from
`app.agents.handoff` (bypassing the shared package) for exactly that reason — that's my own test
file, not shared scaffolding, so no action needed there on your side.

## 2. Where it plugs in, and why there

It must run **after the Reflection loop finishes**, not after HITL. Reflection's severity backstop
(`reflection.py`, `_SEVERE_CODES` check) can itself flip `state.escalated = True` on a case nothing
upstream — including HITL — flagged. Summarising before that check runs risks describing a
soon-to-be-escalated case as routine. It must **not** run when HITL's outcome is `"ask"`
(`state.escalated` stays `False` on that path per [[Clarification Resume API Handoff]]) — the
agent's existing `if not state.escalated` guard already does the right thing there for free, no
extra branch needed.

So: last step in `Supervisor.orchestrate()`, gated on `state.escalated`, after the Reflection
`yield` block and before the generator returns.

## 3. What's needed, concretely

### 3.1 `base.py` — declare the three fields on `CaseState`
Currently plain attributes (works at runtime because `CaseState` has no `__slots__`, but the
contract harness can't police them). Add near the other worker-output fields:
```python
handoff_summary: str = ""
handoff_citations: list[dict] = field(default_factory=list)
handoff_questions: list[str] = field(default_factory=list)
```
Add `"handoff": "Clinician Handoff"` to `AGENT_LABELS`.

### 3.2 `agents/__init__.py` — export it
```python
from .handoff import ClinicianHandoffAgent
```
...and add `"ClinicianHandoffAgent"` to `__all__`, same as the other five workers.

### 3.3 `supervisor.py` — instantiate + orchestrate
```python
self.handoff = ClinicianHandoffAgent()   # in __init__, alongside self.reflection
```
New step at the end of `orchestrate()`, after the Reflection block, gated on `state.escalated`:
```python
if state.escalated:
    yield {"event": "agent_active", "agent": "handoff", "label": AGENT_LABELS["handoff"]}
    await delay()
    self._deliver(bus, self.handoff)
    handoff_result, dur = await self._timed_async(self.handoff.run(state))
    metrics.observe_agent("handoff", handoff_result.get("source", "fallback"), dur)
    audit(actor="handoff", action=handoff_result["source"],
          detail=f"summary_len={len(state.handoff_summary)} questions={state.handoff_questions}")
    log("step=handoff source=%s", handoff_result.get("source"))
    yield {
        "event": "agent_result", "agent": "handoff", "label": AGENT_LABELS["handoff"],
        "summary": "Handoff packet ready for clinician.",
        "data": handoff_result, "durationMs": round(dur * 1000, 2),
    }
    yield self._publish(bus, state, self.handoff.emit(state), audit)
    await delay()
```
`self._deliver(bus, self.handoff)` before `run()` is what lets it consume the `safety.override` /
`care.routed` messages already on the bus by this point, same pattern as every other worker.

### 3.4 `models.py` — carry the fields through the API
Add to `CaseRecord` (same idiom already used for `explanation` / `reflection` — built from
`state.*` in `main.py`):
```python
handoffSummary: str = ""
handoffCitations: list[Citation] = Field(default_factory=list)
handoffQuestions: list[str] = Field(default_factory=list)
```
Add the same three to `EscalationDetail` — this is the field set
[[Clinician Handoff UI]] is already coded against and blocked on
(`handoffSummary` / `handoffCitations` / `handoffQuestions`, camelCase, matching that note exactly).

### 3.5 `store.py` — thread them through `create_escalation_from_case`
```python
handoffSummary=case.handoffSummary,
handoffCitations=case.handoffCitations,
handoffQuestions=case.handoffQuestions,
```
added to the `EscalationDetail(...)` construction, same as `explanation=case.explanation` today.

### 3.6 `main.py` — screen, then build
Two additions in `_triage_event_stream`, both right after the `orchestrate()` loop returns,
mirroring the existing `rationale` guardrail block:
```python
handoff_summary = state.handoff_summary
if handoff_summary:
    handoff_guard = guardrail.screen_output(handoff_summary)
    if handoff_guard.status == "flagged":
        audit_log.record(case_id, actor="output_guardrail", action="flagged",
                          detail=handoff_guard.detail)
        handoff_summary = guardrail._SAFE_OUTPUT_FALLBACK
```
Then when building `case_record`, add:
```python
handoffSummary=handoff_summary,
handoffCitations=[Citation(**c) for c in state.handoff_citations],
handoffQuestions=state.handoff_questions,
```
No change needed to the `if state.escalated: ... store.create_escalation_from_case(...)` gate
itself — `case_record` already carries the fields by the time that call happens.

### 3.7 `tests/agents/harness.py` — register it in the shared contract test
```python
from app.agents import ClinicianHandoffAgent   # add to the existing import block
...
AGENT_CLASSES: dict[str, type] = {
    ...,
    "handoff": ClinicianHandoffAgent,
}
```
That's the only change needed for `test_contracts.py`'s parametrized boundary test to start
covering `handoff.py` automatically — no new test code. (`pytest.ini` already registers the
`handoff` marker, so nothing to do there.)

## 4. Constraints I think are non-negotiable

1. **Ordering: after Reflection, not after HITL.** See §2 — summarising before Reflection's
   severity backstop can run produces a summary for a decision that's about to change.
2. **Skip on `"ask"`, not just on `escalated=False`.** Already true by construction (§2) — flagging
   it so the new `orchestrate()` step doesn't accidentally get placed before HITL/Reflection settle
   the final value of `state.escalated`.
3. **Output-guardrail the summary before it reaches `EscalationDetail`.** Same LLM05 check
   `rationale` already gets — an LLM-authored field reaching a clinician unscreened is the one gap
   `handoff.py`'s own docstring calls out as an explicit non-goal of that file (screening is a
   `main.py` concern).
4. **Don't let this touch `escalated` / `escalation_reason`.** `handoff.py`'s `CONTRACT.writes` is
   `{handoff_summary, handoff_citations, handoff_questions}` only — the contract test in §3.7 will
   catch a violation, but flagging it here too since it's the same invariant HITL and Reflection
   both hold.

## 5. Decisions that are genuinely yours

- Whether `CaseRecord` gets the three fields (my recommendation, §3.4, for symmetry with
  `explanation`/`reflection`) or whether `create_escalation_from_case` instead takes the raw
  `CaseState` alongside `CaseRecord` — your call on `store.py`'s shape.
- Whether the new `orchestrate()` step gets its own `agent_active`/`agent_result` SSE pair (my
  recommendation, §3.3, consistent with every other step) or is folded into the existing
  `reflection` step's events.
- Whether `Home.md` links this note and [[Clarification Resume API Handoff]] from the Core notes
  list — neither is linked there yet; leaving that to you since it's the shared vault index.

## 6. What this unblocks

- [[Clinician Handoff UI]] — the frontend block is already drafted and `&&`-guarded; it starts
  rendering the moment `getEscalation(id)` returns the three new fields.
- `tests/test_eval_handoff.py` (local E7 spec, not yet registered in `app/evals/plan.py`) currently
  scores `handoff.py` against hand-built `CaseState` fixtures only. Once wired, E7 can also run
  end-to-end through `/api/triage/stream`, the same upgrade [[Clarification Resume API Handoff]]
  describes for E2.

## Changelog
- 2026-08-25: note drafted after `handoff.py`'s LLM-path E7 scoring landed; nothing in the
  platform files above touched yet.
