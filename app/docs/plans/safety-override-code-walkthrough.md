# Safety-Override Code Walkthrough

This guide traces where Safety-Override comes into play in CareRoute AI. It assumes no prior knowledge of the application, Python agents, or the backend pipeline.

> Phase 1 status: `SafetyOverrideAgent.prescreen()` and `SafetyOverrideAgent.run()` are implemented and tested. The deterministic pipeline operates without model access; later phases add the load-bearing Supervisor protocol and multilingual model channels.

## Mental Model

Think of the backend as a hospital workflow:

| Component | Simple analogy |
|---|---|
| `Supervisor` | The coordinator deciding which worker runs next |
| `CaseState` | A shared patient worksheet |
| Message bus | Signed handover notes between workers |
| Intake agent | Cleans up the patient's description |
| Classifier | Makes the initial urgency estimate |
| Safety-Override | Independently checks for unmistakable emergencies |
| Care-Routing | Chooses the care destination |
| HITL | Decides whether a clinician must review |
| Reflection | Performs a final consistency check |

Safety-Override is not intended to make the original diagnosis. Its job is narrower:

> If the classifier misses an obvious emergency, Safety-Override must raise the urgency before routing occurs.

## Overall Flow

A triage request is intended to move through this sequence:

```text
Browser
  |
  v
FastAPI endpoint
  |
  v
Input guardrail
  |
  v
PII redaction
  |
  v
Create CaseState
  |
  v
Early Safety prescreen
  |
  v
Intake
  |
  v
Severity Classifier
  |
  v
Authoritative Safety-Override
  |
  v
Care-Routing
  |
  v
Human-in-the-Loop
  |
  v
Reflection
  |
  v
Save and return final result
```

Safety appears in three places:

1. Before Intake and Classification as a cheap prescreen.
2. After Classification as the authoritative override.
3. During a Reflection rerun to ensure the safety decision is still enforced.

## 1. Request Entry

The frontend sends symptoms to:

```http
POST /api/triage/stream
```

The endpoint is defined at `backend/app/main.py:287`.

A typical request resembles:

```json
{
  "text": "I have crushing chest pain going down my left arm",
  "language": "en",
  "isVoice": false,
  "ageBand": "45-64",
  "sex": "male"
}
```

The response uses Server-Sent Events, or SSE. Instead of waiting for the entire pipeline to finish, the backend sends events such as:

```text
agent_active
agent_result
safety_override
final
```

This allows the frontend to animate the agents as they work.

## 2. Guardrail and Redaction

Before any agent receives the symptoms, `_triage_event_stream()` performs two important actions at `backend/app/main.py:89`.

First, it screens the original input:

```python
screening = guardrail.screen(req.text)
```

The guardrail checks for:

- Empty or oversized input.
- Prompt-injection attempts.
- Encoded attacks.
- Fake system messages.
- Clearly non-medical requests.

If the guardrail blocks the input, none of the agents run.

Second, it masks identifiers:

```python
masked_text, pii_found = redact.redact(req.text)
```

This occurs at `backend/app/main.py:110`.

The agents receive `masked_text`, not the original request text.

For example:

```text
My NRIC is S1234567A and I have chest pain
```

might become:

```text
My NRIC is [REDACTED_NRIC] and I have chest pain
```

## 3. Shared CaseState

After redaction, the backend creates a `CaseState` object at `backend/app/main.py:116`.

The class is defined at `backend/app/agents/base.py:112`.

You can think of it as a shared worksheet:

```python
state = CaseState(
    raw_text=masked_text,
    language=req.language,
    age_band=req.ageBand,
    sex=req.sex,
)
```

Initially, many fields contain defaults:

```python
acuity_code = "P3_URGENT"
confidence = 0.5
safety_triggered = False
safety_rule = None
semantic_flags = []
```

Each agent is allowed to update only certain fields:

