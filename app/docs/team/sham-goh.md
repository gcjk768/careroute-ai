# Sham Goh — Symptom-Intake & Orchestration

Back to [who did what](./README.md) · [project README](../../README.md)

## The job

Two roles in one. **Symptom-Intake** is the first agent to see a patient: it turns whatever they
typed — messy, abbreviated, possibly not in English — into one clean clinical sentence the rest of
the pipeline can reason about. **The orchestrator** then runs all seven agents in a fixed,
safety-gated order and assembles the final answer.

The orchestrator role used to live in a separate platform-owned `Supervisor` class. Sham moved it
into Symptom-Intake, so every worker is now called through an agent that declares its own identity
rather than through anonymous machinery.

## What it does

![Sham Goh](../diagrams/generated/member-sham-goh.png)

<sub>Source: [`member-sham-goh.mmd`](../diagrams/src/member-sham-goh.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

## What was delivered

**Input normalisation.** An LLM extraction step with a deterministic fallback, producing a clean
sentence, keywords and a detected language. Everything downstream reads this, not the raw text.

**The orchestrator (`PipelineOrchestrator`).** Step order, the safety gate, A2A plumbing and the
Reflection loop. Mixed into `SymptomIntakeAgent`, so `SLUG == "intake"` and its capability
classification is `ORCHESTRATOR`.

**A private worker set per request (`new_session()`).** This fixed a genuine bug, not a theoretical
one. The orchestrator used to be a process-wide singleton whose workers kept per-case state on
`self`, and `orchestrate()` suspends at every yield — so two patients being triaged at the same time
could interleave, and one patient's safety message could be assembled from another patient's run,
into the audit trail and the API response. Now each request builds its own workers, sharing only
what is expensive and case-independent (clinic data, hours snapshot, OneMap client, route cache, the
ML model).

**Inbox delivery (`_deliver`).** Before calling any worker, the orchestrator hands it a filtered
inbox of the messages it subscribed to — so agents reason about what peers *asserted*, not just
about leftover shared state.

## Files owned

| Path | What it is |
|---|---|
| [`app/agents/intake.py`](../../backend/app/agents/intake.py) | The Symptom-Intake agent |
| [`app/agents/orchestration.py`](../../backend/app/agents/orchestration.py) | `PipelineOrchestrator` — the machinery |

## Declared interface

| Property | Value |
|---|---|
| Autonomy | L2 as a worker, L3 orchestrating |
| Tools | `llm.complete` + orchestrator tools |
| Publishes | `symptoms.normalised` |
| Subscribes | `case.opened` |
| Capability | `ORCHESTRATOR` (+ agent) |

## Prove it

```bash
cd backend
pytest -m intake                              # the agent in isolation
pytest tests/agents/test_orchestrator_comms.py  # orchestration + messaging
pytest tests/agents/test_pipeline_isolation.py  # the cross-patient bleed guard
python scripts/show_a2a.py "chest pain"       # watch it drive the pipeline
```
