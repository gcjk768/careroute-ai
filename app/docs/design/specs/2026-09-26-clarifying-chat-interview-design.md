---
tags: [architecture, careroute, active]
updated: 2026-09-26
---
# Design — Multi-turn clarifying interview ("chat") before the triage decision

Related: [[Clarifying Questions Design]] · [[HITL Clarification Handshake]] · [[Clarification Resume API Handoff]] · [[Evaluation Plan]]

Owner: Koh Guan Chin James. Branch: `feat/clarifying-chat`.

## Problem

The 2026-07-29 design gave the Severity-Classifier ONE clarifying question, asked only
when confidence fell below 0.5 and never twice. Two things made it invisible in practice:

1. The trigger is narrow. Anything with a recognisable symptom scores ≥ 0.74 and is
   never asked. Only vague text ("I don't feel right") reaches the ask branch.
2. On the live deployment (LLM on) the Reflection critic escalates low-confidence
   cases in the SAME turn HITL decided to ask. `main.py` hides `clarification` when
   `escalated` is true, so the patient sees "clinician review", never the question.
3. The resume path was never wired: `TriageRequest` had no `clarifications` field.

The requirement is a chatbot-style intake: the agent interviews the patient with a
few targeted questions before deciding, so the acuity and care tier are sharper.
The system still does not diagnose.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Always interview first**, up to `INTERVIEW_MAX_QUESTIONS = 4`, stop early at `INTERVIEW_CONFIDENCE_TARGET = 0.8` | Collects the most evidence; the 0.5 escalation floor is unchanged and still applies when the budget is spent. |
| 2 | **Stateless turns**: the request carries the original text plus the full transcript | Replayable for audit, no new spoofable identifier — same principle as the 2026-07-29 design. |
| 3 | **Classifier proposes, HITL decides** — unchanged | Governance and the audited action spaces stay as they are. |
| 4 | **Two question sources, fixed first**: value-of-information template pick, then an LLM question on an uncovered dimension (onset / duration / severity / associated), then a fixed four-question bank | Template questions are proven to move the model; the LLM fills what the model cannot measure; the bank keeps the chat alive offline. |
| 5 | **An "ask" ends the turn after HITL**: Reflection and Handoff do not run while the interview is open | The critic's forced escalation is what hid the question on the live site. Reflection still reviews the final turn in full. |
| 6 | **Red flags and P1/P2 never wait** | Safety-Override and the acuity floor run before HITL every turn; a red flag on any answer escalates immediately. |
| 7 | **Answers are folded into the symptom text by Intake**, negation preserved | The trained model, keyword table and red-flag regexes all read `normalised_symptoms`; "no" becomes "no <symptom>" so the negation-aware matcher handles it. |
| 8 | **Never repeat a question** | Asked features / dimensions and asked question text are excluded from both sources. |

## Contract

Request (`models.TriageRequest`):

```json
"clarifications": [
  {"question": "Do you also have a fever or any difficulty breathing?",
   "answer": "no", "feature": "cold_symptoms", "source": "template",
   "statement": "fever or difficulty breathing"}
]
```

At most 4 entries; `answer` 1–300 chars; `question` ≤ 300; `statement` ≤ 120.

Final SSE event gains:

```json
"clarification": {feature, label, question, gain, source, statement} | null,
"interview": {"round": <answers so far>, "budget": 4, "done": true|false}
```

`clarification` is non-null only when HITL chose `ask` this turn. While `done` is
false the rest of the payload is the provisional decision and no case record,
escalation or "completed" metric is written.

## Flow

```
turn n   guardrail + redaction (text AND each answer)
         → intake (folds answers into normalised_symptoms)
         → classifier (acuity; proposes next question from template → LLM → bank)
         → safety (red flag ⇒ escalate, interview over)
         → routing
         → HITL: floors ⇒ escalate | interview open & question ⇒ ask | conf<0.5 ⇒ escalate | proceed
         ask ⇒ STOP (no reflection, no handoff), final{done:false, clarification}
         else ⇒ reflection → handoff → final{done:true}
```

## Ownership

| File | Change |
|---|---|
| `agents/base.py` | `INTERVIEW_MAX_QUESTIONS`, `INTERVIEW_CONFIDENCE_TARGET`, `GAIN_THRESHOLD` moved here; `CaseState.clarification_asked` |
| `agents/classifier.py` | asked-feature exclusion, dimension questions (LLM + bank), `source`/`statement` on the proposal |
| `agents/hitl.py` | interview policy; writes `clarification_asked` |
| `agents/intake.py` | `fold_clarifications` |
| `agents/orchestration.py` | stop after HITL on ask |
| `models.py`, `main.py` | request field, screening, `interview` block, skip persistence on ask |
| `components/PatientTriage.jsx`, `lib/interview.js`, `.module.css` | chat thread, answer box, quick replies |

## Error handling

Question generation is best-effort at every layer: template failure → dimension bank;
LLM failure or invalid output → bank; nothing left to ask → `None` → HITL falls
through to today's rules. The loop can only add evidence before the existing decision.

## Testing

Unit: request validation; intake folding (yes / no / free text); classifier skips
asked features, falls back to the bank, validates LLM output; HITL budget, 0.8 bar,
no-repeat, floors unchanged; orchestrator stops after HITL on ask.
End-to-end: a four-turn offline run of a vague complaint — no repeated question,
terminates, final decision made. E2 gold fixture updated (`already_asked` becomes
`budget_spent`). Frontend `node --test` for the transcript helper. Full suite green
with the LLM disabled (`CLAUDE_CLI_BIN` bogus, as CI).