| Agent | Main fields it writes |
|---|---|
| Intake | Normalized symptoms, detected language, keywords |
| Classifier | Acuity, confidence, evidence, explanation, citations |
| Safety | Prior acuity, safety result, and potentially more urgent acuity |
| Routing | Care tier, clinic, wait estimate |
| HITL | Escalation decision and reason |
| Reflection | Final consistency review and corrections |

These boundaries are declared using `AgentContract`.

## 4. Early Safety Prescreen

The first thing the Supervisor tries is:

```python
self._safety_pregate(state)
```

This is at `backend/app/agents/supervisor.py:264`.

That method calls:

```python
state.safety_fast_path = self.safety.prescreen(state.raw_text)
```

The prescreen is intended to be:

- Fast.
- Deterministic.
- Synchronous.
- Non-mutating.
- Independent of an LLM.

Its purpose is only to set a hint:

```python
state.safety_fast_path = True
```

It does not change acuity.

### Why Have a Prescreen?

Suppose the text says:

```text
I cannot breathe and my lips are turning blue.
```

The deterministic rules can recognize this immediately.

The classifier still runs, but if its trained ML model is unavailable, it can avoid spending time on an unnecessary LLM fallback. Safety already knows that the authoritative override will force an emergency result later.

The prescreen is therefore an optimization, not the final safety decision.

### Current Behavior

`prescreen()` is implemented at `backend/app/agents/safety.py`. It enforces access to `redflags.evaluate`, returns the deterministic trigger result, and safely degrades to `False` if the evaluator fails. Unit tests also prove that it never invokes semantic reasoning or an LLM.

## 5. Symptom Intake

After the prescreen, the Intake agent is supposed to run at `backend/app/agents/supervisor.py:271`.

It takes something like:

```text
Chest feels like elephant sitting on it, started 20 mins ago
```

and produces fields such as:

```python
state.normalised_symptoms = (
    "Severe chest pressure beginning approximately 20 minutes ago."
)

state.intake_keywords = [
    "chest pressure",
    "severe",
    "acute onset",
]
```

The original redacted text remains in `state.raw_text`.

This is important because Safety later examines both:

- What the patient originally said.
- What Intake normalized it into.

Safety should not rely only on normalized text because Intake could accidentally omit an important phrase.

## 6. Severity Classification

The Classifier runs next at `backend/app/agents/supervisor.py:289`.

It attempts the following paths:

```text
Trained RandomForest model
    |
    | unavailable
    v
LLM classification
    |
    | unavailable
    v
Deterministic keyword fallback
```

The classifier writes fields such as:

```python
state.acuity_code = "P4_NON_URGENT"
state.confidence = 0.72
state.evidence = ["chest discomfort"]
state.explanation = [...]
```

This is only the initial estimate. The Safety worker has not yet made its authoritative decision.

## 7. Classifier Message

After classifying the case, the Classifier publishes:

```text
acuity.classified
```

The communication declaration is in `backend/app/agents/classifier.py:84`.

Its payload contains:

```json
{
  "acuity_code": "P4_NON_URGENT",
  "confidence": 0.72,
  "evidence": ["chest discomfort"]
}
```

The message is broadcast to subscribed agents, including Safety.

There are two ways agents share information:

| Mechanism | Purpose |
|---|---|
| `CaseState` | Contains the latest values |
| Message bus | Records what an agent announced at that point in time |

A useful analogy is:

- `CaseState` is the shared worksheet.
- The message bus is the signed handover record.

## 8. Authoritative Safety Step

Safety runs after Classification and before Care-Routing at `backend/app/agents/supervisor.py:308`.

The intended order is:

```python
await self.safety.areason(state)
safety_result = self.safety.run(state)
```

These are two different parts of Safety.

### Semantic Reasoning

`areason()` is defined at `backend/app/agents/safety.py:234`.

It calls `SemanticRedFlagLayer`, which uses the shared LLM provider chain.

The semantic layer is intended to catch phrases that literal regular expressions miss, such as:

```text
It feels like an elephant is sitting on my chest.
```

or:

```text
No puedo respirar.
```

The LLM is not asked to choose an acuity level. It can only choose one of the existing categories:

