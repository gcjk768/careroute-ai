---
tags: [mlops, cicd, careroute]
updated: 2026-09-12
---
# MLOps Pipeline

Back to [[Home]]. Related: [[App Overview]] · [[Loop Engineering]] · [[Roadmap]] · [[Changelog]] ·
[[Courseware Alignment]] (gate-by-gate mapping onto the DOAIS courseware)

The GitLab CI/CD pipeline ([`.gitlab-ci.yml`](../../.gitlab-ci.yml)) implements the full toolchain the
*Integrating & Deploying AI Solutions* module prescribes. Stages:
`lint → train → test → monitor → ai-security → security-scan → report → build → deploy → post-deploy`
(10 stages; `report` runs after `security-scan` so its PDF/Markdown can summarize the scan findings).
Pipeline hygiene: `workflow:` rules de-dupe branch+MR pipelines; jobs are `interruptible`.

## The five MLOps pillars (implemented)
1. **Experiment tracking + Model Registry** — MLflow in [`ml/train.py`](../../backend/app/ml/train.py);
   registers `CareRouteTriageRF`. With `MLFLOW_TRACKING_URI` set it writes there (e.g. GitLab's
   MLflow-compatible registry); unset, it falls back to a local SQLite store at
   `backend/mlflow.db`. **Not `./mlruns`** — mlflow 3.x (pinned 3.13.0) put the filesystem store in
   maintenance mode and refuses it, so a database backend is now required.
   Each run also records the two things the tracking deck asks for beyond metrics —
   **code version and environment** ([`ml/run_context.py`](../../backend/app/ml/run_context.py): git
   commit/branch/dirty, Python, OS, library versions). Without the commit, tracing a misbehaving
   registered model backwards stops at a version string.
2. **Data versioning + lineage** — [`ml/export_dataset.py`](../../backend/app/ml/export_dataset.py) +
   **real DVC** (`.dvc/` initialized; `backend/data/triage_dataset.npz.dvc` pointer committed); the
   `data:validate` gate checks schema/quality, and `test:data-lineage` proves the model trained on
   exactly the versioned dataset (dataSha256 match). See [[App Overview]] (integrity hashes).
   The snapshot metadata records what the data-versioning deck asks for — **schema, statistics,
   splits and provenance** (per-feature min/max/mean/std, label + subgroup balance, the
   0.25/stratified/seed-42 split). All of it **deterministic**: the file is git-committed and
   re-derived by `--check`, so a timestamp or git SHA in it would dirty the file on every export.
3. **Monitoring** — Evidently drift + performance report in
   [`ml/monitor.py`](../../backend/app/ml/monitor.py) (PSI fallback). Headline data drift is the
   **worst per-feature PSI**, not the average: 27 of 29 features are binary flags, and averaging
   over them divided a real shift by ~29 and buried it under the 0.25 gate (the binary branch of
   `population_stability_index` was also dead, scoring a 5%→95% flip as exactly 0.0). The report
   records **which backend actually ran and why** (`backend` + `backendStatus`) — in this venv
   `import evidently.report` raises a TypeError, so the named primary backend was never running
   while the log said "not available". Serving writes a de-identified
   **inference log** ([`ml/inference_log.py`](../../backend/app/ml/inference_log.py)) + model-level
   Prometheus metrics; `CAREROUTE_MONITOR_SOURCE=live` monitors real traffic. Feeds the CT loop.
4. **Continuous Training** — `train:model` fires on a GitLab **scheduled pipeline**, and the
   **drift→retrain loop is CLOSED**: a drift breach (`CAREROUTE_DRIFT_THRESHOLD`) fires a retrain
   pipeline via the trigger API (`CAREROUTE_PIPELINE_TRIGGER_TOKEN`; loop-guarded). The **HITL
   ground-truth log** (clinician `finalAcuity` → `ground_truth.jsonl` + agreement metric) supplies
   real labels — the "hook loop" in [[Loop Engineering]] is live.
   **All three drifts trigger, not just data drift.** The CI/CD deck names dips in accuracy, data,
   target and concept drift; only the first gated. Target drift (PSI ≥ 0.25) and an accuracy fall
   against the reference (≥ 0.10, i.e. concept drift) were computed, published, then ignored — so a
   shifted label mix or a collapsed accuracy passed the gate as long as the *inputs* looked familiar.
   The headline metric still fails **closed** when missing; the secondary signals fail **open**, since
   "target drift was never computed" must not read as "the labels have drifted".
