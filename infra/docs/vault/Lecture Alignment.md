---
tags: [active, careroute, infra]
updated: 2026-09-17
---
# Lecture Alignment — this repo vs the DOAIS decks

Back to [[Home]]. Related: [[Roadmap]] · [[App Overview]] · [[Monitoring Stack Design]] ·
[[Changelog]] · app vault: `../../../careroute_ai_app/docs/vault/Lecture Alignment.md`

The app vault's note of the same name maps the decks to **application** code. This
one maps them to **infrastructure**, which is where four of that note's seven gaps
were parked. Source: `NUS-ISS/Architecting AI Systems/Deploying and Operating AI
Solution/Courseware/*.pdf`, read 2026-09-17.

Deck shorthand used below: **CD** = `02. CD.pdf`, **Agentic** =
`03. CICD_for_Agentic_AI_Solutions.pdf`, **Platform** = `04. ML with platform.pdf`,
**Serving** = `08. Model_Deployment_and_Serving.pdf`, **SMMonitor** =
`MonitoringWithSageMaker.pdf`, **LLMSecOps** = `09. LLMSecOps_v0.6.pdf`.

## Bottom line

The IaC pipeline is *ahead* of what the decks teach. The **deployed runtime** is
behind them in three specific ways, all cheap to close: the release artifact is
mutable and hand-pushed, a bad deploy cannot roll itself back, and nothing runs on
a schedule. One risk the [[Roadmap]] flagged as hypothetical has already happened.

---

## 1. Config drift has already occurred — not hypothetical

[[Roadmap]] listed "Monitoring config drift" as a *blocked on a decision* item.
Diffed on 2026-09-17:

| File | App repo | `modules/monitoring_stack/config/` |
|---|---|---|
| `alertmanager.yml` | — | identical |
| `careroute-triage.json` | 6 panels | identical |
| `alert.rules.yml` | 2026-09-16 | **74 lines behind** |

The infra copy is missing the **entire `careroute-llm` alert group** — all four
rules: `LLMCostPerTriageHigh`, `LLMTokenBurnHigh`, `HighLLMLatencyP95`,
`LLMProviderFailureRateHigh`.

Why this one matters more than a normal stale file: those four rules are how the
app repo closed its Gap 2, and LLMSecOps spends ~15 slides on exactly this
(cost per token, quota alerts, cost guards). So the deployed stack claims the
LLMSecOps cost-and-quota alerting and does not have it. Prometheus loads the file
without complaint — a missing alert group is silent by construction.

**Fix:** re-copy the file, then add a CI job that `diff`s the two copies and fails.
Cross-repo file reference is what this repo cannot do; a diff against a pinned app
commit is what it can.

## 2. The release artifact is mutable and hand-pushed

CD p10 names "manual release steps are error-prone" as the first symptom of no CD.
CD p11: "create the deployable artifact and promote it across all environments."
Agentic p8: "the same tested container image is promoted through each environment —
do not rebuild separately." Agentic p10: "push to registry — **immutable release
unit**." Serving p11 best practices: "immutable images."

What we have:

- The app's `build:images` job builds both images, `docker save`s them to tarballs
  for Trivy, and **never pushes to a registry**. `deploy:terraform-plan` is an
  `echo`.
- `README.md:168-177` therefore documents a manual `docker build && docker push`
  from a laptop as the way images reach ECR.
- `live/demo/terragrunt.hcl` pins `image_tag = "latest"`, and `modules/ecr` defaults
  `image_tag_mutability = "MUTABLE"`.

The consequence is mechanical, not doctrinal: with a fixed `:latest` tag the task
definition JSON is byte-identical between builds, so `terragrunt apply` after a new
push registers **no new revision and starts no new deployment**. And because the
previous image was overwritten in place, there is no earlier tag to roll back to.

Note the inconsistency the repo already argues against itself:
`modules/careroute_stack/variables.tf:513` pins the monitoring images with the
comment *"Pinned, not `:latest` — an observability stack that silently changes
version between applies is not observable."* The same sentence applies to the
triage service.