```text
cardiac_chest_pain
breathlessness
anaphylaxis
stroke_signs
severe_bleeding
suicidal_ideation
seizure
```

If accepted, the finding is saved in:

```python
state.semantic_flags
```

For example:

```python
[
    {
        "triggered": True,
        "label": "cardiac_chest_pain",
        "confidence": 0.91,
        "rationale": "The patient describes severe chest pressure."
    }
]
```

If the LLM is unavailable, malformed, disabled, or uncertain, `areason()` returns `None`.

The deterministic path is supposed to continue normally.

### Deterministic Authoritative Gate

After semantic reasoning, the Supervisor calls:

```python
self.safety.run(state)
```

This is the actual gate that may modify acuity.

`run()` is implemented as a synchronous, escalation-only gate. It evaluates the combined raw and normalized symptom text, records prior acuity on every call, merges existing semantic findings additively, and derives every forced acuity from the deterministic rule table.

## 9. Deterministic Rules

The rule table is in `backend/app/redflags.py:28`.

There are seven current categories:

| Rule | Example phrases | Forced acuity |
|---|---|---|
| `cardiac_chest_pain` | chest pain, chest pressure, left-arm pain | P1 |
| `breathlessness` | cannot breathe, gasping, blue lips | P1 |
| `anaphylaxis` | closing throat, swollen tongue | P1 |
| `stroke_signs` | face droop, slurred speech, one-sided weakness | P1 |
| `severe_bleeding` | uncontrolled bleeding, vomiting blood | P2 |
| `suicidal_ideation` | kill myself, end my life | P2 |
| `seizure` | seizure, convulsion | P2 |

The rules use regular expressions.

For example:

```python
r"chest pain"
r"chest pressure"
r"can'?t breathe"
r"slurred speech"
```

`redflags.evaluate(text)` checks all the rules and returns the most severe match.

An example result is conceptually:

```python
SafetyOverrideResult(
    triggered=True,
    rule="cardiac_chest_pain",
    reason="Possible acute coronary syndrome.",
    forced_acuity="P1_RESUSCITATION",
)
```

## 10. Why Safety Cannot Lower Acuity

The key function is:

```python
redflags.apply_override(current_acuity, safety_result)
```

It is defined at `backend/app/redflags.py:122`.

Suppose the classifier says:

```python
current_acuity = "P4_NON_URGENT"
```

and Safety finds chest pain:

```python
forced_acuity = "P1_RESUSCITATION"
```

The result becomes:

```python
"P1_RESUSCITATION"
```

Now suppose the classifier already says P1, while Safety finds a P2 category:

```text
Classifier: P1
Safety:     P2
```

Safety must not change the result to P2. P2 would be less urgent than P1.

The final result remains P1.

This is the monotone safety rule:

```text
Safety can move urgency up.
Safety can never move urgency down.
```

## 11. Intended State Changes

Before Safety:

```python
state.acuity_code = "P4_NON_URGENT"
state.confidence = 0.72
state.safety_triggered = False
state.safety_rule = None
```

After a chest-pain override:

```python
state.prior_acuity_code = "P4_NON_URGENT"
state.acuity_code = "P1_RESUSCITATION"
state.safety_triggered = True
state.safety_rule = "cardiac_chest_pain"
state.safety_reason = "Possible acute coronary syndrome."
```

Safety is also expected to add an explanation entry so users can see why acuity changed:

```python
{
    "feature": "safety_override:cardiac_chest_pain",
    "weight": 1.0
}
```

The permitted state fields and required return fields are declared at `backend/app/agents/safety.py:177`.

## 12. Deterministic and Semantic Merge

The intended Safety result can come from four channel states:

| Deterministic | Semantic | Channel |
|---|---|---|
| No | No | `none` |
| Yes | No | `deterministic` |
| No | Yes | `semantic` |
| Yes | Yes | `both` |

This logic is implemented in `_merge_channel()` at `backend/app/agents/safety.py:318`.

The important merge rule is:

```text
deterministic OR semantic
```

It is not:

