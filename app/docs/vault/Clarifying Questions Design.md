---
tags: [architecture, careroute, active]
updated: 2026-08-11
---
# Clarifying questions — active elicitation

Back to [[Home]]. Related: [[Agent Capability Audit]] · [[Evaluation Plan]] · [[App Overview]] · [[Loop Engineering]] · [[Changelog]]

Full design: [`docs/design/specs/2026-07-29-classifier-clarifying-questions-design.md`](../design/specs/2026-07-29-classifier-clarifying-questions-design.md).
Code: [`backend/app/agents/classifier.py`](../../backend/app/agents/classifier.py).

> **Superseded in part, 2026-09-26.** The single question became a multi-turn INTERVIEW (up to four questions, as a chat, template screen then LLM-worded dimension questions): [`docs/design/specs/2026-09-26-clarifying-chat-interview-design.md`](../design/specs/2026-09-26-clarifying-chat-interview-design.md). The gap-detection mechanism below still stands; the one-round cap and the 0.5 trigger do not.

## The problem

When a description is vague no feature fires, and the system's only response to
"I don't know enough" is to escalate to a clinician. `ml/features.py:44` records
the measured version: every minor-injury complaint lit up zero symptom flags and
scored an identical 0.448 confidence, just under the HITL threshold.

The better response is to ask the patient one question.

## The shape

**The classifier proposes, HITL decides.** Only the classifier can name the
information gap — it owns the model and its feature space. Only HITL should own
the decision to interrupt a patient; that is its declared action space, and
widening the classifier's would break `enforce_capability()`.

The gap is chosen by **expected information gain**: for each symptom feature that
came back zero, flip it to 1, re-predict, keep the largest swing. The question
asked is therefore one whose answer could actually change the outcome. ~22 extra
RF predictions, deterministic, offline.

**Off the model path** (2026-08-04) the same question is asked of the keyword
rules table: for each rule the patient has not mentioned, score acuity-rank
movement + confidence movement, keep the largest. Same gain scale, so the number
means the same thing to HITL either way. This exists because tying elicitation to
`app/ml/` made it dead on the offline deployment — precisely where confidence is
lowest and a question is worth the most. It substitutes for an **absent** model
and never overrides a present one that declined. "Mentioned" covers reported *and*
denied, so the single round is never spent on a symptom already ruled out.

Phrasing is a **template floor with optional LLM polish** — the LLM may only
reword a gap the model already chose, the same containment
`SemanticRedFlagLayer` uses.

Resume is a **stateless re-run**: `TriageRequest` carries `clarifications`, the
whole pipeline re-runs, and a non-empty array is what caps the loop at one round.
No server state, no TTL, no new spoofable identifier, and the whole thing is
replayable for audit.

## Invariants

- never ask when a red flag is present (`safety_triggered` / `safety_fast_path`)
- never ask when the case already carries an answer
- never ask when no absent feature would move the prediction
- never ask about a symptom already reported *or* explicitly denied
- any failure returns `None` and HITL escalates as before

The loop is strictly additive: it can only turn an escalation into a question,
never a question into a missed escalation. Same monotone-merge guarantee as
[[Agent Capability Audit]].

## The A2A interface (what HITL has to read)

The proposal reaches HITL over the message bus, not by reaching into `CaseState`
— `CaseState` holds only the latest value of a field, the bus holds what each
agent *asserted* when it acted, and the interruption decision should be auditable
against the claim that justified it.

The channel needed no negotiation: the classifier already publishes
`acuity.classified` and `HumanInTheLoopAgent.COMMS` already subscribes to it.
What changed (2026-08-11) is the payload — it carried the question **string**,
and now carries the whole proposal:

```python
payload["clarification"]  # {feature, label, question, gain}  — or None
```

`gain` is the part that matters to the receiver: the text is enough to *ask* but
not to *decide*, and deciding is HITL's half. `None` is sent explicitly, never
omitted — absence of a proposal is itself the instruction (escalate, as today).

Read side, the pattern [`reflection.py:127`](../../backend/app/agents/reflection.py)
already demonstrates:

```python
if getattr(self, "_consumed", False):
    proposal = (self.received_payload("acuity.classified") or {}).get("clarification")
```

A cross-agent contract test in `tests/agents/test_classifier.py` asserts the two
`COMMS` declarations line up. It deliberately asserts nothing about HITL's
behaviour, so it keeps passing while `hitl.py` is still Heriz's to finish.

## Status

Classifier half + `CaseState` fields + the A2A payload: **done**
(`feature/severity-classifier-agent`).

HITL half (Heriz), API half and patient-facing resume UI: **not started**. Until
they land, the question is computed, published on the bus and carried on the
state, but never shown — HITL's action space is still `escalate / proceed` and
must become `escalate / ask / proceed`. Measured 2026-08-11: a vague complaint
scored 0.427 with a proposal at gain 2.456 sitting in HITL's inbox, and was
escalated to a clinician anyway.

The blocker is therefore no longer a missing interface — it is one agent acting
on a message it already receives.

Unblocks [[Evaluation Plan]] E2, which also gains a free objective metric
alongside the human grade — *confidence lift after clarification*.
