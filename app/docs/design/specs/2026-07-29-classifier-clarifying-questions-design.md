---
tags: [architecture, careroute, active]
updated: 2026-07-29
---
# Design — Clarifying questions from the Severity-Classifier

Related: [[Agent Capability Audit]] · [[Evaluation Plan]] · [[App Overview]] · [[Loop Engineering]]

Owner: Koh Guan Chin James (classifier half) · Heriz Yusoff (HITL half) · platform (API half)

## Problem

When a patient's description is vague, the classifier has no feature to fire on.
`app/ml/features.py:44` records the concrete failure: every minor-injury complaint
lit up zero symptom flags, collapsed to the same feature vector, and scored an
identical 0.448 confidence. Below `CONFIDENCE_THRESHOLD`, that becomes a clinician
interruption. The system's only response to "I don't know enough" is to escalate.

The better response is to ask. Nothing in `app/` asks the patient anything today —
[[Evaluation Plan]] E2 is blocked on precisely this, and it is the declared
`upgrade_path` of the HITL worker (`app/agents/hitl.py:58`).

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Classifier proposes, HITL decides** | Only the classifier can name the information gap — it owns the model and its feature space. Only HITL should own the decision to interrupt a patient; that is its declared action space, and widening the classifier's would break `enforce_capability()`. |
| 2 | **Full ask-and-resume loop**, not advisory-only | Requested explicitly: the report needs the loop working end to end. |
| 3 | **Stateless re-run** | `TriageRequest` gains `clarifications`. No server state, no TTL, no new spoofable identifier, fully replayable for audit, and E2 becomes a pure pytest fixture. |
| 4 | **Gap chosen by expected information gain** | For each absent symptom feature, flip it to 1 and re-predict; keep the largest swing. Guarantees the question asked is one whose answer would change the outcome. |
| 5 | **Template floor + optional LLM polish** | Deterministic template per feature category is always available and is what the tests exercise. The LLM may only reword a gap the model already chose — the containment `SemanticRedFlagLayer` uses. |
| 6 | **Exactly one question, one round** | `hitl.py:59` already specifies one; mirrors the reflection loop's one-iteration cap (`base.py:158`). |
| 7 | **Never ask when a red flag fires** | `safety_triggered` or `safety_fast_path` ⇒ escalate immediately. |
| 8 | **Gap detection lives in `classifier.py`**, not a separate `ml/` module | Chosen for presentation clarity — one file a reviewer can follow. Costs: ML imports must stay lazy (the agent must import without `sklearn`), and the template map needs a drift guard against `FEATURE_KEYWORDS`. |

## Architecture

```
first pass    intake → classifier → safety → routing → HITL
                          │                              │
                          │ proposes gap + question      │ escalate / ASK / proceed
                          └──────────────────────────────┘

second pass   frontend re-POSTs original text + [{question, answer}]
              → intake folds the answer in → whole pipeline re-runs
              → clarifications non-empty ⇒ classifier proposes nothing ⇒ one round
```

### Trigger condition

Ask only when **all** hold:

- `confidence < CONFIDENCE_THRESHOLD`
- `not safety_triggered` and `not safety_fast_path`
- `clarifications` is empty (the one-round cap)
- `best_gain >= MIN_GAIN`

The last clause matters: if no missing feature would move the prediction, asking
is worse than useless and the case falls through to escalation exactly as today.

### Ownership

| File | Owner | Change |
|---|---|---|
| `agents/classifier.py` | James | gap detection, templates, propose the question |
| `agents/base.py` | James (platform) | new `CaseState` fields |
| `agents/hitl.py` | Heriz | action space 2 → 3; ask vs escalate vs proceed |
| `models.py`, `main.py` | platform | `TriageRequest.clarifications`, SSE event |
| `components/PatientTriage.jsx` | frontend | ask-and-resubmit UI |

## Error handling

Gap detection is wrapped and returns `None` on any failure — missing ML deps,
unloadable artefact, unexpected feature shape. `None` means no question is
proposed, HITL falls through to its existing rules, and the case escalates as it
does today.

The loop is strictly additive: **it can only turn an escalation into a question,
never a question into a missed escalation.** Same monotone-merge guarantee as
[[Agent Capability Audit]]; keeps `test_triage_eval.py`'s red-flag recall of 1.0
intact.

### Resume-path security

A second POST carrying `clarifications` is patient-supplied text arriving at a
stage the guardrail already vets, so it passes through `app/guardrail.py` and
`app/redact.py` on the same footing as the original text. The answer is appended
to the symptom text and never treated as a control signal. A `clarifications`
array longer than one entry is rejected rather than looped on — otherwise
"answer" becomes an unbounded injection channel that bypasses intake's front door.

## Testing

- **Classifier** — gap detection picks a feature that genuinely moves the
  prediction; returns `None` when nothing does; returns `None` with ML deps
  absent; every `FEATURE_KEYWORDS` category has a template; nothing proposed when
  `safety_fast_path` is set.
- **Contract** — the new field is in `CONTRACT.writes`; `test_contracts.py`
  already fails the build otherwise.
- **HITL** — the three existing rules still fire identically (regression on
  Heriz's floor); ask only when a gap exists and no red flag does.
- **End-to-end** — a fixture whose first pass is low-confidence and whose second
  pass, answer folded in, clears the threshold. This is E2's dataset, and it runs
  in pytest with no server because resume is stateless.

## Evaluation

E2 grades *appropriateness* of the question by human review. The design also
yields a free objective metric alongside it — **confidence lift after
clarification** — which needs no grader.

## Status

Classifier half and `CaseState` fields: implemented on
`feature/severity-classifier-agent`. HITL half, API half and frontend half: not
started, and owned by others.