```text
semantic replaces deterministic
```

If the deterministic rule fires, the LLM cannot veto it.

## 13. Safety Output Events

After Safety runs, the Supervisor intends to send several events.

First:

```text
agent_result
```

This contains the detailed Safety result.

Second:

```text
safety_override
```

This contains:

```json
{
  "triggered": true,
  "rule": "cardiac_chest_pain",
  "priorAcuity": "P4_NON_URGENT",
  "forcedAcuity": "P1_RESUSCITATION"
}
```

Third, Safety publishes the agent-to-agent message:

```text
safety.override
```

The current message is created by `SafetyOverrideAgent.emit()` at `backend/app/agents/safety.py:201`.

## 14. Current Communication Limitation

At present, Safety broadcasts `safety.override` to downstream agents.

The following agents subscribe to it:

- Care-Routing.
- Human-in-the-Loop.
- Reflection.

The Supervisor does not currently subscribe to `safety.override`.

Also, Safety subscribes to `acuity.classified`, but it does not currently use `received_payload()` to validate or reason from that announcement.

So the current message flow is partly an audit trail rather than a fully load-bearing conversation.

That is why the implementation plan introduces:

```text
Supervisor -> safety.assessment.requested -> Safety
Safety -> safety.override -> Supervisor
```

The goal is to make routing wait for a verified Safety response.

## 15. Care-Routing Uses Post-Safety Acuity

Care-Routing runs only after Safety at `backend/app/agents/supervisor.py:340`.

It reads the current:

```python
state.acuity_code
```

That means it sees the overridden value.

Without an override:

```text
Classifier result: P4
Routing sees:      P4
Destination:       GP
```

With an override:

```text
Classifier result: P4
Safety result:     P1
Routing sees:      P1
Destination:       Emergency Department
```

This ordering is critical. If Routing ran before Safety, a severe case could be sent to the wrong care tier.

## 16. HITL Uses the Safety Trigger

The Human-in-the-Loop worker runs after Routing.

Its decision logic is at `backend/app/agents/hitl.py:90`.

It escalates when any of these apply:

```text
Safety triggered
OR
Classifier confidence is below 0.5
OR
Cross-visit recurrence requires review
```

Therefore:

```python
state.safety_triggered = True
```

does more than change acuity. It also guarantees clinician review.

The escalation reason includes the Safety rule:

```text
Safety-override triggered (cardiac_chest_pain).
```

## 17. Reflection Reapplies Safety

Reflection is the final critic.

It checks for problems such as:

- P1 or P2 case not escalated.
- Care tier inconsistent with acuity.
- Routing announcement inconsistent with state.
- Thin evidence or very low confidence.

If Reflection requests a classifier rerun, the Supervisor does this at `backend/app/agents/supervisor.py:219`:

```text
Run Classifier again
Run Safety again
Keep the more severe acuity
Run Routing again
Run HITL again
```

Safety is reapplied so a classifier rerun cannot accidentally remove a previous safety escalation.

The Reflection rerun calls synchronous `safety.run()` without another LLM call. It reuses any semantic finding already saved in `state.semantic_flags`.

## 18. Chest-Pain Example

Consider:

```text
I have crushing chest pain going down my left arm.
```

The intended flow is:

1. Guardrail accepts the input.
2. Redaction masks identifiers, if present.
3. `CaseState` is created.
4. Safety prescreen sees `chest pain`.
5. `safety_fast_path` becomes `True`.
6. Intake normalizes the symptoms.
7. Classifier predicts P4 with confidence 0.72.
8. Classifier broadcasts `acuity.classified`.
9. Safety checks the deterministic rules.
10. `cardiac_chest_pain` matches.
11. Safety records prior acuity P4.
12. Safety forces acuity to P1.
13. Safety publishes `safety.override`.
14. Routing sees P1 and selects the Emergency Department.
15. HITL sees `safety_triggered=True`.
16. HITL creates a mandatory clinician escalation.
17. Reflection confirms P1 is routed and escalated consistently.
18. The final response contains P1 and the Safety explanation.

