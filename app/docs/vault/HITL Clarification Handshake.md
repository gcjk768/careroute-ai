---
tags: [handoff, active, careroute]
updated: 2026-08-18
---
# HITL Clarification Handshake — for Heriz

Back to [[Home]]. Related: [[Clarifying Questions Design]] · [[Agent Capability Audit]] · [[Evaluation Plan]] · [[Changelog]]

**From:** James (Severity-Classifier owner) → **To:** Heriz (HITL owner)
**Status:** my side is done and delivered; your side is not started.

The Severity-Classifier now proposes ONE clarifying question when it is not confident,
and **that proposal is already arriving in your agent's inbox**. Nothing reads it yet, so
today it is discarded and the case escalates. This note is what you need to act on it —
in your own branch, in your own files. I have not touched `hitl.py`.

---

## 1. What is already true (verified, not assumed)

Run this yourself from `backend/` — the script arrives on your branch via
[[HITL Handshake Runbook]] Step 1, so do that first if the file is not there yet:

```bash
C:/venvs/careroute/Scripts/python.exe scripts/show_clarification_handshake.py
```

Output today:

```
classifier -> P3_URGENT 0.427 | threshold 0.5
proposal   -> {'feature': 'cold_symptoms', 'label': 'cold symptoms', 'question': 'Do you also have a fever or any difficulty breathing?', 'gain': 2.482}
hitl inbox -> 1 msg; clarification visible = True
hitl action-> {'source': 'deterministic', 'escalated': True, 'reason': 'Classifier confidence 0.43 is below the 0.50 escalation threshold.'}
```

The proposal is **visible to your agent and ignored by it**. That is the whole gap — there
is no missing interface, no wiring left to do on the bus. Two tests in
`tests/agents/test_classifier.py` (`test_the_proposal_arrives_intact_in_hitls_inbox`,
`test_hitl_can_tell_declined_to_ask_apart_from_nothing_received`) hold the delivery, and
both were confirmed to fail when the message is mis-addressed or the payload is trimmed —
so if delivery ever breaks, it breaks in my file, not yours.

## 2. The payload contract

`received_payload("acuity.classified")["clarification"]` is either `None` or this dict:

| Key | Type | Meaning | Why you need it |
|---|---|---|---|
| `feature` | `str` | which symptom category is missing (e.g. `cold_symptoms`) | lets you record WHICH gap an interruption closed |
| `label` | `str` | human-readable name of that gap | what a clinician reads in the queue |
| `question` | `str` | the exact question to put to the patient | what to ask |
| `gain` | `float` | expected information gain — how far the answer could move the decision | **whether asking is worth interrupting a patient** |

**`None` is published explicitly, never omitted.** It means "I looked and decided no
question is worth asking" — escalate, exactly as today. Do not treat a missing key and a
`None` value as the same thing: an absent key means you are talking to an old classifier,
and that should not silently look like a considered decision.

`gain` is on one scale regardless of which path produced it (trained model or keyword
table), so a threshold you pick holds in both deployments. For reference,
`MIN_INFORMATION_GAIN = 0.15` in `classifier.py` is the floor below which I do not
propose at all — anything reaching you already cleared it.

## 3. What to do in `hitl.py`

Read the proposal inside `run()`, guarded by `_consumed` so your existing standalone unit
tests (which run with no bus) are unaffected:

```python
proposal = None
if getattr(self, "_consumed", False):
    payload = self.received_payload("acuity.classified") or {}
    proposal = payload.get("clarification")
```

`ReflectionAgent` (`reflection.py:127`) already does exactly this — copy that shape rather
than inventing a second one. `received_payload` enforces your `COMMS.subscribes`
declaration, and `acuity.classified` is already in it, so no COMMS change is needed.

Then widen the action space from escalate / proceed to **escalate / ask / proceed**.

## 4. The constraints I think are non-negotiable

These are safety properties, not preferences — E5 gates on red-flag recall AND
specificity ≥ 0.80, and asking must not cost you either:

1. **Asking may only ever replace a low-confidence escalation (your rule 2).** Never a
   `proceed` — turning a proceed into a question invents an interruption from nothing, and
   would show up as a specificity loss on E5.
2. **Never over a hard floor.** `safety_triggered` and `cross_visit_escalation` escalate,
   full stop. Asking a patient a follow-up while they describe chest pain is the one
   failure mode this must not have. (I already refuse to propose when
   `safety_triggered`/`safety_fast_path` — but do not rely on my restraint for your floor.)
3. **Never over an escalation set upstream of you.** You added this floor after my merge and
   I had missed it: an incoming `state.escalated == True` — today only Supervisor's Safety
   A2A fail-safe, `_apply_safety_failsafe` — is un-clearable, so asking must not replace it
   either. Your branch is right and my read of the floors was one commit stale.
4. **Never at P1/P2**, even absent a safety trigger.
5. **Never twice.** The one-round cap rides on the request (`state.clarifications`), not on
   server state, so the loop cannot run away.

If the loop is strictly additive in this way, E5 recall on `must_escalate` is unaffected
**by construction** — worth stating in your commit message, because it is the thing the
reviewer will ask.

## 5. The decisions that are genuinely yours

- **The `gain` threshold above which asking beats escalating.** I have no basis to pick
  this: it is a triage-workload judgement (how much clinician queue relief justifies how
  much patient friction), not a modelling one. I used `1.0` in a throwaway prototype and I
  do not defend it.
- **Which acuity codes are off-limits for asking.** I assume P1/P2; you own the floor.
- **What happens to a declined proposal** — I would return it as `declinedQuestion` rather
  than drop it, so the audit trail shows a question existed and was refused. Your call.
- **Whether HITL stays a POLICY_NODE.** My read: yes. Three outcomes instead of two, but
  the choice between them is still a threshold on a number a peer computed, not inference.
  The genuine upgrade in your `upgrade_path` is the memory half — reading
  `monitoring/ground_truth.jsonl` back. Worth being straight about this in the audit
  rather than claiming agency the code does not have.

## 6. Acceptance tests to add to `tests/agents/test_hitl.py`

Your file, so I have not added them. These are what I would expect to see green:

- a low-confidence case with a proposal above your threshold → action is `ask`, and the
  question returned is the classifier's, not one HITL invented
- the same case with `safety_triggered=True` → escalates, question ignored
- the same case at P1/P2 → escalates
- a case with `state.clarifications` already populated → escalates, never asks twice
- a proposal below your `gain` threshold → escalates
- `clarification: None` → escalates, unchanged from today
- run with no bus at all (`_consumed` false) → behaviour byte-identical to today

The last one matters most: it is what proves you have added a path rather than changed
the existing one.

## 7. Full disclosure — the prototype branch

I already built this end-to-end on a local branch (`feature/hitl-handoff-tweaks`) before
we agreed it should be yours. It edits `hitl.py` and `handoff.py` and picks two constants
on your behalf. **It has never been pushed** and I am not proposing to merge it. It exists
if you want to read it as a reference, and it should be deleted once your version lands.
Ask me and I will push it to a `spike/` branch; otherwise it stays local.

## 8. What unblocks when this lands

[[Evaluation Plan]] **E2 (Appropriateness of clarifying questions)** — owner: you, agent:
`hitl` — currently cannot run because no part of the app asks a patient anything. It is
the last evaluation with no result. The API field (`TriageRequest.clarifications`), the SSE
event and the patient-facing resume UI are still unowned; happy to take those once your
half sets the shape.
