# Bundle A — Responsible-AI + security hardening (courseware gap closure)

Date: 2026-09-16. Branch: `integration-all-agents-2026-08-31`. Owner: James.

## Why

A four-module audit of the application against the NUS-ISS courseware
(Explainable & Responsible AI, AI & Cybersecurity, Architecting Agentic AI,
Deploying & Operating AI) found that the deterministic, gated engineering is
strong but several things the lectures treat as core practice are missing or
only claimed in docs. Bundle A is the subset that needs no new infrastructure.

## Scope (nine items, in build order)

| # | Item | Courseware anchor | Where |
|---|------|-------------------|-------|
| 6 | Embedded-instruction guard in every LLM system prompt, asserted by a test | AIC Day 2 LLM01 mitigation 1 | `agents/base.py`, `classifier.py`, `handoff.py`, `routing.py`, `tests/agents/test_prompt_hygiene.py` |
| 5 | Screen retrieved (Onyx) and remembered (prior-visit) text for indirect injection | AIC Day 2 IPI / RAG poisoning activities, LLM04/LLM08, ASI06 | `rag.py`, `main.py`, `metrics.py`, `tests/test_rag_injection.py` |
| 7 | Real clinician identity + final acuity on a decision | XRAI Day 1 accountability; PDPC human involvement | `ClinicianDashboard.jsx`, `tests/test_store.py` |
| 8 | Live labelled metrics: join `ground_truth.jsonl` to the inference log by case id | XRAI Day 1 deployment review; DOAIS logging & monitoring | `agents/base.py` (case_id), `main.py`, `ml/model.py`, `ml/inference_log.py`, `ml/monitor.py` |
| 4 | 5-fold stratified CV (mean ± std) in the training audit | XRAI Day 1 model building | `ml/model.py`, `models.py`, `MODEL_CARD.md` |
| 2 | SHAP vs local-surrogate agreement audit (replaces dead LIME path) | XRAI Day 2 LIME vs SHAP, faithfulness/stability | `ml/model.py`, `ml/train.py` |
| 1 | Single-edit counterfactual explanation surfaced to the patient | XRAI Day 2 "why-not / how-to-be-that"; PDPC 5.5 | `ml/model.py`, `classifier.py`, `base.py`, `models.py`, `main.py`, `PatientTriage.jsx` |
| 3 | Raise-only, group-specific severe threshold (post-processing) + gated Equal Opportunity gap | XRAI Day 2 pre/in/post-processing mitigation | `ml/fairness.py`, `ml/model.py`, `ml/train.py`, `tests/test_fairness_gate.py` |
| 9 | Correct documentation claims the code does not back; redact feedback/notes before storage | all modules | `SECURITY.md`, `ASPECTS.md`, `GOVERNANCE.md`, vault notes, `MODEL_CARD.md`, `main.py` |

## Design decisions

- **Counterfactual search is single-edit and bounded.** One batch prediction
  over every symptom-flag toggle; demographic features are never toggled.
  Returns the smallest change in each direction (more urgent / less urgent)
  plus a plain sentence. Deterministic; no optimisation library.
- **LIME is replaced, not installed.** The `lime` package was never in
  requirements, so the per-prediction `lime_explanation` was always empty.
  A LIME-style local linear surrogate (perturbed neighbourhood, distance-
  weighted ridge) is implemented in-repo and used only for the training-time
  agreement audit (top-3 overlap + Spearman vs SHAP over held-out rows).
- **Post-processing is deterministic and raise-only.** Fairlearn's
  ThresholdOptimizer randomises predictions to reach exact parity; a
  patient's acuity must not depend on a coin flip, and lowering acuity would
  conflict with the monotone-escalation safety rule. Per-group thresholds on
  calibrated P(severe) are derived on the training split and can only raise
  a P3–P5 argmax to P2. Fairlearn `MetricFrame` verifies the result in the
  gate test.
- **Gates are absolute and imported.** `MAX_EQUAL_OPPORTUNITY_GAP` and
  `MIN_EXPLANATION_AGREEMENT` live in `ml/model.py`, are enforced in
  `train.validate` before persistence, and are imported (not restated) by
  the CI tests.
- **Artifact schema bumps 2 → 3.** The payload gains the group thresholds and
  loses the LIME background sample; an old artifact is rejected on load and
  rebuilt.
- **Case id plumbing.** `CaseState.case_id` is set by `main.py` before any
  agent runs; the classifier passes it to `predict`, which logs it. Nothing
  else reads it.

## Testing

Every item is test-first. New tests: `tests/agents/test_prompt_hygiene.py`,
`tests/test_rag_injection.py`, `tests/test_counterfactual.py`,
`tests/test_explanation_agreement.py`, `tests/test_cross_validation.py`,
`tests/test_live_labelled_monitor.py`, plus additions to
`tests/test_fairness_gate.py`, `tests/test_store.py`, `tests/test_model_gate.py`.
The full backend suite must stay green (977 passed at the start of the bundle).

## Out of scope (other bundles)

LLM-decided control flow, embedding RAG, tool registry/gateway, LLM router,
CD pipeline, MLflow registry serving, live LLM evals, impact assessment and
AI security policy documents, patient review-request UI.