The critical part is:

```text
Classifier says P4
        |
        v
Safety independently finds chest pain
        |
        v
Final acuity becomes P1
```

## 19. Benign Example

Consider:

```text
I have a mild sore throat and runny nose.
```

The intended flow is:

1. Safety prescreen finds nothing.
2. Intake normalizes the symptoms.
3. Classifier assigns an acuity.
4. Safety rules find no red flag.
5. Semantic layer finds no known emergency category.
6. Safety records `triggered=False`.
7. Acuity remains unchanged.
8. Routing uses the classifier's acuity.
9. HITL only escalates if confidence is low or another rule applies.

Safety does not make every case urgent. It only changes the result when a known emergency signal is found.

## 20. Frontend Display

The frontend handles `safety_override` at `frontend/lib/useTriage.js:51`.

When triggered, the pipeline visualizer marks Safety as flagged and displays the acuity change.

The banner is rendered in `frontend/components/PipelineVisualizer.jsx:113`.

It can show something conceptually like:

```text
Safety override triggered
Prior acuity: P4
Forced acuity: P1
```

One caveat is that frontend stream errors fall back to an in-browser simulation in `frontend/lib/api.js:71`. Because the current backend fails at `prescreen()`, the simulation may hide the backend failure during a demonstration.

## Intended SSE Sequence

Once the missing Safety methods are implemented, the expected event order is approximately:

```text
case_open
guardrail
agent_message(case.opened)

agent_active(intake)
agent_result(intake)
agent_message(symptoms.normalised)

agent_active(classifier)
agent_result(classifier)
agent_message(acuity.classified)

agent_active(safety)
agent_result(safety)
safety_override
agent_message(safety.override)

agent_active(routing)
agent_result(routing)
agent_message(care.routed)

agent_active(hitl)
agent_result(hitl)
agent_message(review.decision)

agent_active(reflection)
agent_result(reflection)
agent_message(decision.reviewed)

final
```

## Current Reality

Phase 1 has restored the deterministic baseline. Both Safety methods are implemented, the authoritative gate is monotone, Reflection reapplication is idempotent, and Safety, contract, red-flag, pipeline, API, and full backend tests pass without model access. The semantic scaffold remains additive; later phases strengthen its schema, context handling, and model evaluation.

## Related Observation

Agents receive redacted `masked_text`, but the current persistence code assigns the original `req.text` to `CaseRecord.rawText` in `backend/app/main.py:191`. This is separate from Safety-Override, but it should be reviewed because it may persist identifiers that were removed before agent processing.

## Suggested Reading Order

Read these files in order for a manageable code walkthrough:

1. `backend/app/agents/base.py:112` - understand the shared `CaseState`.
2. `backend/app/redflags.py` - understand the rules and escalation-only merge.
3. `backend/app/agents/safety.py:59` - understand categories, LLM scaffolding, contracts, and missing methods.
4. `backend/app/agents/supervisor.py:256` - follow the exact execution order.
5. `backend/app/agents/hitl.py:90` - see how a Safety trigger creates clinician review.
6. `backend/app/agents/routing.py` - see how post-Safety acuity determines the destination.
7. `backend/app/main.py:89` - see how the request creates state, runs the Supervisor, saves the result, and sends SSE events.
8. `backend/tests/test_redflags.py` - read executable examples of the deterministic guarantees.
9. `backend/tests/test_pipeline.py` - see the intended end-to-end behavior and message order.

## Key Takeaways

- Safety-Override is an independent safety net, not the primary classifier.
- The early prescreen is only a fast hint; the later Safety step is authoritative.
- Safety runs after the Classifier and before Routing so downstream workers see the corrected acuity.
- A Safety trigger also causes mandatory clinician review through HITL.
- Reflection reapplies Safety if classification is rerun.
- The deterministic result is intended to remain authoritative even when NLP or LLM reasoning is added.
- The current branch completes this path deterministically; later phases add bounded multilingual NLP and LLM adjudication without replacing the rule floor.