5. **Deployment lifecycle** — staging/production `environment:` blocks, a **champion–challenger gate**
   on promotion (candidate metrics must be ≥ the Production champion's), MLflow
   **Dev→Staging→Production** stage transitions, and rollback (registry stage change + version pin).
   Containers ship from **this pipeline** onto the infra repo's ECS Fargate stack:
   `deploy:push-images` → `deploy:ecs` (automatic on `release/deploy`, manual on `main`) ([`scripts/deploy_ecs.sh`](../../scripts/deploy_ecs.sh):
   new task-definition revision with the tag, update-service, wait, verify). ECS rolls back **on its own** —
   circuit breaker for tasks that never start, a 5xx-rate CloudWatch alarm (the 1% of
   [`ml/canary.py`](../../backend/app/ml/canary.py)) for tasks that start and then fail — and `deploy:ecs`
   fails when that happens. `rollback:production` runs the same script onto the previous verified tag
   (SSM release history) and re-pins the previous model. Terraform still owns the infrastructure. The rollout is **rolling**: native blue/green or canary
   needs AWS provider 6.x there, and canary.py's tool-failure gate is not yet a CloudWatch alarm.
   Configured, not yet applied. `notify:pipeline-outcome` closes the feedback half of CI/CD.

## Gates that block release (the deterministic verifiers)
These are the [[Loop Engineering]] verifiers of the CI loop — objective pass/fail the model can't argue with:
- `lint:backend` — ruff, blocking.
- Training-time gate in [`ml/train.py`](../../backend/app/ml/train.py) — acc ≥ 0.75, red-flag
  recall ≥ 0.95, subgroup fairness gap ≤ 0.35 (**absolute**, not just "no worse than the age-blind
  baseline") and calibration ECE ≤ 0.05. **Same thresholds as CI** — they are imported from
  `model.py`, not re-typed — so a failing model is never persisted/registered.
- `data:version` — `export_dataset --check` first: fails if the code no longer reproduces the
  **committed** snapshot, *then* re-exports. (Without that ordering the pipeline regenerated the
  dataset and compared it to itself.)
- `data:validate` — dataset schema / value domain / label balance / hash consistency.
- `test:model-gate` — accuracy ≥ 0.75, red-flag recall ≥ 0.95 (safety-critical), fairness gap
  ≤ 0.35, ECE ≤ 0.05 — and it asserts the model was **loaded from the persisted artifact**
  (`TriageModel.loaded_from_artifact`). `get_model()` silently trains a fresh model when none
  loads, so the gate previously passed while grading a model it had just built itself.
- `test:data-lineage` — **committed** dataset hash (`git show HEAD:...`) == model's training-data hash.
- `test:backend` — coverage floor (`--cov-fail-under=80`).
- `ai-security:fairness-gate` — Fairlearn subgroup parity (no regression to the age-blind baseline).
- `scan:secrets-gitleaks` (blocking) and `scan:trivy-fs` (blocks on CRITICAL CVEs).
- `deploy:promote-production` — champion–challenger regression check before registry promotion.
- `ai-security:guardrail-score` — **scored**, not pass/fail: injection bypass ≤ 2%, recall ≥ 0.95,
  false positives ≤ 0.05 over a 55-case labelled corpus. The false-positive half is the half that
  bites — it caught the guardrail rejecting a sports injury and a patient describing memory loss.
- `scan:pii-egress` — zero identifiers in any published artifact. Runs in the `report` stage because
  it audits what has already been published, the executive report included.
- `scan:no-live-credentials` — no agent-reachable live credential outside production.
- `test:agent-graph` — every declared worker reachable from a real `orchestrate()` run; ≤ 9 worker
  steps per triage (measured 6–7).
- `test:load-locust` — **advisory, deliberately**. The locustfile computes a real verdict (p95 + error-rate
  SLOs, non-zero exit on breach) but the job carries `allow_failure: true` because no baseline on CI
  hardware exists yet; a threshold guessed before the first measurement is a flaky gate, not a gate.
  Promoting it is deleting that one line — the way `test:e2e` was promoted on 2026-09-02.

## Runtime evidence + the expanded scanner set (2026-09-12)

The pipeline grew from 38 to **62 jobs** (`security-scan` 8 -> 29, `test` 8 -> 10,
`post-deploy` 1 -> 2). Three changes worth knowing, because each fixed a *structural* reason evidence was
missing rather than adding a tool for its own sake:

1. **Runtime tests that need no deployment.** `test:load-locust` (Locust, SLO check in
   [`tests/load/locustfile.py`](../../backend/tests/load/locustfile.py)) and
   `test:api-fuzz-schemathesis` (property-based fuzzing off our own OpenAPI schema) **boot the backend
   inside the job**, using the recipe `test:e2e` already proves works — no LLM provider is configured in
   CI, so the agents take their deterministic path and the app is healthy without any secret. That is what
   makes them produce evidence on *every* pipeline instead of waiting on a deployed target, which is the
   mistake that left `dast:owasp-zap` dormant.
2. **The DAST jobs moved into `security-scan`.** ZAP-api, ZAP-full, Nikto and Nuclei each boot their own
   disposable backend, so they never needed a deployment — and `report` runs *before* `post-deploy`, so
   results produced there could never have reached the PDF. Only the two jobs that genuinely need a live
   target (`dast:owasp-zap`, `loadtest:staging`) remain in `post-deploy`.
3. **GitLab's own SAST + Secret Detection are included** (`Jobs/SAST`, `Jobs/Secret-Detection`). Both
   analyzers **run on the Free tier** — it is the Security Dashboard and the MR widget that need Ultimate,
   not the jobs — so their `gl-*-report.json` artifacts are real evidence. They are additional engines
   alongside Semgrep / Bandit / gitleaks, not replacements.

[`ml/report.py`](../../backend/app/ml/report.py) parses all of it: **SARIF** (checkov, kics, njsscan,
eslint, osv, grype, dependency-check), the two **GitLab** report formats, and `locust-summary.json`. Two
behaviours it is careful about — a scanner whose artifact is **absent is reported as skipped, never as
clean**, and a **TruffleHog `verified` secret is flagged blocking** (verified means the key actually
authenticates, not that it looks like a key).

The deliberate overlap (four secret scanners, four dependency scanners, two IaC, four DAST) buys different
rule corpora and advisory databases; the cost is wall-clock, and a cold `scan:deps-dependency-check`
downloads the whole NVD feed. Full per-scanner table in the repo [`README.md`](../../README.md).

## Course toolchain mapping (Note 02)
Git/GitLab ✓ · GitLab CI/CD ✓ · Feast ⏸ (deferred) · MLflow ✓ · Kubeflow ⏸ (GitLab DAG stand-in) ·
Pytest ✓ · Docker ✓ · FastAPI ✓ · Evidently + Prometheus/Grafana ✓. Full detail in the repo
[`README.md`](../../README.md) → "MLOps course alignment".

## Run locally (from `backend/`)
```
pip install -r requirements.txt -r requirements-mlops.txt
python -m app.ml.train            # pillar 1: train + gate + register
python -m app.ml.export_dataset   # pillar 2: version the dataset
python -m app.ml.monitor          # pillar 3: drift + performance report
python -m app.ml.report           # executive PDF + Markdown twin
# load test (needs the backend running on :8000, and `pip install locust`)
locust -f tests/load/locustfile.py --headless -u 20 -r 5 -t 60s --host http://127.0.0.1:8000
mlflow ui --backend-store-uri sqlite:///mlflow.db   # browse the local run store
```
`mlflow ui` on its own will NOT find these runs: it defaults to `./mlruns`, while training now
writes to `backend/mlflow.db` (see pillar 1 above).

## The 10-stage pipeline

Diagrams and the gate table live in [docs/MLOPS.md](../MLOPS.md) so they render on one page
(GitLab draws at most ~2000 characters of Mermaid per page).
