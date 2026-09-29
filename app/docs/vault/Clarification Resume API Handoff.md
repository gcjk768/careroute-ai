---
tags: [handoff, active, careroute]
updated: 2026-08-20
---
# Clarification Resume API Handoff — for James

Back to [[Home]]. Related: [[HITL Clarification Handshake]] · [[Evaluation Plan]] · [[Changelog]]

**From:** Heriz (HITL owner) → **To:** James (platform: `main.py`, `models.py`, `supervisor.py`)
**Status:** my side is done and delivered; the patient-facing half is not started.

> **Done 2026-09-26.** The patient-facing half shipped as the clarifying INTERVIEW —
> `TriageRequest.clarifications`, the `interview` block on the final event, an ask that ends the turn
> before Reflection, and a chat thread in the patient UI. Widened beyond the shape below (up to four
> questions, LLM-worded dimension questions). See
> [`docs/design/specs/2026-09-26-clarifying-chat-interview-design.md`](../design/specs/2026-09-26-clarifying-chat-interview-design.md)
> and the [[Changelog]]. The rest of this note is kept as the record of the handoff.

`HumanInTheLoopAgent.run()` can now decide **escalate / ask / proceed** instead of just
escalate/don't (see [[HITL Clarification Handshake]] for the design). The "ask" branch works and
is tested. Nothing downstream of it exists yet — a real patient today can never actually see or
answer the question. This note is what's needed to close that gap, on your side, in your own
files. I have not touched `main.py`, `models.py`, or `supervisor.py`.

---

## 1. What is already true (verified, not assumed)

`backend/app/agents/hitl.py::run()`, when it decides to ask, returns:

```python
{
    "source": "deterministic",
    "escalated": False,
    "reason": None,
    "action": "ask",
    "question": "Do you also have a fever or any difficulty breathing?",
    "clarification": {
        "feature": "cold_symptoms", "label": "cold symptoms",
        "question": "Do you also have a fever or any difficulty breathing?",
        "gain": 2.482,
    },
}
```

I checked exactly where this goes today: `Supervisor.orchestrate()` (`supervisor.py` around the
HITL step) yields it as `{"event": "agent_result", "agent": "hitl", ..., "data": hitl_result}` —
so it reaches the SSE stream, but only inside the generic pipeline-console event a developer sees,
never in the patient-facing `"final"` event (`main.py`'s `_triage_event_stream`, the last `yield`
before the function returns). I grepped `main.py` for `clarif` — zero matches. `TriageRequest` in
`models.py` has no `clarifications` field. Nothing is broken; it was never built, on purpose (your
own note in [[HITL Clarification Handshake]] §8 said you'd take this once my half set the shape).

**One more thing this touches, already true today:** `CaseState.clarifications` (a list, the
patient's prior-round answers) already exists in `base.py` and is already read by both the
classifier (won't re-propose an already-answered gap) and by HITL (won't ask twice — see
`hitl.py`'s "never twice" floor). **The read side is built. There is currently no way for a
request to ever populate it** — that's the missing link.

## 2. What "ask" means for the response you build

When `hitl_result["action"] == "ask"`:
- `state.escalated` is `False` — **no escalation record gets created**
  (`main.py`'s `if state.escalated: ... store.create_escalation_from_case(...)` block is correctly
  skipped; don't touch that gate).
- The case is **not finished**. Today's `"final"` SSE event assumes every case ends in
  escalated-or-not; a case that ends in "ask" needs a third outcome shape, or the patient gets a
  response that looks like "you're fine" when actually a question is pending.
- `state.clarification` (set by the classifier, `{feature, label, question, gain}`) is what to
  show. `hitl_result["question"]` is the same string, already extracted for you.

## 3. What's needed, concretely

1. **A response shape (or new SSE event) for "needs clarification"** distinct from `"final"` —
   carrying at minimum the `question` string and enough of an identifier (`caseId`, `sessionId`)
   to resume. Whether this is a new event name (`clarification_requested`?) or a variant of
   `"final"` with a `needsClarification` flag is your call — I'd lean toward a distinct event so a
   client that doesn't handle it yet fails obviously instead of silently mis-rendering, but you
   own this file.
2. **A `clarifications` field on `TriageRequest`** (`models.py:64`, alongside `sessionId`/`text`)
   so a RESUME request can carry the patient's answer(s) back in. Shape should match what
   `CaseState.clarifications` already expects — a list of dicts; the classifier and HITL don't
   care about the schema beyond "non-empty means don't ask again," so keep it whatever's easiest
   for the frontend to build (e.g. `[{"feature": "cold_symptoms", "answer": "no fever"}]`).
3. **Wire that field into `state.clarifications`** in `_triage_event_stream` (`main.py:89`,
   right where `CaseState(...)` is constructed from `req.*` — `age_band`, `latitude`, etc. are
   already threaded through the same way).
4. **The resume path itself** — how a second request continues the SAME case rather than starting
   a fresh one. The pipeline is stateless per-request today (a resume would just re-run
   intake→classifier→...→HITL with `state.clarifications` now populated, and HITL's one-round cap
   means it can't ask again). Whether that's the same `/api/triage/stream` endpoint called again
   with `clarifications` filled in, or a dedicated `/api/triage/resume`, is your call.
5. **Frontend**: a UI step in the patient triage flow that shows the question, collects free text
   (or a yes/no — your call, matches whatever `answer` shape you pick in #2), and resubmits.

## 4. Constraints I think are non-negotiable

1. **An "ask" outcome must never create an escalation record.** That gate already exists and is
   correct (`main.py`'s `if state.escalated` block) — just don't let a resume path accidentally
   flip `escalated` before HITL runs again.
2. **The one-round cap is enforced on YOUR side too, not just HITL's.** HITL already refuses to
   ask again when `state.clarifications` is non-empty, but that only holds if the resume path
   actually populates it before re-running the pipeline. If it doesn't, a patient could get asked
   the same or a different question every single resubmission.
3. **No raw patient text should leak further than it already does.** The `question` string itself
   is safe (it's a template about a symptom category, not a quote of the patient's own words) —
   same privacy bar the A2A bus payload already holds itself to (see [[HITL Clarification
   Handshake]] §2, "no raw patient text").

## 5. Decisions that are genuinely yours

- New SSE event vs. a flag on `"final"` (§3.1).
- Resume as a repeat call to the same endpoint vs. a dedicated one (§3.4).
- The exact shape of an "answer" (§3.2) — free text, structured, yes/no.
- Whether the resumed request needs `sessionId` alone to correlate, or a new `caseId` echo-back.

## 6. What this unblocks

[[Evaluation Plan]] **E2 (Appropriateness of clarifying questions)** — my side is unblocked
(the feature exists) but E2 can't run **end-to-end through the real API** until a patient can
actually receive and answer a question. `tests/fixtures/clarifying_questions_gold.json` (the gold
set, still to be built, mine) can score `hitl.py` in isolation regardless — this handoff is what
lets E2 also prove the whole round-trip, not just the agent's decision.