**Fix:** app CI pushes `<repo>:$CI_COMMIT_SHA` to ECR; infra takes
`TF_VAR_image_tag=$CI_COMMIT_SHA`; `image_tag_mutability = "IMMUTABLE"`.

## 3. No automatic rollback on the deployed target

Agentic p15 (release control): "automatic rollback based on monitoring thresholds."
Agentic p19 is a whole slide on the rollback loop. CD p13: "versioned artifacts and
deployment scripts support rollback and redeploy."

`modules/ecs_service/main.tf` sets no `deployment_circuit_breaker`, no
`deployment_controller`, and no `deployment_minimum_healthy_percent` /
`maximum_percent`. A task that crash-loops on boot leaves ECS retrying forever
behind a half-drained target group.

**Fix:** `deployment_circuit_breaker { enable = true, rollback = true }`. Three
lines, $0, and it converts a bad deploy into an automatic revert.

Related and nearly free: the app's `deploy:canary-production` and
`rollback:production` jobs print `NOT RUN` because `CANARY_TARGET` and
`PROMETHEUS_URL` are unset — and this repo now provisions **both** (an ECS service,
and Prometheus reachable in-VPC). Exporting a `prometheus_url` output and passing
the service name turns two documented stubs into a real ramp. The traffic shift
itself is ALB weighted target groups; Agentic p17 lists canary / blue-green /
feature flag / shadow, and weighted forward actions cover the first two.

**Update 2026-09-19.** "Based on monitoring thresholds" is now literal: a 5xx-rate
CloudWatch alarm per service in the ECS `alarms { rollback = true }` block
(`careroute_stack/deploy_safety.tf`), a pipeline step that fails when ECS reverted a
release (`scripts/verify_rollout.sh`), and a `rollback` job for the manual case. The
app's Prometheus-driven canary job is retired in favour of it: GitLab's shared
runners cannot reach an in-VPC Prometheus, and ECS can read CloudWatch itself.
Blue-green / canary as ECS strategies still need AWS provider 6.x.

## 4. One environment, no staging

CD p8 names a "staging deployment — prove deployability in a production-like
environment" as a standard pipeline stage. Agentic p15/p16 show Dev → Test →
Staging → approval gate → production canary, with staging as where end-to-end agent
runs, RAG validation and safety checks happen. Platform p15 best practice:
"multi-account strategy for dev/test/prod segregation."

`live/` holds `demo` only. The module code supports more — it is one folder — and
[[Roadmap]] records the decision as "not worth it while the point is a single demo."

That remains defensible on cost, but it is the deck we align with least, and the
report should say so in those words rather than let a marker find it. Cheapest
honest option: add `live/staging/` with `enable_prometheus_stack = false` and never
apply it, so the promotion path is *in the code* even if it never bills.

## 5. Nothing runs on a schedule

Platform p15: "schedule model retraining via serverless such as AWS Step Functions
to be more cost effective." Platform p22: "schedule heavy jobs (e.g. retraining) in
off-peak hours." SMMonitor p17 and Serving p31 both build an **hourly monitoring
schedule** as the worked example of the whole module.

This repo has zero EventBridge, Lambda, Step Functions or Scheduler resources. The
drift→retrain loop exists only as a CI job, so in the cloud it never fires.

**Fix:** an EventBridge Scheduler rule → `ecs:RunTask` on the existing cluster,
running `python -m app.ml.monitor` against the backend image. ~30 lines, no new
always-on cost (the task lives for minutes), and it is the cloud rendering of the
`monitor.create_monitoring_schedule(...)` the deck spends six slides on.

## 6. No durable inference capture — the Model Monitor equivalent

Serving p26-28 and SMMonitor p12-14 make `DataCaptureConfig` the centrepiece:
100% of request/response pairs written to S3 as JSON lines, which is what a
monitoring schedule later reads. Platform p15: "store metrics and logs in a log
repository and storage such as S3 for audits."

