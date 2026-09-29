---
tags: [active, careroute, infra]
updated: 2026-09-17
---
# Roadmap

Back to [[Home]]. Related: [[App Overview]] · [[App Contract Settings]] ·
[[Lecture Alignment]]

What is deliberately not built, and what would have to be true first. Ordered by
what blocks the most.

## Blocked on the application

- **Authentication on the clinical endpoints** — [[Clinical Endpoint Auth Gap]].
  The infra half (a Secrets Manager secret + env wiring) is an hour's work the
  moment the app reads `CAREROUTE_STAFF_API_KEY`.
- **Shared state for the backend** — until `store.py` has a backing store, the
  service cannot scale past one task. The infra half is done
  (`modules/state_store`, DynamoDB, behind `enable_shared_state_store`; see
  `careroute_stack/scaling.tf`). The app half: implement `store.py`,
  `audit.py` and `ratelimit.py` against `CAREROUTE_STATE_TABLE`. Then set
  `allow_multi_task_backend` + `enable_autoscaling`. No ALB stickiness is
  needed once state is shared: any task can resume any session.
- **A real sub-agent split** — the tier deploys idle replicas until the agents
  talk over HTTP instead of an in-process bus.

## Blocked on a decision

- **Durable ML telemetry.** The backend writes its inference log and drift
  reports to a Fargate filesystem that every deploy destroys. The artifacts
  bucket is provisioned; nothing ships to it yet. Options: an S3 write from the
  app, a log-based path via CloudWatch, or EFS (rejected for a demo — it is an
  always-on cost and a mount point for a stateless task).
  **The courseware settles this: S3.** `DataCaptureConfig` → S3 JSON lines is
  the worked example in both AWS decks, and "store metrics and logs in a log
  repository and storage such as S3 for audits" is a named best practice. See
  [[Lecture Alignment]] §6.
- **Nothing runs on a schedule.** No EventBridge, Lambda, Step Functions or
  Scheduler anywhere in this repo, so the drift→retrain loop exists only as a CI
  job and never fires in the cloud. An EventBridge Scheduler rule →
  `ecs:RunTask` on the existing cluster is the cheap version of the hourly
  monitoring schedule the decks build as their worked example.
  [[Lecture Alignment]] §5.
- **Grafana state is ephemeral.** Fargate task storage, so any dashboard edited
  in the UI is lost on redeploy. Provisioned dashboards survive; ad-hoc ones do
  not. Durable Grafana needs RDS or EFS.
- **MLflow tracking backend.** The app's champion-challenger check is inert
  because runs do not survive between CI jobs. Needs a tracking server or
  GitLab's MLflow-compatible registry — a decision, then `MLFLOW_TRACKING_URI`.

## Known, offered, not taken (2026-09-11)

These were surfaced by running Checkov locally and reviewed with James, who
chose the free security fixes only. Recorded so they are decisions, not
oversights:

- **Curated `.checkov.yaml` baseline.** 53 findings remain and are
  undifferentiated: a reviewer cannot tell a deliberate demo trade-off from a
  real regression. A baseline with a written reason per skip would make the
  `checkov` job meaningful instead of noise.
- **OPA rule for S3.** `policy/terraform.rego` covers security-group ingress and
  RDS encryption only. The artifacts bucket holds a (synthetic) clinical
  dataset and has no policy rule of its own.
- ~~**`.terraform.lock.hcl` is gitignored**~~ — taken 2026-09-17. See
  [[Changelog]]. It was the one item here that a courseware principle names
  directly ("keep everything versioned — code, configuration, infrastructure").

## Nice to have

- **HTTPS.** The demo ALB is HTTP-only; TLS needs a domain and an ACM
  certificate. Worth doing before anything real, and required for an IdP.
  Checkov flags this four ways (CKV_AWS_2, CKV2_AWS_20, CKV_AWS_103,
  CKV_AWS_378); it is the largest remaining security gap for a healthcare app.

- **Per-agent least-privilege task roles** — one role per service instead of the
  shared base role, once the agents are actually separate services.
- **A second environment.** The module code already supports it; it is one new
  `live/<env>/` folder. Not worth it while the point is a single demo — but it
  is the courseware requirement we meet least (staging validation, dev/test/prod
  segregation, promote-the-same-artifact), so say it as a scope decision rather
  than let a marker find it. [[Lecture Alignment]] §4.
- **Security groups cannot be edited in place.** `description` and `name` are
  immutable in EC2, so touching either destroys and recreates the group — and the
  `vpc_link` group cannot be destroyed while the API Gateway VPC link's ENIs hold
  it. Give every SG `lifecycle { create_before_destroy = true }` plus
  `name_prefix` (two groups cannot share a name during the overlap) so attribute
  changes stop being outages-in-waiting. Deferred on 2026-09-21 because enabling
  it forces one replacement of its own, which must not ride along with the apply
  that is currently blocked. [[Changelog]] 2026-09-21.

## Resolved

- ~~Monitoring config drift~~ — done 2026-09-17. The `config_drift` CI job now
  hard-fails on divergence from the app repo. Closing it found the drift had
  *already happened*: `alert.rules.yml` was missing the whole `careroute-llm`
  group. [[Lecture Alignment]] §1.
- ~~No automatic rollback on a failed deployment~~ — done 2026-09-17. ECS
  deployment circuit breaker with `rollback = true` on every service.
- ~~No metric-driven rollback~~ — done 2026-09-19. 5xx-rate alarms rolled back on by
  ECS, a verify step that fails a reverted release, and a manual `rollback` job.
- Native blue/green or canary (`deployment_configuration.strategy`) — needs the AWS
  provider 5.x → 6.x upgrade first.
- ~~p95 latency alarms need Amazon Managed Prometheus~~ — done 2026-09-11.
  `enable_prometheus_stack` runs the app's `alert.rules.yml` unchanged, so both
  `histogram_quantile(0.95, ...)` rules work as written, with no managed-service
  floor to pay. See [[Monitoring Stack Design]].
