---
tags: [roadmap, future, careroute]
updated: 2026-07-17
---
# Roadmap

Back to [[Home]]. Related: [[MLOps Pipeline]] · [[Loop Engineering]] · [[RAG and Onyx]] · [[App Overview]] · [[Changelog]]

Future movements. Update [[Changelog]] as each lands, and tick it here.

## Near-term (high value, low risk)
- [x] **Close the drift→retrain loop** — done 2026-07-17: `monitor.py` drift gate + GitLab trigger-API
  retrain (loop-guarded). Set `CAREROUTE_PIPELINE_TRIGGER_TOKEN` as a CI var to activate
  ([[Loop Engineering]] / [[MLOps Pipeline]] Pillar 4).
- [x] **Reflection loop stop rules** — iteration + budget caps + telemetry (done 2026-07-17, see [[Loop Engineering]]).
- [ ] **Grafana dashboard** over Prometheus `/metrics` — now includes MODEL metrics too
  (prediction mix / confidence / model version / HITL agreement), so the dashboard is worth more.
- [ ] **Wire MLflow → GitLab registry** in CI (set `MLFLOW_TRACKING_URI`/token as CI vars) — see [[MLOps Pipeline]].
- [ ] **Frontend: clinician finalAcuity picker** on the escalation decision form (the API + ground-truth
  loop already accept it; the staff portal just doesn't send it yet).
- [ ] **Clinician handoff summary in the dashboard** — see [[Clinician Handoff UI]] for the frontend plan;
  blocked on `handoff.py` being merged into the pipeline + `EscalationDetail`/`store.py` carrying the
  three new fields.

## Mid-term
- [ ] **Onyx CE integration** — stand up the service + connectors for real clinical corpora ([[RAG and Onyx]]).
- [ ] **pgvector/Chroma retrieval** as a lighter alternative to Onyx.
- [ ] **DVC remote** — `dvc init` + local pointer done 2026-07-17; still to do: push the snapshot to a
  real remote (local/SSH/S3) for team sharing.

## When-to-adopt (deliberately deferred — not now)
- **Feast (feature store)** — when features are served online / shared across models.
- **Kubeflow / SageMaker / Vertex Pipelines** — when training/serving needs elastic (GPU) scale.
- **ELK aggregated logging** — when many agents/services need centralized log search.

## Grading narrative to lean on
Frame the whole system as **[[Loop Engineering]]** (trigger → goal → deterministic verifier → stop rules),
back it with the [[MLOps Pipeline]] gates, and cite the [[RAG and Onyx]] upgrade path — a current,
defensible story grounded in the course toolchain ([[MLOps Pipeline]]).
