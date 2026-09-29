# Koh Guan Chin James — Severity-Classifier, ML/MLOps & Platform

Back to [who did what](./README.md) · [project README](../../README.md)

## The job

Three things. The **Severity-Classifier** agent — the only worker in the system backed by a trained
model. The **ML and MLOps stack** behind it: training, explainability, the fairness audit, drift
monitoring and the CI/CD pipeline. And the **agent platform** every other member builds on top of:
the contract system, the message bus, the guardrails and the audit log.

## What it does

![James Koh](../diagrams/generated/member-james-koh.png)

<sub>Source: [`member-james-koh.mmd`](../diagrams/src/member-james-koh.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

## What was delivered

**A real model, gated on real numbers.** A RandomForest with isotonic calibration. Current artifact:
accuracy **0.917**, red-flag recall **0.957**, calibration error **0.022**, fairness gap **0.549 →
0.170** after mitigation, counterfactual sex-flip rate **0.0**. A model that misses any release
threshold is never persisted or registered — the training run fails first.

**Genuine explainability.** `shap.TreeExplainer` produces signed, urgency-oriented contributions over
the symptoms the patient actually reported, surfaced to both patient and clinician. Not a mock.

**A fairness audit that had something to fix.** The dataset carries a deliberate, clinically realistic
bias — the 65+ band is under-represented — so mitigation is measurable rather than decorative.
Reports accuracy gap, demographic parity, equal opportunity and a counterfactual sex flip.

**Two defects found and fixed in his own work, kept visible:**
- *Drift detection could never fire.* PSI was exactly 0.0 for every binary feature (27 of 29) because
  quantile edges collapsed to a single bin, and the gate averaged across all features. Binary shifts
  now score properly and the gate reads the **maximum** per-feature PSI.
- *The model was blind to minor trauma.* Evaluation E5 found no minor-trauma features at all, so cuts
  and sprains all scored an identical 0.448 confidence and were silently escalated. Now measured as
  `zeroCoverageRate` and deliberately **reported, never gated** — gating it would have made the
  defect look intentional.

**The platform.** `AgentContract` lane enforcement, the typed A2A `MessageBus` and `COMMS`
least-privilege interface, the capability audit (you cannot call yourself an agent without an
inference step and a real action space), the six-layer input guardrail, output guardrail, PII
redaction, and the SHA-256 hash-chained audit log.

**The pipeline.** The 10-stage GitLab MLSecOps pipeline and its blocking gates — see
[MLOps](../MLOPS.md).

## Files owned

| Path | What it is |
|---|---|
| [`app/agents/classifier.py`](../../backend/app/agents/classifier.py) | The Severity-Classifier agent |
| [`app/ml/`](../../backend/app/ml/) | Model, training, fairness, drift, monitoring, report |
| [`app/rag.py`](../../backend/app/rag.py) · [`rag_onyx.py`](../../backend/app/rag_onyx.py) | Citation retrieval |
| `agents/base.py`, `capability.py`, `messaging.py`, `reflection.py` | The agent platform |
| `guardrail.py`, `redact.py`, `audit.py`, `metrics.py`, `llm.py` | Cross-cutting services |

## Declared interface

| Property | Value |
|---|---|
| Autonomy | L2 |
| Tools | `ml.predict`, `llm.complete`, `rag.retrieve` |
| Publishes | `acuity.classified` |
| Subscribes | `symptoms.normalised` |
| Capability | `AGENT` — the **only** one with `uses_trained_model=True` |

## Prove it

```bash
cd backend
pytest -m classifier          # the agent in isolation
pytest tests/test_ml.py tests/test_fairness.py
pytest tests/test_model_gate.py     # the blocking release gate
pytest -m capability          # agent vs policy node, enforced
curl localhost:8000/api/fairness    # the live audit
```
