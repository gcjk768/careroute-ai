# The MLOps pipeline

Back to the [project README](../README.md) · full job-by-job detail in the
[Technical Reference](./TECHNICAL_REFERENCE.md#cicd--the-mlsecops-pipeline)

Every push runs a **10-stage GitLab pipeline**. The point of it is not automation for its own sake —
it is that **a model which fails a quality, safety or fairness threshold cannot reach production**,
and no human has to remember to check.

---

## The 10 stages

![The 10 pipeline stages](diagrams/generated/mlops-stages.png)

<sub>Source: [`mlops-stages.mmd`](diagrams/src/mlops-stages.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

The panel beside the pipeline says what each stage does and which ones block a release.

## What blocks a release

A **blocking** gate stops the pipeline before build or deploy. Everything else publishes its finding
but lets the release through.

| Gate | Threshold |
|---|---|
| `test:model-gate` | accuracy ≥ 0.75, **red-flag recall ≥ 0.95**, ECE ≤ 0.05, fairness gap ≤ 0.35 |
| `train:model` release gate | the same floors — so a failing model is never even persisted |
| `data:validate` | schema, domain, label balance, hash consistency |
| `test:data-lineage` | the dataset's hash **==** the model's training-data hash |
| `ai-security:fairness-gate` | subgroup accuracy parity within the same ceiling |
| `ai-security:guardrail-regression` | prompt injection blocked, safety override un-overridable |
| `test:backend` | 80% coverage floor |
| `lint:backend` | ruff, pinned to 0.16.3 |
| `scan:secrets-gitleaks` · `scan:trivy-fs` | committed secrets · CRITICAL CVEs |
| `ai-security:guardrail-score` | injection bypass ≤ 2%, recall ≥ 0.95, **false positives ≤ 0.05** |
| `scan:pii-egress` | zero identifiers in any published artifact |
| `scan:no-live-credentials` | no agent-reachable live credential outside production |
| `test:agent-graph` | every agent reachable in a real run; ≤ 9 worker steps per triage |

The last four came from the courseware pass (see [Courseware Alignment](vault/Courseware%20Alignment.md)).
`guardrail-score` is worth singling out, because it is the one that found something: the
`guardrail-regression` row above had always been green, since it asserts that *named payloads* are
blocked. Scoring a labelled corpus instead measures a **rate** — and the first run put the guardrail's
false-positive rate at **11%** against a 5% budget. Two patients were being turned away: someone who
had *"sprained my ankle playing football"* (the clinical lexicon had no joints or extremities at all,
so the off-scope layer saw a chat request) and someone who *"forget[s] everything when the migraine
starts"* (an imperative injection pattern matching a neurological red flag). Both fixed; the attack
forms still block. **A pass/fail gate measures the payloads someone thought of.**

---

## The closed drift→retrain loop

The part that makes this continuous rather than a one-off. Monitoring does not just report — a drift
breach fires a retrain, and the retrained model must clear the same gates before it can replace the
one in production.

![The drift to retrain loop](diagrams/generated/mlops-retrain-loop.png)

<sub>Source: [`mlops-retrain-loop.mmd`](diagrams/src/mlops-retrain-loop.mmd) — regenerate with `node scripts/render-diagrams.mjs`</sub>

The **loop guard** matters: a pipeline triggered by drift must not itself trigger another, or one
noisy day becomes an infinite retrain loop.

---

## The five pillars — and their honest status

| # | Pillar | Status |
|---|---|---|
| 1 | Experiment tracking + model registry | **Runs.** MLflow logs params/metrics and registers `CareRouteTriageRF` on every gated run — plus the **code version and environment** (git commit, Python, OS, library versions), without which a registered model cannot say which commit built it |
| 2 | Data versioning + lineage | **Runs.** Content-addressed dataset snapshot, DVC pointer committed, lineage proven by a blocking gate. The snapshot also records **statistics, splits and provenance** — a changed hash alone says *that* the data moved, never *how* |
| 3 | Monitoring | **Runs.** Drift reports, de-identified inference log, Prometheus metrics (now including **token and cost per model**), Alertmanager routing |
| 4 | Continuous training | **Configured.** Weekly schedule + drift trigger exist; needs `CAREROUTE_PIPELINE_TRIGGER_TOKEN` to fire. **All three drifts now trigger** — data, target and concept (an accuracy fall against the reference); previously only data drift did, so a shifted label mix passed as long as the inputs looked familiar |
| 5 | Deployment lifecycle | **Configured.** Staging shadow deploy, champion–challenger promotion, a 5/25/50/100 **canary with metric-breach auto-rollback**, and rollback — all defined but not yet exercised against a live environment |

Pillars 4 and 5 are written and wired but unproven in practice. Saying so is more useful than
claiming five green ticks.

---

## The current model

From `backend/models/model_audit.json`:

| Metric | Value | Gate |
|---|---|---|
| Overall accuracy | 0.917 | ≥ 0.75 |
| Red-flag recall | 0.957 | ≥ 0.95 |
| Calibration error (ECE) | 0.022 | ≤ 0.05 |
| Fairness gap | 0.549 → **0.170** | ≤ 0.35 |
| Counterfactual sex-flip rate | 0.0 | ~0 |
| Weakest subgroup | 65+ Female, 0.79 (n=86) | — |

> **Known issue:** this artifact was trained **2026-07-27** and predates the later agent work. It
> should be retrained — which is exactly what pillar 4 is for, once its trigger token is set.

## Run it locally

```bash
cd backend && pip install -r requirements.txt -r requirements-mlops.txt
python -m app.ml.export_dataset     # snapshot + content hash
python -m app.ml.validate_data      # the data-validation gate
python -m app.ml.train              # train, gate, persist, register
python -m app.ml.monitor            # drift + performance report
```
