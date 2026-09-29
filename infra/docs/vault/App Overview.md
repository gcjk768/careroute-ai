---
tags: [active, careroute, infra]
updated: 2026-09-17
---
# App Overview — what this repo provisions

Back to [[Home]]. Related: [[App Contract Settings]] · [[Changelog]] ·
[[Clinical Endpoint Auth Gap]] · [[Lecture Alignment]]

Terraform modules composed by Terragrunt into one AWS stack for the CareRoute AI
triage app. Verified against `careroute_ai_app@integration-all-agents-2026-08-31`
on 2026-09-11.

## Runtime shape (demo)

Rendered, with icons and a numbered request flow:
`../careroute-demo-runtime-architecture.png` (source `.drawio` beside it, with
the editable XML embedded in the PNG). The sketch below is the same thing in
text, for when you just need the shape.

```
Browser ──▶ internet-facing ALB :80
              ├── /*      ─▶ frontend  (Next.js standalone, :3000, Fargate Spot)
              └── /api/*  ─▶ backend   (FastAPI, :8000, Fargate Spot)
                               ├─▶ hosted LLM (OpenAI-compatible endpoint)
                               ├─▶ OneMap  (travel times; Secrets Manager creds)
                               ├─▶ Onyx    (RAG; only if configured)
                               └─▶ CloudWatch logs
                              └ sidecar: ADOT ─▶ CloudWatch EMF (app metrics)
```

The `/api/*` listener rule is load-bearing: the browser calls `/api`
same-origin, and the frontend image's own Next `rewrites()` fallback was baked
at build time pointing at a compose hostname that does not resolve in the VPC.

## Modules

`modules/careroute_stack/` composes everything; `live/demo/terragrunt.hcl` sets
the inputs.

| Module | Provisions | Notes |
|---|---|---|
| `networking` | VPC, public/private subnets, optional NAT | demo runs tasks in public subnets, no NAT |
| `security` | every security group | tasks accept inbound from the ALB only; `modules/security/main.tf` SG `description`/`name` are immutable — editing either forces a replacement ([[Changelog]] 2026-09-21) |
| `ecr` | one repo per image | images are built by the **app** repo's pipeline |
| `ecs_cluster` | Fargate cluster, log group, exec + task roles | Container Insights on |
| `alb` | ALB, two target groups, `/api/*` rule | `idle_timeout` is an SSE correctness setting |
| `ecs_service` | ⭐ reusable service + optional sidecars | one call per container service |
| `api_gateway` | HTTP API + VPC link | optional; **breaks the SSE triage stream** |
| `rds` | Postgres | optional; see "provisioned for a future app" below |
| `artifacts` | private S3: DVC remote + ML telemetry | optional |
| `observability` | CloudWatch dashboard, alarms, SNS, saved Logs Insights queries | includes the app's own metrics |
| `scheduled_task` | EventBridge Scheduler → `ecs:RunTask` | the drift monitor; off by default |
| `mlflow` | MLflow tracking server on Fargate, S3 artifact root | off; internal-only, no auth |
| `ci_oidc` | GitLab OIDC provider + ECR-push and ECS-image-rollout role for the **app** repo (`modules/ci_oidc/main.tf`) | role `careroute-demo-app-ci-ecr-push`; trust is keyed on the app project's **numeric id** (84456994), never its path ([[Changelog]] 2026-09-21 second) |

## The three services that exist for an app that does not exist yet

Named explicitly so nobody re-discovers them as bugs:

1. **RDS** — the backend has no database client at all (no psycopg, no
   SQLAlchemy) and reads no `DB_*` variable. `store.py` is a dict. The module
   stays as the migration target; the env vars are injected so the app can pick
   them up the day it grows one.
2. **Sub-agent tier** — the app reads no `AGENT_ROLE`. Its six agents are
   in-process objects behind an in-memory A2A bus, so turning the tier on
   deploys idle replicas of the whole backend. It is the proposal's topology,
   not a working split.
3. **Chroma** — *removed* on 2026-09-11. The app has never had a Chroma client
   and never read `CHROMA_URL`. Retrieval is `rag.py` (in-process TF-IDF); the
   documented upgrade path is `rag_onyx.py`, so the stack now takes
   `onyx_base_url` + `onyx_api_key` for an Onyx instance hosted elsewhere.

## Observability

CloudWatch covers the infrastructure (CPU, memory, ALB 5xx). The *clinical*
signals — escalation rate, guardrail blocks, per-agent step latency, served
acuity mix, clinician agreement — only exist on the app's Prometheus `/metrics`,
and there are two independent ways to carry them into AWS.

**`enable_prometheus_stack` (default ON).** Prometheus, Alertmanager and Grafana
as Fargate Spot tasks, running the app's *own* `alert.rules.yml`,
`alertmanager.yml` and `careroute-triage.json`. Prometheus resolves the backend
through Cloud Map and scrapes it directly; Grafana is served at `/grafana` on the
app's ALB. See [[Monitoring Stack Design]] for how the configs get into stock
images on Fargate, and for the config-drift risk that creates.

**`enable_app_metrics` (default off).** An ADOT collector **inside the backend
task**, scraping `127.0.0.1:8000/metrics` over the shared network namespace and
exporting EMF — no second task, no new network path. The alarms in
`observability` are a hand-translation of the same rules.

They solve different problems and can both be on. The Prometheus path gives the
real `histogram_quantile` p95 rules and the actual dashboard, but dies with the
stack. The CloudWatch path survives teardown and needs no extra compute, but EMF
flattens a histogram to a StatisticSet, so it can only alarm on *mean* latency.
