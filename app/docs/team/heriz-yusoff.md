# Heriz Yusoff — Human-in-the-Loop & Clinician-Handoff

Back to [who did what](./README.md) · [project README](../../README.md)

## The job

The part that keeps a human in charge. **Human-in-the-Loop** decides when the system must stop
deciding and ask a clinician. **Clinician-Handoff** then writes the packet that clinician reads —
and it only runs when a case is actually escalated, so the expensive summarisation never happens on
a routine sore throat.

## What it does

![Heriz Yusoff](../diagrams/generated/member-heriz-yusoff.png)

<sub>Source: [`member-heriz-yusoff.mmd`](../diagrams/src/member-heriz-yusoff.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

## What was delivered

**The escalation decision.** Escalates on low classifier confidence or a safety trigger. Deliberately
a **policy node**, not an LLM call — the rule for "get a human" should be inspectable and stable.
Its declared `upgrade_path` records what genuine agency would require.

**The Clinician-Handoff agent.** An L2 agent that summarises the case for a clinician, grounded in
retrieval, with a deterministic template as fallback. Wired into the pipeline to run only on the
escalation branch, subscribing to `safety.override` and `care.routed`.

**Closing the learning loop.** The clinician's recorded `finalAcuity` is written to a ground-truth log
with agreement metrics — real labels, from a real expert, for measuring live accuracy. This is what
makes the HITL step more than a safety net.

**Two evaluations, and an honesty fix worth noting.** E8 checks the handoff summary is *faithful* —
that it does not assert things the case does not support. E2 checks the clarifying questions are
appropriate, with a gold fixture. E8 was originally carried as a local, unregistered "E7" inside a
test file, which collided with Aaron's registered E7-safety-context — two different evaluations both
calling themselves E7. Renumbered rather than left to confuse a reader.

## Still open — stated plainly

**A patient cannot yet answer a clarifying question.** The agent can *ask* one, and E2 evaluates the
asking, but there is no resume endpoint, so the handshake dead-ends. The spec exists
([Clarification Resume API Handoff](../vault/Clarification%20Resume%20API%20Handoff.md)); the
implementation does not. This is the largest functional gap in the project.

## Files owned

| Path | What it is |
|---|---|
| [`app/agents/hitl.py`](../../backend/app/agents/hitl.py) | The Human-in-the-Loop policy node |
| [`app/agents/handoff.py`](../../backend/app/agents/handoff.py) | The Clinician-Handoff agent |

## Declared interfaces

| Agent | Autonomy | Tools | Publishes | Subscribes | Capability |
|---|---|---|---|---|---|
| Human-in-the-Loop | L2 | `escalation.create` | `review.decision` | `acuity.classified`, `safety.override`, `care.routed` | `POLICY_NODE` |
| Clinician-Handoff | L2 | `llm.complete`, `rag.retrieve` | `handoff.ready` | `safety.override`, `care.routed` | `AGENT` |

## Prove it

```bash
cd backend
pytest -m hitl                         # escalation decisions
pytest -m handoff                      # the packet
pytest tests/agents/test_handoff_pipeline.py
pytest tests/test_eval_handoff.py tests/test_eval_clarifying_questions.py

python scripts/show_clarification_handshake.py   # the ask/resume handshake
python scripts/smoke_test_handoff_llm.py         # against a real LLM
```