`modules/artifacts` already has the `ml-telemetry` prefix, versioning, TLS-only and
a scoped RW IAM policy — but `enable_artifacts_bucket = false` in `live/demo`, and
nothing writes to it. Inference logs and drift reports go to Fargate task storage
that every deploy destroys.

[[Roadmap]] has this as an open decision between "S3 write from the app", "a
log-based path", and "EFS (rejected)". **The decks answer it: S3.** That is the
option both AWS slides use, it is the option Platform p15 names for audits, and it
is the only one that survives teardown.

## 7. MLflow has no tracking backend

Platform p4 (AWS), p8 (Azure), p11 (GCP) all open the same way: a bucket to hold
the `mlruns` folder and act as the centralized model registry. It is the single
most repeated infrastructure instruction in that deck.

Not provisioned. The app's champion/challenger promotion (`ml/lifecycle.py`,
`ml/promote.py`) is therefore inert between CI jobs — runs do not survive. The same
artifacts bucket plus `MLFLOW_TRACKING_URI` closes it.

## 8. Central logging: partly there, undersold

The app vault records Gap 3 (aggregated logging) as open, citing ELK. On AWS the
container logs of every task already go to one CloudWatch log group
(`modules/ecs_cluster`), which *is* the central store — that is worth stating
rather than leaving as a gap.

What is genuinely missing is the query side. `app/correlation.py` stamps every line
with `correlation_id` and `case_id`, and nothing in AWS indexes them. An
`aws_cloudwatch_query_definition` with the saved Logs Insights queries (by
correlation id, by case id, guardrail blocks) costs nothing and is what makes the
aggregation usable. LLMSecOps p116 (Pillar 3: Audit Logging) lists exactly the
fields to pivot on.

## 9. Security posture vs LLMSecOps p117

That slide is the wrap-up checklist: auth tokens, encrypt at rest (AES-256),
**encrypt in transit (TLS)**, rate limit, anomaly detection, PII redaction,
tenant-separated indexes, **rotate encryption keys**.

| Item | State |
|---|---|
| Encrypt in transit | ❌ ALB is HTTP :80 only. Largest remaining gap; [[Roadmap]] has it under "nice to have" — for a healthcare app it is not |
| Encrypt at rest | ✅ S3 AES256, ECR AES256 — but S3-managed, not a rotatable CMK |
| Rotate keys | ❌ no KMS CMK anywhere, so nothing to rotate |
| Rate limit | ✅ app-side, and `trust_alb_as_proxy` makes the bucket per-client |
| Auth tokens | ❌ [[Clinical Endpoint Auth Gap]] |
| Audit logging | ⚠️ app-side hash-chained log, but no ALB access logs, no VPC flow logs, no CloudTrail |
| Fine-grained roles | ⚠️ one shared task role (Platform p15 asks for per-team/per-project) |

ALB access logs to the artifacts bucket are near-free and close the cheapest third
of that row. Flow logs and a CMK are real (small) money — worth a written decision
either way rather than silence.

## 10. Lock file not committed

CD p6, the CD principles slide: "**keep everything versioned** — code,
configuration, infrastructure, and pipeline definitions are traceable."

`.gitignore` excludes `.terraform.lock.hcl`, so provider versions are not pinned and
CI can resolve a different patch release than a developer did. [[Roadmap]] lists it
under "known, offered, not taken". It is the one item there that a deck principle
names directly, and committing it costs nothing.

---

## Where we are ahead of the decks

Worth saying in the report, because it is the difference between checkbox
completion and judgement:

- **Infracost in CI** — Platform p23 explicitly teaches "predict the infra cost
  using tools like Terraform Cost Estimation". We run it on the machine-readable
  plan, per MR.
- **Prometheus + Grafana for infra and cost tracking** — Platform p23, done, and
  running the *app's own* rules rather than a re-written copy.
- **Policy-as-code (OPA/Conftest), Checkov, tflint, gitleaks as a hard gate** — CD
  p17 lists SCA, SAST, access controls, secrets, registry scanning, DAST. We cover
  that list and add IaC policy, which is not taught at all.
