---
tags: [active, careroute, infra]
updated: 2026-09-29
---
# Changelog

Back to [[Home]]. Newest first. One entry per change that alters what gets
deployed or how it is operated.

## 2026-09-29 (second) — semantic safety activation on; safety LLM pinned to gpt-5.4-mini

- `live/demo`: `safety_prototype_semantic_activation = true` — the safety LLM may now **raise** acuity
  (never lower it) above its 0.85 threshold. Live evidence: "left hand went numb and clumsy, the words come
  out wrong" was flagged `stroke_signs` at 0.9 by the LLM but, in shadow mode, still routed P3 unescalated.
  Local end-to-end check with activation on: that case and "an elephant is sitting on my chest" → P1,
  escalated (`additive_escalation`); a twisted ankle and a mild sore throat stay P4. Rollback: `false` + apply.
- New `safety_llm_model` (`variables.tf`) → `CAREROUTE_SAFETY_LLM_MODEL` when non-empty; demo `gpt-5.4-mini`.
  Measured on the adjudication prompt, 4 cases each: gpt-4o-mini 2.0–3.3 s, conf 0.90; **gpt-5.4-mini
  1.7–3.4 s, conf 0.97–0.99**; gpt-5.4 2.3–4.2 s (too close to the 5 s budget). All three 4/4 correct.
- **Unchanged on purpose:** Safety-NLP timing. Measured on 2 vCPU / 4 GB: at the 1 s limit every stage times
  out (no NLP output, +1–3.6 s per triage); with mDeBERTa at a 10 s limit a request takes 8–18 s; without
  mDeBERTa 0.7–0.9 s, all stages succeed. But once the NLP succeeds, the app's design lets it skip the LLM
  check (tests/agents/test_safety.py `test_semantic_reasoning_skips_llm_without_uncertain_nlp`), so today's
  timeouts are what make the LLM check every case. Left for the Safety-Override owner to decide.

## 2026-09-29 — Safety-NLP runtime: flags, memory guard, demo sized for the models

- New variables (`modules/careroute_stack/variables.tf`): `safety_nlp_direct_nli`,
  `safety_prototype_semantic_activation`, `enable_safety_llm` (all default `false`).