- **Plan-time preconditions** (`terraform_data.guards`) that fail a plan for
  combinations which would apply cleanly and then misbehave. Not in any deck.
- **Fargate Spot + manual apply + scheduled auto-destroy** — Platform p22's cost
  optimisation, applied harder than taught.

## Order of work

**2026-09-17 (third pass): every item below is now built.** The stack is not
applied — that is a week away — so everything here is `terraform validate`-clean
and reviewed, not proven. What is proven is marked as such.

| § | Built | Default |
|---|---|---|
| 1 | config re-sync + `config_drift` hard gate | on |
| 2 | ECR `IMMUTABLE`, `IMAGE_TAG` env-driven, app CI pushes to ECR + triggers infra | demo still falls back to `latest` |
| 3 | deployment circuit breaker; `prometheus_url` + `backend_service_name` outputs for the canary job | on |
| 4 | `live/staging/` — private networking, on-demand, its own state key | not applied |
| 5 | `modules/scheduled_task` → EventBridge Scheduler → `ecs:RunTask` | `enable_scheduled_monitor`, off |
| 6 | telemetry shipper sidecar → S3 (`DataCaptureConfig` equivalent) | `enable_ml_telemetry_shipping`, off |
| 7 | `modules/mlflow` — tracking server, S3 artifact root | `enable_mlflow_server`, off |
| 8 | 5 saved Logs Insights queries on `correlation_id` / `case_id` | on (free) |
| 9 | ACM/HTTPS listener + 301 redirect, ALB access logs, VPC flow logs, S3 + interface endpoints | TLS off (no domain); logs off |
| 10 | lock file committed | done |
| — | OPA rules for the artifacts bucket; curated `.checkov.yaml` | on |

Three things stayed deliberately unbuilt, and the reasons are not cost:

* **WAF on the ALB.** A genuine gap for a healthcare app, not a trade-off.
  Recorded in `.checkov.yaml` as *not* skipped so `CKV2_AWS_28` keeps failing.
* **MLflow reachable from CI.** The server is internal-only because MLflow has
  no authentication. GitLab CI — where training actually runs — therefore cannot
  reach it without a VPC-resident runner. Naming this is more useful than
  exposing an unauthenticated model registry to close a checklist item.
* **Per-agent task roles.** Still blocked on the agents becoming separate
  services; one role per in-process object is theatre.

Done earlier the same day — see [[Changelog]]:

1. ✅ Re-synced `alert.rules.yml` (all 9 rules, both groups); new hard-gate
   `config_drift` CI job diffs the three verbatim copies against the app repo
   and self-skips green without `APP_REPO_URL`. *(§1)*
2. ✅ `deployment_circuit_breaker { enable, rollback }` on every ECS service,
   behind `enable_deployment_rollback` (default on). *(§3)*
3. ⚠️ **Half.** `modules/ecr` now defaults to `IMMUTABLE` and the stack takes
   `image_tag` / `ecr_image_tag_mutability`; `live/demo` reads `IMAGE_TAG` from
   the environment and only falls back to the `latest`/`MUTABLE` pair when it is
   unset. The other half — the app pipeline pushing `$CI_COMMIT_SHA` to ECR
   instead of `docker save`-ing tarballs — is an app-repo change and is NOT done.
   *(§2)*
4. ✅ `.terraform.lock.hcl` committed and un-ignored. *(§10)*

What is left is not code — it is the things only an apply can settle:

* Stand `live/staging` up once and confirm the three-container drift-monitor
  task orders correctly (restore → monitor → archive). The ECS `dependsOn`
  pattern is standard, but it has never run here.
* Confirm the MLflow image tag matches the app's `mlflow==3.13.0` pin. A 3.x
  client against a 2.x server fails on the registry API — the exact half the
  server exists to provide.
* Get a domain and a certificate. Everything TLS is wired and waiting on one
  ARN.