- `local.safety_nlp_env` (`main.tf`) now also sets `CAREROUTE_SAFETY_NLP_DIRECT_NLI`,
  `CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION`, `CAREROUTE_SAFETY_LLM`,
  `CAREROUTE_SAFETY_LLM_PROVIDER_ORDER=openai`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`.
- Precondition in `terraform_data.guards`: `enable_safety_nlp` needs `backend_memory >= 3072`
  (`>= 4096` with direct NLI).
- `live/demo`: backend `2048 / 4096` (was `512 / 1024`); Safety-NLP, direct NLI and the safety LLM on;
  semantic activation **off** for the first rollout. Sized from a local measurement of the release image
  (1.85 GiB idle, 2.14 GiB peak over 20 triages); re-measure under concurrent load on AWS.
- Needs the app image from `careroute_ai_app` with the Safety-NLP build args (its `build:images`, 2026-09-29).
  See [[App Contract Settings]] §6.

## 2026-09-26 — optional LangSmith key for the app's LLM tracing

New sensitive `langsmith_api_key` (`modules/careroute_stack/variables.tf`), set by the masked CI variable
`TF_VAR_langsmith_api_key`. When non-empty: Secrets Manager secret `<prefix>/langsmith-api-key` and
`LANGSMITH_API_KEY` in the ECS `secrets` of the backend and every sub-agent (`local.langsmith_secrets`,
`modules/careroute_stack/main.tf`); the execution role already reads `<prefix>/*`. Empty (default): nothing is
created and the app's LangSmith sink stays off. Pairs with app MR !10 (`backend/app/tracing.py`).

## 2026-09-25 (third) — Checkov 28 → 4, Ollama removed, Infracost removed, config_drift always runs

- **Checkov: 28 failed → 4** (7 fixed, 17 skipped inline with a reason on the resource). Fixed: public subnets no longer
  hand out public IPs (`map_public_ip_on_launch = false`; Fargate gets its IP from the service), the default security
  group is locked to no rules, RDS minor upgrades / IAM auth / snapshot tags. Skipped with reasons: cross-module SG
  attachments Checkov cannot follow, the ALB→task hop, Container Insights and CMK costs, and **SNS encryption — the
  AWS-managed key would silently stop every CloudWatch alarm e-mail**. The 4 left (TLS/WAF) stay red on purpose per
  `.checkov.yaml`, whose CKV_AWS_378 note was wrong (a certificate does not fix the ALB→task hop) and is corrected.
  Files: `modules/{alb,artifacts,ecs_cluster,networking,observability,rds,scheduled_task,security}/main.tf`.
- **Ollama removed** — the LLM is the OpenAI API only. Deleted `modules/ollama_gpu`, its `module "ollama"`, the
  `enable_ollama` / `ollama_instance_type` / `ollama_model` variables, the `ollama_private_ip` output, the `OLLAMA_*`
  env on backend and sub-agents, and the Ollama security group (`modules/security`). **Next plan destroys that unused
  SG** and updates the two public subnets and the default SG (above); nothing stateful is replaced.
- **Infracost removed** (needs an account key); `config_drift` now clones the app repo over `CI_JOB_TOKEN` by default
  instead of skipping green when `APP_REPO_URL` is unset — if the clone is refused, add this project to the app
  project's job-token allowlist (`.gitlab-ci.yml`, `README.md`).

## 2026-09-25 (second) — OIDC trust for the app's deploy-only branch

`modules/ci_oidc` gains `extra_refs`; `careroute_stack` passes `app_ci_extra_refs` (default `["release/deploy"]`), so
pipelines on the app's deploy-only branch can push images and deploy. **Apply only after `release/deploy` is a
protected branch in GitLab**: the trust keys on the branch name, so an unprotected branch would let anyone who can
push to it deploy.

## 2026-09-25 — staff login path routed to the frontend

After app 0.2.0-252 went live, anonymous `/api/escalations` returned 401 as intended, but `/api/staff/session`
(the new login Route Handler) was answered by the backend with 404 (`server: uvicorn`): the ALB's `/api/*` rule sends
everything under /api to the backend except `frontend_proxied_paths`, which only listed the escalation paths. Added
`/api/staff/*` to `local.staff_proxy_paths` (`modules/careroute_stack/staff_auth.tf`); 3 of the 5 values one ALB
condition allows. Lesson: a new Next.js Route Handler under /api needs an ALB path too.

## 2026-09-24 (seventh) — staff password secret for the frontend

New sensitive `staff_password` (masked CI variable `TF_VAR_staff_password`) → Secrets Manager
`<name_prefix>/staff-password` → frontend-only `CAREROUTE_STAFF_PASSWORD` (`local.staff_frontend_secrets`). The app's
escalation proxy now requires a staff session cookie issued after this password check; before, it lent the staff key
to anonymous callers. Unset = a random password, so the portal fails closed rather than open.

## 2026-09-24 (sixth) — per-task telemetry prefix; monitoring configs synced

The bucket showed a single `ml-telemetry/1TASK_ID/` folder: the shipper wrote `.../ml-telemetry/$$TASK_ID/`, and in
HCL only `$${` is an escape, so the shell received `$$TASK_ID` (its own PID, 1 in a container, + "TASK_ID") and every
task shared one prefix. Now `$${TASK_ID}` (rendered as `${TASK_ID}`; checked with `terraform console`). Also synced
`config/careroute-triage.json` and `config/alert.rules.yml` with the app repo (byte-identical to app main 2090bb3), so
Grafana on AWS gets the new panels (plan shapes, HITL SLA, retrain queue, LLM routing, per-call guardrails).

## 2026-09-24 (fifth) — precedent memory and retrain queue on the telemetry volume

`CAREROUTE_PRECEDENT_LOG` and `CAREROUTE_RETRAIN_QUEUE` (new in the app: HITL precedent memory and the
clinician-disagreement retrain queue) now point into the shared telemetry volume, next to the inference and
ground-truth logs, so the shipper archives them to `s3://…/ml-telemetry/` and they survive a rollout.


## 2026-09-24 (fourth) — full-colour icons in the two architecture diagrams

Docs only, nothing deployed. `docs/careroute-demo-runtime-architecture.drawio` and
`docs/careroute-cicd-pipeline.drawio` swapped their single-colour Simple Icons and generic AWS
placeholders for official full-colour logos (Grafana, Terraform, GitLab, OpenAI + Claude, Onyx)
and Fluent colour icons (patient/clinician, OneMap, smoke test, destroy). Generated by
`report/diagrams/recolor_icons.py`; PNGs re-rendered.
Committed 2026-09-26 after the 09-25 Infracost removal: the Review stage now shows OPA/Conftest only
(Infracost icon, caption and `INFRACOST_API_KEY` legend line dropped).

## 2026-09-24 (third) — ML telemetry never reached S3

After a 15-minute load test the artifacts bucket still held only `dvc/`. The telemetry volume is created by
Fargate root-owned 0755 and the backend runs as the non-root `careroute` user, so every inference-log append
failed (silently, at DEBUG) and the shipper synced an empty directory. The shipper sidecar and the drift
monitor's restore container (both root) now `chmod -R a+rwX` the task-private volume. The shipper also read its
task id with `wget`, which the aws-cli image lacks, so every task would have shared `ml-telemetry/unknown/`;
it tries `curl` first.


## 2026-09-24 (second) — drift-monitor task definition rejected by ECS

The first apply of `481e003` updated the app CI role and created the SNS e-mail subscription, then failed on
`aws_ecs_task_definition` for the drift monitor: *"A dependency container with SUCCESS or COMPLETE condition
cannot be an essential container."* `modules/scheduled_task` marked the main container essential while the
archive finalizer depends on it with `SUCCESS`. The main container is now essential only when there are no
finalizers, and finalizers are essential: the task ends when the archive step exits, and a failed main run
leaves the finalizer's dependency unmet so ECS stops the task without archiving a partial report.

## 2026-09-24 — alarm emails and the scheduled drift monitor on in demo

`live/demo`: `alarm_email = "alerts@example.com"` subscribes the team inbox to the CloudWatch alarm
SNS topic (modules/observability); AWS sends a confirmation link after apply and delivers nothing until
it is clicked. `enable_scheduled_monitor = true` runs `python -m app.ml.monitor` as a one-shot Fargate
task on `cron(0 3 * * ? *)` (03:00 UTC daily), restoring the inference log from the artifacts bucket and
archiving its drift report back. The deploy-rollback alarms stay without an SNS action (rollback
triggers, not pages). Alertmanager receivers stay empty: Prometheus alerts are visible in its UI only.

## 2026-09-23 — image rollouts move to the app pipeline

**Why.** A release took two clicks in two projects: the app's `deploy:trigger-infra`,
then this repo's manual `apply`. The app job went green as soon as the trigger was
accepted, so it could not say whether ECS was serving the new build. The comments and
the report described one click and an automatic `apply`, but `apply` had no rule for that.

**Now.** Terraform (this repo) owns the infrastructure. The app pipeline owns which
image tag runs: its `deploy:ecs` and `rollback:production` run `scripts/deploy_ecs.sh`,
which registers a revision copied from the running one with the tag swapped, updates the
service, waits, and fails unless ECS settles on that tag. It records the release in the
same SSM parameters `release_history.sh` uses. An infra-only `apply` keeps the running tag
(`running_image_tag.sh`), so it does not undo a deploy.

**Change here.** `modules/ci_oidc` (role `careroute-<env>-app-ci-ecr-push`, name kept so
`AWS_DEPLOY_ROLE_ARN` stays valid) gains `ecs:Describe/RegisterTaskDefinition`,
`ecs:DescribeServices/UpdateService` on this cluster's services only, `iam:PassRole` on the
two ECS roles (to `ecs-tasks` only), and `ssm:Get/PutParameter` on
`/careroute/<env>/image-tag/*`. **Needs one `apply` here before the app's `deploy:ecs` works.**
The trigger path and the `rollback` job stay as a Terraform-side fallback. Gap: the
drift-monitor schedule pins its task-definition revision, so it moves to a new tag on the
next `apply` here.

## 2026-09-22 (second) — the diagram draws what runs; the duplicate diagrams go

**Runtime diagram.** It showed an ADOT collector sidecar in the backend task
(dashed, "optional") and an EMF arrow into CloudWatch. `enable_app_metrics` is
off in `demo`, so nothing of the kind runs. The sidecar the task *does* carry is
`ml-telemetry-shipper` (plain `aws-cli`, one-way `aws s3 sync` of the
`/telemetry` volume into the artifacts bucket), which the picture never showed.
The box is now that shipper, solid, with its own legend step; the EMF arrow is
gone and a logs arrow (awslogs) runs from the backend to CloudWatch instead.
CloudWatch and SNS stay, because they are real without ADOT: every task's logs,
the backend-CPU and ALB-5xx alarms and the two deploy-rollback alarms all exist
in state and notify the SNS topic (no email subscriber in `demo`). The legend
names what is deliberately not drawn: ADOT, API Gateway, RDS, MLflow, the
drift-monitor task and the sub-agent tier — all off.

**Removed** (git history keeps them):

- `docs/diagrams/careroute-aws.{drawio,html,architecture.json}` — a second
  drawing of the same stack (archify build of 2026-09-21). Its HTML still said
  the drift monitor was off and did not know about the bucket or the fourth
  secret; keeping two diagrams current is how one of them goes stale. The
  README now has one architecture diagram.
- `docs/iac-map.html` — an interactive module map from 2026-09-19 that nothing
  linked to.
- the `.gitignore` rule for archify sidecars, which had nothing left to guard.

**Kept, deliberately:** every optional module (`api_gateway`, `rds`, `mlflow`,
`ollama_gpu`, `scheduled_task`, `state_store`, the sub-agent tier). They are
wired into `careroute_stack` behind flags, guarded at plan time, and the README
and COST.md lean on them to explain what a fuller build costs; removing them is
a refactor of the composition, not a cleanup, and belongs in its own change if
the team wants it. `live/staging` stays for the same reason. The
[[Clinical Endpoint Auth Gap]] note gains a closed banner instead of being
deleted — it is the record of why the staff key exists.

CI/CD diagram: the target box no longer says "ADOT sidecar"; S3 is no longer
"opt". Not applied — no `.tf` changed.

## 2026-09-22 — applied: the OIDC fix and the artifacts bucket are live; docs redrawn to match

**Applied twice today, both from the pipeline's manual `apply`** (the first time
`apply` has been the thing that changed the account, rather than a laptop):

- 12:28 SGT, #2868203585 (`1b57565`): the project-id trust policy, the OneMap
  secret, backend task definition rev 4.
- 14:10 SGT, #2870052162 (`677426e`): `enable_artifacts_bucket` +
  `enable_ml_telemetry_shipping` in `live/demo` — 9 added, 2 changed, 1 destroyed.
  `verify_rollout.sh` passed for backend and frontend; release history records
  `current=latest`, `previous=` (no SHA-tagged release has been through CD yet).

State serial 13, 81 resources, Terraform 1.9.8. Five ECS services at 1/1, ALB 200,
Grafana provisioned with the Prometheus datasource and the triage dashboard,
Prometheus scraping the backend every 15 s, 14 alert rules loaded, none firing.
Secrets in the account: `openai-api-key`, `onemap`, `staff-api-key`,
`grafana-admin-password`.

**The app side caught up the same afternoon.** `main` had been failing
`train:model` on the explanation-agreement gate and then `test:model-gate` on
feature-contract probe coverage (the 22 → 51 symptom widening never extended the
probe table). Both are fixed on `main` (`446c23b`); `backend/.dvc/config` now
points its default remote at `s3://careroute-demo-artifacts-<acct>/dvc`, which
is why the bucket had to land first. The first end-to-end CD run
(`deploy:push-images` → `deploy:trigger-infra` → `plan` → `apply`) is the next
thing to happen.

**Docs.** README and both architecture diagrams now say what is deployed rather
than what was planned: five Spot tasks, the S3 bucket on, four secrets, the OIDC
push role, the deploy-rollback alarms, seven agents in-process, and the
`project_id` ID-token setting the app project needs. Not applied — no `.tf` changed.

## 2026-09-21 (second) — the app CI role trusted a claim GitLab will never issue

Not applied. `terraform fmt -check -recursive modules/` is clean.

**The bug: `deploy:push-images` in the app repo could not start at all.**

```
failure_reason: id_token_burned_project_path
started_at:     None      <- never reached a runner
trace:          0 bytes
```

GitLab mints a job's OIDC ID token *before* scheduling it. It keeps a tombstone
of every project path a **different** project once held, and refuses to issue a
token when a project sits on such a path while its subject claim is path-based —
otherwise reclaiming a freed path would mint a `sub` byte-identical to the
original project's and satisfy a trust policy written for it. The app project
(`teamproject4040840/nus-iss3/careroute_ai/careroute_ai_app`, id **84456994**) is
on a burned path and had `ci_id_token_sub_claim_components = ['project_path',
'ref_type', 'ref']`, so the mint was refused and the job failed before running.

Two consequences that were not obvious:

- The `id_tokens:` block is fatal **on its own**. GitLab tries to mint whether or
  not the script uses the token, so falling back to `AWS_ACCESS_KEY_ID` while
  leaving the stanza in place would not have helped.
- The job is `allow_failure: true`, so pipeline #2866876547 reported `success`
  having deployed nothing.

**The fix.** `modules/ci_oidc` now keys its trust policy on the numeric project
id instead of the path:

```hcl
values = ["project_id:${var.project_id}:ref_type:branch:ref:${var.ref}"]
```

`var.project_path` became `var.project_id`, and `app_ci_project_path` became
`app_ci_project_id` (default `84456994`). The numeric id is globally unique and
never reassigned, so the policy also survives any future rename or group
transfer — and a project that later takes over the old path does *not* inherit
the trust.

**Not self-sufficient.** This half only works once the app project's
CI/CD → "ID token subject claim" setting is `["project_id", "ref_type", "ref"]`.
Apply this stack first, then flip the GitLab setting; the reverse order leaves a
window where GitLab issues a claim the role does not trust.

The OIDC machinery itself was already built and applied — the plan shows
`aws_iam_openid_connect_provider.gitlab`, `aws_iam_role.push`
(`careroute-demo-app-ci-ecr-push`) and its inline policy all merely refreshing.
Only the `sub` condition was wrong. See [[App Overview]].

## 2026-09-21 — a doc string was destroying a live security group

Not applied. `terraform fmt -check -recursive modules/security/` is clean.

**The bug: `apply` had never been able to finish, and the reason was prose.**
Pipeline #2867507633's `apply` job burned 16m21s and died on

```
Error: deleting Security Group (sg-0002c507a5ed6e15e):
  api error DependencyViolation: resource sg-0002c507a5ed6e15e has a dependent object
```

The plan that produced it (`Plan: 7 to add, 10 to change, 5 to destroy`) said why:

```
# module.security.aws_security_group.vpc_link must be replaced
  ~ description = "API Gateway VPC link ENIs to internal ALB"
              -> "API Gateway VPC link ENIs -> internal ALB"   # forces replacement
```

EC2 has no `ModifySecurityGroupDescription` call — a security group's
`description` is immutable — so the provider can only honour an edited
description by destroying the group and creating a new one. The deployed group
reads `... ENIs to internal ALB`; `modules/security/main.tf` has read
`... ENIs -> internal ALB` since the squashed initial commit. Those two strings
differing by an arrow meant **every** apply planned to delete a security group
that the API Gateway VPC link's ENIs are actively holding. AWS refuses that with
`DependencyViolation`, Terraform retried for 15 minutes, then failed the job.

Nothing recent caused it: since SG descriptions cannot be edited in the console
either, the live group must predate this repo's history. It was standing drift
that every apply would keep hitting, and the pipeline badge hid it — both deploy
jobs are `allow_failure: true`, so #2867507633 reports `success` while having
deployed nothing.

**The fix.** `modules/security/main.tf` now carries the deployed spelling, so the
replacement vanishes from the plan. A `[TF]` comment above the resource records
that this string is load-bearing and must not be "tidied".

**Left deliberately undone.** The durable fix is
`lifecycle { create_before_destroy = true }` plus `name_prefix` (two groups
cannot share a name during the overlap), which would let SG attributes change
safely in future. It is not in this change because enabling it itself forces one
replacement, and that must not land in the apply that is rescuing this one.
Tracked in [[Roadmap]].

**Still broken after this.** The failed apply was partial and left `backend` and
`grafana` with their task definitions deregistered and not re-created; the
running services are unaffected (deregistering a revision does not stop running
tasks) but cannot cleanly replace a task until an apply completes. The plan also
ran with `IMAGE_TAG not given - keeping the running tag: latest`, so this apply
was never going to ship new images regardless. See [[App Overview]].

## 2026-09-19 (fifth) — the clinician labels never reached the volume; deployment shape settled

Not applied. `terraform fmt -check` and `terraform validate` are clean.

**The bug: concept drift could never be computed in the deployed stack.**
`local.telemetry_path_env` pointed the app's telemetry at the shared
`ml-telemetry` volume with **three** paths and needed four — it was missing
`CAREROUTE_GROUND_TRUTH_LOG`. Those rows are the clinician HITL decisions, the
only labels this system ever gets, and `app/ml/monitor.py` joins them against the
inference log by case id to compute concept drift. Unset, the app fell back to
its in-image default (`backend/monitoring/ground_truth.jsonl`), which is not on
the volume: the service wrote labels into its own container filesystem, they died
with the task, and the scheduled monitor read a path that was always empty.

Nothing errored. The drift report simply came back with no labelled rows — an
MLOps pillar reporting success because its input never arrived. The local comment
three lines above the map already stated the rule it broke ("they have to agree
on where the inference log lives or the job reads an empty directory and reports
no drift"). Worse, the app's own `docker-compose.yml` sets this variable on all
three services, so **Compose was right and only the deployed stack was wrong** —
the shape of gap that never shows up locally. Recorded as §7 of
[[App Contract Settings]].

**Deployment shape settled: one compute, and it stays that way.** The app now
supports one-agent-per-container (`AGENT_TRANSPORT=http`), but this stack keeps
deploying the `monolith` image with every agent in-process. That is a cost
decision: ten containers would need a 2 vCPU / 8 GB task instead of the current
one, and ten ~1.1 GB images in ECR bill storage per retained tag for images
nothing pulls. The app side now names what it publishes
(`CAREROUTE_PUSH_IMAGES = "backend frontend"`) and still builds and scans all
twelve every pipeline, so the split stays proven without being paid for. No ECR
repos, no `sidecar_containers`, no Redis and no task-size change are needed here.

**Two guard messages corrected, because they had quietly become traps.** Both
said multi-task was safe "once `store.py` has a shared backing store". As of
today it *has* one — Redis, via `app/persistence.py` — and multi-task is still
unsafe: Redis made a restart lossless, not a second task correct, because memory
stays authoritative while a task runs and is reloaded only at start-up. The
second precondition is narrower than it reads, too: it checks that *some* shared
store exists, not that the app uses *that* one, so enabling DynamoDB would
satisfy it while the app talks to Redis — the same data loss with one more billed
resource. `enable_shared_state_store` stays `false`, so the DynamoDB table is not
created and costs nothing.

**Monitoring configs re-synced** with the app repo (`alert.rules.yml` gained
`AgentDown` and `AgentMetricsDown`; the dashboard was 2.7 KB behind), so
`config_drift` goes green again. Both new alerts are **inert** under the
single-compute shape — there are no agent containers emitting
`careroute_agent_calls_total`, and no `careroute-agents` scrape job here — so
they cost nothing and cannot false-alarm; they start working the day the task
definition grows containers.

**Deliberately NOT done:** no scheduled `batch_score` task. `app.ml.batch_score
--from-store` re-scores the **in-process** escalation queue, so a separate task
would boot with an empty store and score nothing. It becomes meaningful only when
store.py reads through to a shared store.

## 2026-09-19 (fourth) — the full dashboard, synced monitoring configs, Container Insights off

Not applied. `terraform fmt`/`validate` are clean. A mocked `terraform test` passed
five plans, including one that renders the monitoring module. All 91 dashboard
queries parse against Prometheus 3.1. I ran them against a local backend on a fake
OpenAI endpoint: every query that stayed empty was one whose event never happened
in that run, or read `process_*` series, which the Python client exports on Linux
only.

**Dashboard (`config/careroute-triage.json`, copied from the app).** It went from 9
panels to 77 in 11 rows, and now plots every metric `app/metrics.py` exports. Before,
15 of its 23 metrics had no panel of their own. It also adds the recording rules, backend process
runtime, firing alerts, and the health of Prometheus and Alertmanager. There are
`$agent` and `$model` filters. The dashboard refreshes every 1 minute instead of 30
seconds, because each refresh runs about 90 queries on a 0.25 vCPU Prometheus. The
app now generates the dashboard (`monitoring/grafana/generate_dashboard.py`), and a
test fails if a panel reads a metric that doesn't exist or a metric has no panel.

**It could not ride in an env var any more**, because 56 KB minified is close to the
task definition's 64 KB total. It now ships gzipped (`base64gzip`, ~10 KB) and is
unpacked by busybox in the Grafana entrypoint. `GF_METRICS_ENABLED=false`: Grafana's
own unauthenticated `/metrics` was reachable through the public `/grafana` route,
and nothing scraped it.

**Config drift fixed.** `alert.rules.yml` here was 80 lines behind the app: it was
missing the whole `careroute-availability` group, meaning the yield, harvest and
uptime recording rules, plus `LowTriageYield` and `AnswersDegradedWhileYieldHealthy`.
The dashboard was 3 panels behind. The `config_drift` job would have failed. Both
are now byte-identical to the app.

**Prometheus scrapes Alertmanager** (Cloud Map DNS), for the notification panels.

**Container Insights off by default** (`enable_container_insights`). It was
hard-coded on, and it bills as custom metrics that nothing reads.

## 2026-09-19 (third) — built to scale: every tier ready, the backend's blocker named

Not applied. `terraform fmt` and `terraform validate` are clean (Terraform 1.9.8). A mocked
`terraform test` ran four plans: the defaults, the staging shape, a scaled backend,
and the guard refusing more than one task with no store. All four passed.

**Frontend autoscaling (`modules/ecs_service`, `careroute_stack`).** The stateless
half never autoscaled: the module input was only ever passed to the backend. The
frontend now has its own switch, `frontend_enable_autoscaling`, on in staging (1 → 3).

**Better scaling signals.** Besides CPU, target tracking can now also use memory and
ALB requests per task. Requests per task is the signal that matters for the backend,
which mostly waits on the LLM. Scale-in now waits 300 s (it was 120 s), because
removing a task cuts off its open SSE streams.

**Spot with an on-demand floor.** `fargate_on_demand_base` keeps N tasks on FARGATE
and runs the rest on Spot.

**Least-outstanding-requests routing on the backend target group** (`modules/alb`).
Changes nothing at one task. Once the backend runs several, it stops round robin
from adding new streams to a task that is already busy.

**Prometheus scrapes every backend task** (`monitoring_stack`). The static target
was a Cloud Map MULTIVALUE name, so each scrape reached one random task, and
counters would have jumped between processes. It now uses `dns_sd_configs`. With
that change, a backend that has no tasks at all produces no `up` series, so the
app's `BackendMetricsDown` could never fire. The new infra-owned
`config/platform.rules.yml` adds `BackendNoScrapeTargets` (`absent()`) to cover it.

**The shared state table (`modules/state_store`, `careroute_stack/scaling.tf`).**
One DynamoDB on-demand table, with TTL and PITR. The task role can read and write
items, but IAM denies deleting or updating any `AUDIT#` item. A free gateway
endpoint and the `CAREROUTE_STATE_*` env vars come with it. It is off by default
because the app has no client for it yet. `allow_multi_task_backend` is now refused
unless a shared store (this table or RDS) is on. Before this change, the flag turned
the data-loss guard off and changed nothing else. Also added: a Conftest deny rule
for a state table without PITR, and a `CKV_AWS_119` skip with its reason.

## 2026-09-19 (second) — the staff key reaches AWS; `apply` gets its plan

**Staff API key (`careroute_stack/staff_auth.tf`, `modules/alb`).** The app guards
`/api/escalations*` with `CAREROUTE_STAFF_API_KEY`, attached by the frontend's Route
Handler. On AWS that protected nothing: the stack never set the key (backend in open
demo mode), and the ALB's `/api/*` rule sent escalation calls straight to the backend,
past the handler. Now a generated key goes to both tasks from Secrets Manager, a
priority-5 rule sends `/api/escalations*` to the frontend, and the handler reaches the
backend over Cloud Map (so the backend joins Cloud Map whenever the key is on).

**`apply` had no plan to apply.** `plan -out=tfplan` wrote into Terragrunt's hashed
`.terragrunt-cache/<x>/<y>/` working copy, which neither the repo-root job cache nor an
artifact picked up. The plan now goes to an absolute path next to `terragrunt.hcl` and
travels as an artifact (`access: developer`, 1 day — it contains secrets, as
`plan.json` already did).

## 2026-09-19 — alarm-driven rollback, a verified release, and the app CD seam

Not applied. `terraform validate` clean on AWS provider 5.100.0 / Terraform 1.9.8.

**Alarm rollback (`careroute_stack/deploy_safety.tf`, `modules/ecs_service`).** The
circuit breaker only sees tasks that fail to *start*. A release that boots, passes
`/api/health` and then serves 5xx was invisible to it. Each ALB-fronted service now
has a 5xx-rate alarm (`deploy_rollback_error_rate`, 0.01 = the app's
`canary.MAX_ERROR_RATE`, with a request floor so an idle demo cannot trip it) wired
into the service's `alarms { rollback = true }`. Kept out of `modules/observability`
because that module reads `module.backend.service_name`: a dependency cycle.

**A deploy that was rolled back now fails the pipeline.** `apply` returns before ECS
rolls out. `scripts/verify_rollout.sh` waits for the rollout *and* its bake period and
compares the settled image tag with the deployed one — "stable" alone would report a
reverted release as a good one. Only then does `scripts/release_history.sh` record the
tag in SSM, which is what the new manual `rollback` job re-applies.

**IMAGE_TAG default removed.** It was `${CI_COMMIT_SHORT_SHA}` — this repo's commit,
which names no image. `plan` now takes the tag from the app trigger or reuses the
running one.

**App CD seam.** `modules/ci_oidc`: a GitLab-OIDC role that can push to the two ECR
repos and nothing else — the app's `deploy:push-images` uses it instead of stored keys.
`apply` stays a manual second click when the app's `deploy:trigger-infra` starts a
pipeline; `ROLLBACK_REQUESTED=true` (sent by the app's `rollback:production`) runs only
`rollback`, automatically.

**Not done:** native blue/green / canary (needs provider 6.x — a major upgrade of its
own), and a CloudWatch alarm on the app's tool-failure rate (Prometheus-only today).

## 2026-09-17 (third pass) — build out everything the review found

Not applied: the stack goes up in about a week. Everything below is
`terraform fmt`/`validate` clean and reviewed, and none of it has run. Where a
design has a failure mode only an apply can reveal, the code says so.

Every new capability is **off by default**, so `live/demo` costs exactly what it
cost this morning. `live/staging` turns them on.

**Network (`modules/networking`).** S3 gateway VPC endpoint, on by default
because gateway endpoints are free; it keeps ECR layer pulls off NAT wherever
NAT exists. Optional PrivateLink endpoints for ECR/logs/Secrets Manager (~$58/mo
for four across two AZs, so off, and ignored without NAT since there is nothing
to privatise when tasks sit in public subnets). Optional VPC flow logs.

**TLS (`modules/alb`).** `certificate_arn` adds an HTTPS :443 listener carrying
all the routing and turns :80 into a 301. Empty by default — the demo has no
domain — but everything else is wired: `url_scheme` is now an ALB *output* that
the stack builds CORS origins and Grafana's root URL from. Hardcoding `http`
there would mean that the day a certificate appears, the app advertises an
origin the browser is no longer using and every staff-page preflight fails, with
a valid certificate installed, which is the last place anyone would look. The
:443 security-group rule is likewise tied to the certificate: an open port with
nothing behind it is worse than a closed one, because it is a finding a reviewer
has to chase to discover it leads nowhere.

**Durable ML telemetry (`enable_ml_telemetry_shipping`).** The SageMaker
`DataCaptureConfig` equivalent, as a sidecar rather than an app change — checked
first, and the backend has no boto3 and no S3 client at all, so injecting a
bucket name would have produced configuration the application cannot read. The
app goes on writing plain files to `CAREROUTE_INFERENCE_LOG` / `_MONITOR_DIR` /
`_REPORT_DIR`, now pointed at a shared task volume, and a stock `aws-cli`
container syncs it out on a timer. The sync is additive (no `--delete`), keyed
per task id: a replacement task starting with an empty volume must never be able
to wipe the history already in S3, which is precisely what `--delete` would do
on every deploy.

**Scheduled drift monitor (`modules/scheduled_task`, `enable_scheduled_monitor`).**
EventBridge Scheduler → `ecs:RunTask`, running `python -m app.ml.monitor` from
the *same backend image* the service runs, so the scheduled job cannot drift to
different model code. Three ordered containers: restore telemetry from S3,
monitor, archive the report back — ECS `dependsOn: SUCCESS` in both directions.
The ordering is the point: a monitor that starts on an empty directory does not
fail, it reports **no drift**, which is indistinguishable from a healthy model
and is the one wrong answer nobody investigates.

Not Step Functions, which is what Platform p15 names. A state machine earns its
keep when there are steps to sequence and compensate; this is one container
running one command, and Step Functions would call `ecs:RunTask` underneath
anyway. When the loop grows to monitor → retrain → gate → register, that flips.

**MLflow (`modules/mlflow`, `enable_mlflow_server`).** A tracking server on
Fargate with S3 as the artifact root — the first instruction on the Platform
deck's AWS slide, repeated for Azure and GCP, and the one with no implementation
here at all. Two limitations stated in the module header rather than designed
around: MLflow has **no authentication**, so it is internal-only by default; and
internal-only means GitLab CI, where training actually runs, cannot reach it
without a VPC-resident runner. Exposing an unauthenticated model registry to
close a checklist item would be the wrong trade. Without RDS the registry is
SQLite on ephemeral storage and is lost on every redeploy while the S3 artifacts
survive — orphaned artifacts plus a registry that has forgotten them, which is
worse than no server because it looks like it works. Said so in the variable.

**Saved Logs Insights queries.** Five, pivoting on the `correlation_id` /
`case_id` that `app/correlation.py` has been stamping on every line with nothing
in AWS indexing them. Worth stating plainly for the report: the *store* half of
Lecture 03's central-logging requirement already existed — every container in
every task writes to one CloudWatch log group — what was missing was any way to
ask it a question.

**`live/staging/`.** The promotion target CD p8 and Agentic p15 ask for.
Deliberately *not* a copy of demo: private subnets with NAT, on-demand rather
than Spot (so a reclaim cannot be mistaken for an application fault mid-
validation), 14-day retention, and the MLOps plumbing on. A staging environment
configured exactly like the cheap demo proves only that the cheap demo works.
`image_tag` has **no `latest` fallback** here — staging exists to validate a
specific build, so an unset tag should fail loudly rather than quietly validate
whatever is sitting on a floating tag. Nothing is applied; `apply` stays pinned
to `TG_ENV`, manual.

**The CD seam.** `deploy:push-images` in the app repo pushes the **scanned
tarballs** (`docker load`, not a second `docker build`) to ECR tagged with the
commit SHA, and `deploy:trigger-infra` hands that tag to this pipeline. Both
manual, both self-skipping green without credentials. Rebuilding in the deploy
job would have published a different image from the one every gate passed —
exactly the substitution Agentic p8 warns about.

**Policy.** `policy/terraform.rego` gains four `deny` rules for the artifacts
bucket (public-access block on all four axes, encryption, versioning) and two
`warn`s (`force_destroy`, plaintext listeners). Public access is checked per
axis on purpose: three-of-four is not "mostly private", it is public by whichever
route was left open.

**`.checkov.yaml`.** The curated baseline [[Roadmap]] listed as offered and not
taken. Every skip carries a written reason; the file's header lists what is
**deliberately not skipped** — TLS, mutable tags, ALB access logs, flow logs and
WAF — so the report stays red where the repo genuinely is. A gate whose output is
undifferentiated is a gate nobody reads.

## 2026-09-17 (later) — the second apply would have failed

A second review pass, this one looking for plain defects rather than courseware
alignment. Most of the repo held up: RDS is encrypted and not publicly
accessible, the Ollama host requires IMDSv2 and encrypts its root volume, the
ALB drops invalid headers, and every security group is least-privilege except
where a comment explains why not. Two things did not.

**Secrets Manager reserves a deleted secret's NAME for 30 days, and nothing here
said otherwise.** All five secrets — `openai-api-key`, `onemap`, `onyx-api-key`,
`grafana-admin-password`, `db-credentials` — had fixed derived names and no
`recovery_window_in_days`, so they inherited the AWS default of 30. The sequence
this entire repo is built around (apply → demo → destroy → apply again) fails on
the **second** apply with `InvalidRequestException: You can't create this secret
because a secret with this name is already scheduled for deletion`.

Two details make it worse than an ordinary footgun, and are the reason it is now
written up in [[App Contract Settings]] §7 rather than just fixed:

* it fails at **apply** — the one step you run live, in front of people; and
* the nightly `scheduled_destroy` job, which exists to stop a forgotten stack
  billing overnight, is what arms it. Following the repo's own cost advice is
  what guarantees the next morning's failure.

Now `secret_recovery_window_days`, default `0`, on all five. `0` is right *here
and only here* — these secrets are re-created from CI variables on every apply,
so there is nothing to recover. The variable validates 0 or 7–30, because AWS
rejects everything in between.

**ECR would have grown without bound.** The lifecycle policy expired untagged
images only. That was survivable while every push overwrote `:latest`; it stops
being survivable the moment tags are immutable and each build pushes a distinct
SHA, because then nothing is ever untagged. Added a second, lower-priority
`tagStatus = any` rule keeping the newest `max_tagged_images` (default 10) —
enough to roll back several releases, which is the only reason to keep any. This
is a consequence of the immutability change earlier today, not an independent
finding.

## 2026-09-17 — courseware review, and the four free fixes it found

Read the DOAIS decks against this repo; findings and citations in
[[Lecture Alignment]]. Four things changed here as a result.

**1. The monitoring config had already drifted, and nobody could have known.**
`modules/monitoring_stack/config/alert.rules.yml` was 74 lines behind the app
repo — the whole `careroute-llm` group (LLM cost-per-triage, token quota, p95
latency, provider failure rate) was missing. Re-synced against app repo
`498b2b3` (2026-09-16); the file now carries both groups, 9 rules.

The failure mode is the interesting part: **Prometheus loads a rules file with a
group missing without complaining.** There is no error, no warning, no degraded
state — the alerts simply never fire, and the stack goes on claiming the LLM cost
alerting that closed the app's Gap 2. [[Roadmap]] had listed this exact risk as
hypothetical. It was not.

New `config_drift` job in the `validate` stage diffs the three *verbatim* copies
(`alert.rules.yml`, `alertmanager.yml`, `careroute-triage.json`) against a clone
of the app repo. It is a **hard gate** — silence is the whole problem, so a
warning would be the wrong instrument. `prometheus.yml` and the Grafana
datasource are deliberately excluded: they are templated here (Cloud Map DNS
instead of compose hostnames), so they are *expected* to differ, and diffing them
would only train people to ignore the job. Self-skips green without
`APP_REPO_URL`, the same contract `infracost` uses.

**2. A failed deployment now rolls itself back.** Every service built by
`modules/ecs_service` gets `deployment_circuit_breaker { enable, rollback }`
(variable `enable_deployment_rollback`, default on). Previously a task that
crash-looped on boot — bad image, missing secret, OOM during the severity-model
warmup — left ECS relaunching it indefinitely while `apply` had already returned
green. $0.

**3. Image identity.** `modules/ecr` now defaults to `IMMUTABLE`; the stack takes
`ecr_image_tag_mutability`, and `live/demo` reads `IMAGE_TAG` from the
environment, falling back to the `latest`/`MUTABLE` pair only when it is unset —
so the README's manual push still works, but the trade-off is now a recorded
decision rather than an inherited default.

Worth stating plainly because it is a live limitation, not a style point: with a
fixed `:latest` tag the rendered task definition is **byte-identical between
builds**, so ECS registers no new revision and an `apply` after a push deploys
nothing. It also leaves no earlier tag to roll back to — which is exactly what
the new circuit breaker needs in order to have somewhere to roll back *to*. The
two changes are one change. Closing it properly needs the app pipeline to push
`$CI_COMMIT_SHA` rather than `docker save` a tarball, which is an app-repo job.

**4. `.terraform.lock.hcl` is committed.** Provider patch versions are now pinned
for real; CI can no longer resolve a different `aws` build than a developer got.
The committed file carries full `zh:` registry hashes for every platform, so
Linux CI verifies against it even though it was generated on Windows — if a
platform-specific `h1:` is ever needed, regenerate with
`terraform providers lock -platform=linux_amd64 -platform=windows_amd64`.

Verified: `terraform fmt -check -recursive` clean, `terraform validate` on
`modules/careroute_stack` passes, all three YAML files parse, `alert.rules.yml`
loads as 2 groups / 9 rules.

## 2026-09-11 (later still) — Prometheus, Alertmanager and Grafana on ECS

New `modules/monitoring_stack`, on by default in `live/demo`. Rationale and the
three non-obvious decisions are in [[Monitoring Stack Design]].

**What prompted it.** The practice-module briefing asks for a physical
architecture diagram and an MLSecOps section naming the monitoring and alerting
tools. Worth recording precisely: the briefing **never names Prometheus** —
checked the PDF, zero occurrences of Prometheus, Grafana, CloudWatch, AWS,
Docker or Kubernetes. The requirement is "specify tools". Prometheus is our
choice, justified by the app already shipping it, not a rule being followed.

The real problem it fixes is coherence: with only the ADOT/CloudWatch path, the
demo shows Grafana from docker-compose while the architecture diagram shows
CloudWatch — the same metrics twice, in two places, unexplained.

**What it deploys**

- Prometheus, Alertmanager and Grafana as Fargate Spot tasks, running the app's
  own `alert.rules.yml`, `alertmanager.yml` and `careroute-triage.json`.
- Prometheus resolves the backend through Cloud Map and scrapes
  `backend.careroute.local:8000/metrics` on the app's own 15s interval. This is
  why the backend now registers in Cloud Map at all.
- Grafana at `/grafana` on the existing ALB (new optional target group + rule),
  with `GF_SERVER_SERVE_FROM_SUB_PATH`, so there is still one entry point and
  one hourly load-balancer charge. Its admin password is generated into Secrets
  Manager, not put in the task definition.
- Prometheus and Alertmanager have **no public route** — neither authenticates,
  and Prometheus exposes operational detail.

**Supporting changes**

- `ecs_service` gained `entry_point`, which is how stock images get their config
  on Fargate without EFS or a custom build.
- `alb` gained an optional Grafana target group and listener rule.
- `local.app_base_url` derived once and used for both the CORS allow-list and
  Grafana's root URL, so the two cannot drift.

**Verified:** fmt, validate, all six configs parse (YAML/JSON) with the rendered
templates pointing at the right Cloud Map targets, Checkov 187 passed / 55
failed (the two new failures are secret-rotation and an HTTP target group —
the same already-accepted categories as the existing resources).
**Not verified:** the containers have not been run. Docker is unavailable on
this machine, so the entrypoint config-injection is validated by inspection
only. First apply should check all three tasks reach RUNNING, Prometheus
`/targets` shows the backend UP, and the dashboard renders with data.

**Cost:** three Spot tasks, ~$0.02/hr — about 2 cents for a two-hour demo,
~$6/mo if left running.

**Follow-up (same day):** Prometheus, Grafana and Alertmanager added to the
**CI/CD diagram** too, as a second row in the deployed-stack box with the
caption "Monitoring & alerting — the app's own Prometheus tooling, not a cloud
substitute". Report §7 asks the CI/CD diagram itself to describe monitoring and
alerting, so naming the tools only in the runtime diagram left that section
unsupported. The Deploy legend entry now says the pipeline provisions the
observability stack rather than it being bolted on by hand.

Also corrected in [[Monitoring Stack Design]]: the Prometheus requirement comes
from the **lecture**, not the written briefing. The earlier entry said only that
the PDF does not name it, which was true but read as if the choice were
discretionary.

## 2026-09-11 (later) — Close two Checkov findings

Ran the repo's own Checkov gate locally (167 passed / 58 failed) and fixed the
two failures that cost nothing and were worth having.

- **`drop_invalid_header_fields = true` on the ALB** (CKV_AWS_131). Not generic
  hardening: it is load-bearing for the trusted-proxy change made earlier the
  same day. The backend now believes `X-Forwarded-For` from the ALB's subnets,
  and that trust is only sound if what reaches the task is a header the ALB
  actually produced. Without this, a caller could send a malformed or duplicated
  XFF and have it forwarded through the trusted path — the exact bypass the
  trusted-proxy check exists to close. See [[App Contract Settings]] §1.
- **Descriptions on the four security-group egress rules** (CKV_AWS_23), each
  saying what the rule is actually for and, for the ECS one, why it is not
  tightened (public endpoints with no stable CIDR; VPC endpoints cost more than
  the demo saves).

`terraform validate` rejected the first attempt: AWS restricts security-group
rule descriptions to `^[0-9A-Za-z_ .:/()#,@\[\]+=&;{}!$*-]*$`, so the em dashes
used everywhere else in this repo are illegal there. Rewritten with colons.

After: **172 passed / 53 failed**, both target checks passing.

The remaining 53 are the demo's deliberate trade-offs (no HTTPS/ACM, no WAF, no
KMS CMKs, no S3 access logging or replication, 3-day log retention, mutable ECR
tags) plus findings against RDS and Ollama, which are not deployed. They are not
yet curated — a reviewer still cannot tell an intentional trade-off from a real
regression. See [[Roadmap]].

## 2026-09-11 — Realign the stack with the integration branch

Read `careroute_ai_app@integration-all-agents-2026-08-31` and updated the IaC to
match what the application actually does. See [[App Contract Settings]] for the
settings this produced and [[App Overview]] for the resulting shape.

**Removed — dead wiring the app never used**

- The **Chroma** Fargate service, `enable_chroma`, `chroma_image` and the
  `CHROMA_URL` env var. The app has no Chroma client; retrieval is `rag.py`
  (in-process TF-IDF). The Cloud Map namespace is now created only for the
  sub-agent tier.

**Added — configuration the app reads and was not getting**

- `CAREROUTE_TRUSTED_PROXIES`, computed from the ALB's subnet CIDRs
  (`trust_alb_as_proxy`). Without it every client shared one rate-limit bucket.
- `CAREROUTE_CORS_ORIGINS`, computed from the deployed entry point instead of
  leaving the app's localhost-only default in a cloud deployment.
- `CAREROUTE_SSE_HEARTBEAT_SECONDS`, plus `alb_idle_timeout` (120 s) and a
  target-group `deregistration_delay`, so the SSE triage stream survives.
- `CAREROUTE_DEBUG_LLM=0` pinned, so prompt/response content (PHI) cannot reach
  CloudWatch Logs via an inherited environment.
- The Safety NLP block (`CAREROUTE_SAFETY_NLP*`), off, with the model paths
  pointing at in-image artifacts — never a download at request time.
- `CAREROUTE_REFLECTION_MAX_ITERS` / `_BUDGET_MS`, injected only when set, since
  a literal `0` would disable the loop rather than mean "use the default".
- **OneMap credentials** as one JSON Secrets Manager secret (`email` +
  `password`, so the pair rotates atomically) → `ONEMAP_EMAIL` /
  `ONEMAP_PASSWORD`. Without them Care Routing degrades to a labelled
  straight-line estimate.
- **Onyx** RAG inputs (`onyx_base_url` + `onyx_api_key` → Secrets Manager),
  injected only when both are present, matching `rag_onyx.py::_configured()`.

**Added — services**

- `modules/artifacts`: private, versioned, encrypted, TLS-only S3 bucket with a
  scoped read/write policy. Gives DVC a remote (the app's `.dvc/config` is
  empty) and the ML telemetry somewhere to survive a redeploy. Off by default.
- ADOT collector **sidecar** on the backend task (`enable_app_metrics`),
  scraping `127.0.0.1:8000/metrics` and exporting EMF to CloudWatch, plus five
  alarms and four dashboard rows translating the app's
  `monitoring/alert.rules.yml`. `ecs_service` grew a `sidecar_containers` input;
  `ecs_cluster` now outputs `task_role_name` so per-feature grants attach only
  when the feature is on.

**Added — guards** (`terraform_data.guards`, plan-time preconditions)

- More than one backend task is refused: `store.py` is in-process memory, so a
  second task silently loses escalations. Override:
  `allow_multi_task_backend`.
- API Gateway is refused unless `accept_api_gateway_sse_break` is set: an HTTP
  API integration times out at 30 s and buffers, killing the triage stream.
- `enable_app_metrics` requires ≥ 1024 MB so the sidecar does not take memory
  the model warmup needs.
- `backend_memory` validation at 1024 MB: below that the sklearn/SHAP warmup
  OOMs and the health check never goes green.

**Changed — defaults**

- `backend_memory` 512 → 1024 and `backend_cpu` 256 → 512 in `live/demo`
  (about a cent extra for a demo; the previous size did not boot).
- `enable_api_gateway` and `alb_internal` default to `false` — an internal ALB
  with nothing in front of it is unreachable.
- `enable_rds` defaults to `false`, with the reason stated in the variable.
- `backend_max_count` 4 → 1, consistent with the single-task guard.
- `sub_agents` renamed to the six workers actually in `backend/app/agents/`.

**Documented, not fixed**

- [[Clinical Endpoint Auth Gap]] — an application gap that infrastructure
  cannot close without breaking the clinician dashboard.

**Diagram + README**

- `docs/careroute-demo-runtime-architecture.drawio` redrawn against the new
  stack (official AWS4 icons, shape names checked against the draw.io library
  rather than guessed): the ADOT sidecar inside the backend task, CloudWatch →
  SNS, OneMap and Onyx as external services, the S3 artifacts bucket, no
  Chroma. Dashed = optional or startup-only. Re-exported to PNG at 2x with the
  editable XML embedded.
- `docs/careroute-cicd-pipeline.drawio` updated too. The pipeline's *jobs* did
  not change, but two things it depicts did: the "AWS demo stack" target box
  showed only ALB + ECS, and now shows what `apply` actually creates (ECS with
  the ADOT sidecar, Secrets Manager, CloudWatch → SNS, the optional S3 bucket);
  and `plan` is now a **hard gate**, not just a preview, because the guard
  preconditions fail it. The legend also lists the CI/CD variables, including
  the new optional `TF_VAR_onemap_*` and `TF_VAR_onyx_api_key`.
- `.gitlab-ci.yml` header documents those new variables and explains that a red
  `plan` may be a guard doing its job.
- README §1 now states the app-imposed constraints as a table, §5 lists the new
  toggles, §7 carries the auth blocker, §8 the new CI variables.

Verified: `terraform fmt -check -recursive` and `terraform validate` on
`modules/careroute_stack` both pass; the four guard preconditions were exercised
with `terraform plan` (3 pass / 3 fail as designed). Not applied to AWS.
