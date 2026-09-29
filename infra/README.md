# CareRoute AI — Infrastructure as Code (Terraform + Terragrunt)

Infrastructure for the **CareRoute AI (Evolved)** app (Team 3), provisioning the
AWS deployment from the proposal's cloud diagram. It is tuned for a **single,
cost-minimal `demo` environment** for the master's-degree presentation — stand it
up for the demo, then tear it down.

The application it deploys lives in `../careroute_ai_app` (FastAPI backend +
Next.js frontend).

> 💸 **Cost first:** this stack is *create-and-destroy* and stripped to the bare
> minimum. A couple of hours costs **cents to ~$1**; left running 24/7 it's
> ~$30/mo (ALB + five Fargate Spot tasks). **Always `terragrunt destroy` when
> the demo is done.** See [`COST.md`](./COST.md). The account currently runs on
> a $100 AWS Free Tier credit, so the burn shows on *Billing ▸ Credits* rather
> than as a charge.

> **Status (2026-09-22):** the `demo` stack is **up**, applied by the pipeline's
> manual `apply` (81 resources in the shared S3 state). Cluster
> `careroute-demo-cluster` runs five services at 1/1 — backend, frontend,
> Prometheus, Alertmanager, Grafana — behind one ALB; Grafana is at
> `<app_url>/grafana`. The app pipeline's `deploy:push-images` pushes over the
> OIDC role this stack created, and its manual `deploy:ecs` rolls the new image
> out on ECS from the app pipeline (see §8).

---

## 1. What the demo deploys

![CareRoute AI demo runtime architecture — an internet-facing ALB path-routes to a Next.js frontend task and a single FastAPI backend task on ECS Fargate Spot; the backend calls the OpenAI/Claude API and OneMap; a Prometheus, Alertmanager and Grafana tier runs in the same subnet with Prometheus scraping the backend over Cloud Map and Grafana served at /grafana; a telemetry-shipper sidecar syncs the inference log and clinician labels to the S3 artifacts bucket; CloudWatch holds the container logs and the infrastructure alarms that notify an SNS topic; Secrets Manager and ECR sit alongside](./docs/careroute-demo-runtime-architecture.png)

<sub>All five tasks — frontend, backend, Prometheus, Alertmanager, Grafana — run on **ECS Fargate SPOT** in **public subnets** (public IP, no NAT gateway). The ALB
path-routes `/api/*` to the backend and everything else to the frontend; its **120 s idle timeout** is
what keeps the SSE triage stream alive. Every credential — the LLM key, the OneMap pair, the staff API key,
the Grafana admin password — is held in **Secrets Manager** and injected as a container secret; images are
pulled from **ECR**, where the app repo's pipeline pushes them tagged by commit SHA through a push-only
**GitLab OIDC** role. The **S3 artifacts bucket** is on in `demo`: it is the app's DVC remote and where the
inference log and drift reports land, synced by a
**telemetry-shipper sidecar** (plain `aws-cli`, one-way `s3 sync`) that rides inside the backend task.
**CloudWatch** holds every task's logs plus the infrastructure alarms — backend CPU, ALB 5xx and the
deploy-rollback alarms ECS acts on — which notify an **SNS** topic (no email subscriber in `demo`).
**Prometheus, Alertmanager and Grafana** run as Fargate tasks in the same subnet, using the
app's own configs and dashboard; Prometheus finds the backend through **Cloud Map** and Grafana
is served at `/grafana` on the same ALB. Dashed boxes are startup-only. The optional pieces that are
**off** — the ADOT metrics sidecar, API Gateway, RDS, MLflow, the drift-monitor task, the sub-agent
tier — are not drawn; §5 lists the flags. Diagram source (editable):
[`docs/careroute-demo-runtime-architecture.drawio`](./docs/careroute-demo-runtime-architecture.drawio).</sub>

Everything expensive or unnecessary for a demo is **off**: no EC2 GPU, no RDS,
no NAT gateway, no API Gateway, no sub-agent tier. All of those remain one flag
away for a fuller build (see §5).

### One compute, on purpose — even though the app is now microservices

Since 2026-09-19 the application can run **one agent per container** (ten containers, reached over
HTTP through a gateway; `AGENT_TRANSPORT=http`). **This stack deliberately does not deploy that
shape.** It deploys the `monolith` image — the Dockerfile's *default* target, every agent in one
process — as a single ECS task.

That is a cost decision, and it is the difference between a stack that survives a semester and one
that does not:

| | one compute (what we deploy) | ten containers |
|---|---|---|
| ECS task size | the current one | 2 vCPU / 8 GB |
| ECR storage | 2 images | 12 images, ~1.1 GB each, billed per retained tag |
| Terraform change | none | `sidecar_containers`, 10 ECR repos, per-container secrets, scrape targets |

The split is still *proven* rather than abandoned: CI builds and scans all twelve images on every
pipeline and runs a real red-flag triage through all ten containers
(`scripts/compose_smoke.py`), but only the two images ECS actually pulls are pushed
(`CAREROUTE_PUSH_IMAGES` in the app repo). Adding a service to that list means creating its ECR repo
**here** in the same change — `module "ecr"` repositories, since ECR repos are not auto-created and a
push to a missing repo fails the job.

Consequence worth knowing: `AgentDown` and `AgentMetricsDown` exist in `alert.rules.yml` but are
**inert** under this shape — nothing emits `careroute_agent_calls_total` and there is no
`careroute-agents` scrape job here — so they cost nothing and cannot false-alarm. They begin working
the day the task definition grows containers.

The application-side split is drawn in the app repo at `docs/diagrams/careroute-agents.html`.

### Observability: two paths, and why both exist

The app exports a rich Prometheus surface (`backend/app/metrics.py` — escalations,
guardrail blocks, per-agent step latency, served acuity mix, clinician agreement)
and ships thirteen alert rules (`monitoring/alert.rules.yml`) plus a Grafana dashboard
for it; this repo adds one platform rule, `BackendNoScrapeTargets`. It can carry
those signals into AWS two ways, and they are independent:

| | `enable_prometheus_stack` **(on by default)** | `enable_app_metrics` (off) |
|---|---|---|
| What runs | Prometheus + Alertmanager + Grafana as Fargate Spot tasks | an ADOT collector sidecar in the backend task |
| Where metrics land | Prometheus TSDB, queried by Grafana | CloudWatch, as EMF |
| Alert rules | **the app's own `alert.rules.yml`, unchanged** | hand-translated CloudWatch alarms |
| p95 latency rules | ✅ real `histogram_quantile` | ❌ EMF flattens a histogram to a StatisticSet — mean only |
| The team's dashboard | ✅ the same `careroute-triage.json` | ❌ rebuilt as CloudWatch widgets |
| Cost | 3 Spot tasks, ~$0.02/hr | no extra task |
| Survives teardown | ✗ ephemeral | ✅ alarms outlive the stack |

`demo` runs the Prometheus stack and not the sidecar, and the diagram above draws only
what runs. The Prometheus stack is the default because a system architecture document and a
live demo should describe the same thing: without it you present Grafana from
docker-compose while the physical architecture diagram shows CloudWatch, and a
reader has to work out that those are the same metrics twice. Turn it off with
`enable_prometheus_stack = false` if you want the cheapest possible stack.

After apply:

```bash
terragrunt output -raw grafana_url               # <alb>/grafana
terragrunt output -raw grafana_admin_password    # user: admin
```

Prometheus and Alertmanager are deliberately **in-VPC only** — neither has
authentication of its own, and Prometheus exposes operational detail.

### What the infra does *because of* what the app does

The stack is wired against a specific application revision —
`careroute_ai_app@main` (446c23b, 2026-09-22; the 2026-08-31 integration branch
has since been merged into it) — and
several settings exist only because of how that app behaves. They are the ones
that look like tuning knobs and are actually correctness:

| Setting | Why it is not optional |
|---|---|
| `trust_alb_as_proxy = true` | The app's rate limiter ignores `X-Forwarded-For` from untrusted peers (`main.py::_client_key`). Behind an ALB the peer is *always* an ALB node, so without this every patient on earth shares one 30-req/min bucket and the second concurrent user gets a 429. |
| `alb_idle_timeout = 120` | `POST /api/triage/stream` is SSE held open for the whole run, with measured 24–30 s silent stretches. The AWS default of 60 s cuts the stream and the UI hangs on "Triaging…". |
| `sse_heartbeat_seconds = 10` | The app's own keepalive comment frame, which resets every idle timer on the path. Must stay well under the smallest timeout in front of it. |
| `backend_memory ≥ 1024` | The backend image carries scikit-learn + SHAP and warms a baked-in severity model at boot. At 512 MB it OOMs during warmup, which presents as an ALB health check that never goes green. |
| one backend task | `store.py` keeps cases, escalations, session history and the audit trail authoritatively **in process memory**. A second task means a clinician's `/api/escalations` and the patient's case live in different processes. The stack refuses to plan more (`allow_multi_task_backend` to override). **Since 2026-09-19 `store.py` does have a shared store — Redis, via `app/persistence.py` — and this is still one task:** Redis made a *restart* lossless, not a second task correct, because memory reloads only at start-up. Lifting it needs store.py to **read** through to that store per request. |
| `CAREROUTE_GROUND_TRUTH_LOG` on the telemetry volume | The clinician HITL decisions are the only labels this system ever gets, and `app/ml/monitor.py` joins them against the inference log **by case id** to compute *concept* drift. Unset, the app falls back to `backend/monitoring/ground_truth.jsonl` inside its own container — not on the shared volume — so the labels died with the task and the scheduled monitor read an always-empty path. Nothing errored; drift just reported no labelled rows for ever. Fixed 2026-09-19: it is the fourth entry in `local.telemetry_path_env`, and the reason that map is not three. |
| `enable_api_gateway = false` | An HTTP API integration caps at 30 s and buffers the body, so it breaks the SSE triage stream outright. Guarded by `accept_api_gateway_sse_break`. |

Two services in this repo exist for a shape the app has **not** reached yet, and
say so in their own comments rather than pretending otherwise: **RDS** (the
backend has no database client — no psycopg, no SQLAlchemy, no `DB_*` read) and
the **sub-agent tier** (the app reads no `AGENT_ROLE`; the six agents are
in-process objects, so the tier deploys idle replicas of the backend).

A third has been **removed**: the Chroma Fargate service and its `CHROMA_URL`
env var, which nothing in the app has ever read. Retrieval is `rag.py`
(in-process TF-IDF) and its real upgrade path is `rag_onyx.py` — so the RAG
inputs here are now `onyx_base_url` + `onyx_api_key`, pointing at an existing
self-hosted Onyx instance.

---

## 2. Repository layout

```
Infra/
├── root.hcl                     # Terragrunt: remote state (S3+DynamoDB) + AWS provider
├── .gitlab-ci.yml               # pipeline: validate → scan → plan → review → deploy → verify → cleanup
├── .tflint.hcl                  # tflint AWS ruleset config (provider-aware linting)
├── .checkov.yaml                # Checkov IaC scan config
├── policy/terraform.rego        # OPA/Conftest policy-as-code (runs against the plan JSON)
├── scripts/                     # used by the pipeline's apply / plan / rollback jobs
│   ├── verify_rollout.sh        #   fail apply if ECS rolled the release back
│   ├── running_image_tag.sh     #   plan keeps the tag the backend already runs
│   └── release_history.sh       #   current/previous image tag in SSM (rollback target)
├── COST.md                      # cost breakdown + levers
├── docs/                        # the two diagrams (.drawio + .png) and the Obsidian vault notes
├── live/
│   ├── demo/                    # the environment that is deployed
│   │   ├── env.hcl              # region + env name
│   │   └── terragrunt.hcl       # cost-minimal inputs
│   └── staging/                 # a fuller shape (frontend autoscaling on); not deployed
└── modules/                     # reusable Terraform modules
    ├── networking/              # VPC, public/private subnets, optional NAT
    ├── security/                # security groups (least-privilege firewall)
    ├── ecr/                     # image repositories
    ├── ecs_cluster/             # Fargate cluster, log group, IAM roles
    ├── alb/                     # ALB, target groups, path routing (/api/*, /grafana*, /api/escalations*)
    ├── api_gateway/             # HTTP API + VPC link (optional layer)
    ├── ecs_service/             # ⭐ reusable service (frontend/backend/agents) + sidecars
    ├── ci_oidc/                 # GitLab OIDC provider + the push-only role the app pipeline assumes
    ├── ollama_gpu/              # EC2 g5 GPU + Ollama (optional self-hosting)
    ├── rds/                     # Postgres (optional; the app has no DB client yet)
    ├── mlflow/                  # MLflow tracking server (optional)
    ├── scheduled_task/          # EventBridge-scheduled Fargate task (the optional drift monitor)
    ├── state_store/             # DynamoDB shared state — what the backend needs to scale out (optional)
    ├── artifacts/               # S3 for the DVC remote + ML telemetry (on in demo)
    ├── monitoring_stack/        # ⭐ Prometheus + Alertmanager + Grafana on Fargate
    ├── observability/           # CloudWatch dashboard, alarms, SNS + the app's own metrics
    └── careroute_stack/         # ⭐ composition wiring it all together
```

`ecs_service` is written once and reused for every container; `careroute_stack`
composes the modules and exposes toggles the `demo` environment sets.

---

## 3. Why Terragrunt

Even with one environment, Terragrunt removes two things you'd otherwise hand-manage:

1. **Remote state** — `root.hcl` configures the S3 backend + DynamoDB lock once and
   auto-creates them on first run (state key `demo/careroute/terraform.tfstate`).
2. **The provider block** — generated with region + `default_tags`, so no
   `provider {}` is hard-coded in the modules.

Adding another environment later is just a new `live/<env>/` folder — the module
code doesn't change. (For a single throwaway env you *could* use plain Terraform
with a backend file; Terragrunt keeps it tidy and future-proof.)

---

## 4. Deploy the demo

Prereqs: **Terraform ≥ 1.9**, **Terragrunt**, **AWS CLI v2** authenticated
(`aws sts get-caller-identity` works), and an **OpenAI API key** (the demo
default; swap the base URL for an Anthropic key to use Claude instead — see §6).

```bash
export TF_VAR_openai_api_key=sk-...   # your OpenAI key (or set as CI var)

cd live/demo
terragrunt init
terragrunt apply

terragrunt output -raw app_url               # ← open this in the browser
# ... give your presentation ...
terragrunt destroy                           # ← the moment you're done
```

### Pushing the images (once, before apply works end-to-end)
ECS pulls `…/backend:$IMAGE_TAG` and `…/frontend:$IMAGE_TAG` from ECR, so build & push first.
The **app repo** (`../careroute_ai_app`) owns image build & push in its own pipeline
(`build:*` jobs, after the MLOps `train`/`model-gate` stages); **this infra repo does
not build images** — it only provisions the ECR repos they land in.

In normal operation nobody pushes by hand: the app's `deploy:push-images` job assumes
the push-only IAM role this stack creates (`modules/ci_oidc`; trust keyed on the app
project's **numeric id**, see §8) and pushes `:<tag>`, then the app's `deploy:ecs`
rolls that tag out on ECS. The manual route below is the bootstrap
after a `destroy`, when the ECR repos come back empty and `plan` has no running tag to keep:

```bash
# Tag with the app commit, NOT :latest — see the note below.
export IMAGE_TAG=$(git -C ../../../careroute_ai_app rev-parse --short HEAD)

REPO=$(terragrunt output -raw ecr_backend_repo)         # after a first apply creates the repos
aws ecr get-login-password --region ap-southeast-1 | docker login --username AWS --password-stdin "${REPO%/*}"
docker build -t "$REPO:$IMAGE_TAG" ../../../careroute_ai_app/backend && docker push "$REPO:$IMAGE_TAG"
# repeat for ecr_frontend_repo, then:
terragrunt apply                                        # picks up $IMAGE_TAG
```

> **Why not `:latest`.** `IMAGE_TAG` defaults to `latest` so the commands above
> still work unset, but a fixed tag has a cost worth knowing before the demo:
> the rendered task definition is byte-identical between builds, so ECS registers
> no new revision and **`apply` after a push deploys nothing** — you would have to
> force a new deployment by hand. It also leaves no earlier tag to roll back to,
> which is what the ECS deployment circuit breaker needs in order to have
> somewhere to roll back *to*. Exporting `IMAGE_TAG` fixes all three, and flips
> the ECR repos to `IMMUTABLE` automatically. Same variable in CI:
> `TF_VAR_image_tag` / `IMAGE_TAG=$CI_COMMIT_SHA`.

Without a key set the backend still runs in **deterministic-fallback mode** ($0 LLM),
so you can demo the infra + app flow even before wiring ChatGPT.

---

## 5. Turning features back on (optional)

Every stripped-out piece is a single input in `live/demo/terragrunt.hcl`:

| Want | Set |
|---|---|
| ChatGPT via API key (default) | `openai_model = "gpt-4o-mini"` + `TF_VAR_openai_api_key` (OpenAI key); `openai_base_url` defaults to OpenAI |
| Claude instead | `openai_base_url = "https://api.anthropic.com/v1"` + `openai_model = "claude-opus-4-8"` + `TF_VAR_openai_api_key` (Anthropic key) |
| Self-host on a GPU | `enable_ollama = true` + add `ollama` to `llm_provider_order` (adds an EC2 g5 — expensive) |
| **Real travel times in Care Routing** | `TF_VAR_onemap_email` + `TF_VAR_onemap_password` (free account at onemap.gov.sg) |
| **The app's own metrics in CloudWatch** | `enable_app_metrics = true` (ADOT sidecar → EMF; adds the clinical alarms + dashboard rows) |
| **Prometheus + Alertmanager + Grafana** | `enable_prometheus_stack = true` — **already on** in `live/demo`; see §1 |
| **A DVC remote / somewhere for drift reports** | `enable_artifacts_bucket = true` + `enable_ml_telemetry_shipping = true` — **already on** in `live/demo`; `terragrunt output -raw dvc_remote_url` prints the remote and the app CI role can read/write it |
| Private networking + NAT | `public_networking = false` |
| API Gateway in front | `enable_api_gateway = true`, `alb_internal = true`, `accept_api_gateway_sse_break = true` — **breaks the triage stream**, see §1 |
| Persistent SQL state | `enable_rds = true` (provisions the database; the app cannot use it yet) |
| Onyx-backed RAG | `onyx_base_url = "https://…"` + `TF_VAR_onyx_api_key`, pointing at an Onyx you host separately |
| One task per agent | `enable_subagent_tier = true` (topology only — see §1) |
| **Frontend autoscaling** | `frontend_enable_autoscaling = true` + `frontend_max_count` (on in `live/staging`) — see *Scaling* below |
| **Shared state table (DynamoDB)** | `enable_shared_state_store = true` (provisions the table; the app cannot use it yet) |
| On-demand floor under Spot | `fargate_on_demand_base = 1` |

### Scaling

The design lives in `modules/careroute_stack/scaling.tf`. Each tier is either
scaling already or needs one switch, **except the backend, which the app blocks
today**:

| Tier | Scales? | How / what blocks it |
|---|---|---|
| ALB | ✅ | AWS scales it. |
| Frontend (Next.js) | ✅ stateless | Target tracking on CPU and, optionally, ALB requests per task (`frontend_enable_autoscaling`, `frontend_requests_per_target`). On in `live/staging`: 1 → 3 tasks. |
| Backend (FastAPI) | ⛔ one task | Cases, escalations, session memory, the audit trail and rate-limit windows are **process memory** (`store.py`, `audit.py`, `ratelimit.py`). Two tasks would each hold half the clinical record. The guard in `main.tf` refuses to plan it. |
| Backend state | 🟡 ready, off | `modules/state_store`: one DynamoDB on-demand table ($0 idle, no sizing, PITR, append-only audit enforced in IAM), a free gateway endpoint, and `CAREROUTE_STATE_*` env vars — the contract the app still has to implement. |
| LLM | provider-side | Bounded by the provider's rate limits and the app's cost guards, not by this stack. |
| Monitoring | ✅ | Prometheus finds backend tasks by DNS (`dns_sd_configs`), so it scrapes 1 or N tasks with no change, and every dashboard/alert query already `sum()`s across them. `BackendNoScrapeTargets` (`config/platform.rules.yml`) covers the one case DNS discovery hides: no tasks at all. |

What is already in place for the backend, and does nothing until it runs more
than one task:

- **Least-outstanding-requests routing** on the backend target group. SSE triage
  streams stay open ~30 s; round robin counts requests, not how long they stay
  open, and would keep adding runs to a task that is already busy.
- **Request-count scaling** (`backend_requests_per_target`), because the backend
  mostly waits on the LLM: CPU stays low while requests pile up, so CPU alone
  scales out late. Several policies can run together: the service scales out
  when any one of them asks, and scales in only when all of them agree. Scale-in
  waits 300 s after the last one, because removing a task cuts off its open streams.
- **An on-demand floor under Spot** (`fargate_on_demand_base`): a Spot reclaim
  can take burst tasks, but never the last one.

To scale the backend out: (1) the app implements its store against
`CAREROUTE_STATE_TABLE`; (2) `enable_shared_state_store = true`; (3)
`allow_multi_task_backend = true`, `enable_autoscaling = true`,
`backend_max_count = N`. Step 3 is refused at plan time without step 2. The
per-client rate limit (`rate_limit_per_min`) is enforced by each task for its own
clients, so with N tasks one client can get up to N× that limit, until
`ratelimit.py` moves its counters to the shared table too.

### The artifacts bucket and DVC (done for `demo`)

This closed the gap the app repo recorded in
`docs/vault/Infra-Dependent Work 2026-09-02.md` §4. Since 2026-09-22 the app's
`backend/.dvc/config` on `main` has its default remote at
`s3://careroute-demo-artifacts-<acct>/dvc` (a `gitlab` package-registry remote stays
as the fallback for branches that cannot assume the AWS role), and the app's
`data:version` job pushes on `main` through the OIDC role. A fresh clone restores
`triage_dataset.npz` with `dvc pull`. For another environment, repoint it:

```bash
terragrunt output -raw dvc_remote_url          # s3://careroute-<env>-artifacts-<acct>/dvc
cd ../../../careroute_ai_app/backend
dvc remote modify s3 url "$(…that url…)"
git add .dvc/config && git commit -m "chore(dvc): point the S3 remote at <env>"
dvc push
```

---

## 6. LLM providers (ChatGPT, or Claude via the same provider)

The backend (`../careroute_ai_app/backend/app/llm.py`) tries providers in
`LLM_PROVIDER_ORDER`; the first that works wins, else deterministic fallback.
The app ships **three** providers — note there is no native `anthropic` one:

| Provider (name) | Uses | Works in the cloud container? |
|---|---|---|
| `openai` | OpenAI-compatible Chat Completions + `OPENAI_API_KEY` at `OPENAI_BASE_URL` | ✅ **the demo default** |
| `claude_cli` | local `claude` binary (Claude Code login) | ❌ local dev only (no binary in a container) |
| `ollama` | self-hosted GPU | ✅ but costly |

The demo uses **ChatGPT through the `openai` provider** — `OPENAI_BASE_URL`
defaults to `https://api.openai.com/v1` with an OpenAI key and `gpt-4o-mini`.
Because the provider is OpenAI-*compatible*, you can point `OPENAI_BASE_URL` at
any drop-in endpoint with no app code change: Anthropic's OpenAI-compat endpoint
(`https://api.anthropic.com/v1` + a `claude-*` model + an Anthropic key) to run
Claude, or an OpenRouter URL for other models.

The API key lives in **Secrets Manager** and is injected as a container secret —
never in the task definition, code, or state-as-plaintext.

---

## 7. Security notes (kept sane even in demo mode)

- Tasks have public IPs (needed for egress without a NAT), but their security group
  **only accepts inbound from the ALB** — not the internet directly.
- Every credential is in **Secrets Manager**, injected at runtime: the LLM API key,
  the OneMap email/password (one JSON secret, so the pair rotates as a unit), the
  staff API key, the Grafana admin password, and the Onyx key when Onyx is configured.
  `CAREROUTE_DEBUG_LLM` is pinned to `0` so prompt/response content (PHI)
  can never reach CloudWatch Logs, even if an inherited environment sets it.
- IMDSv2 enforced on any EC2; ECR scan-on-push; S3 artifacts bucket is private,
  versioned, encrypted and TLS-only; `force_delete` so demo teardown is clean.
- Trade-off for cheapness: no API Gateway auth/rate-limit layer (the app has its own
  in-process rate limit) and no private-subnet isolation. Turn both on via §5 for a
  hardened build — but read the SSE warning first.

> ### Clinical endpoints: staff API key (closed 2026-09-19)
>
> `/api/escalations*` returns every escalation with the patient's free-text
> presentation, and `POST /api/escalations/{id}/decision` closes a clinician
> review. The app now guards them with `CAREROUTE_STAFF_API_KEY` (header
> `X-Staff-Key`, 401 otherwise), attached server-side by the frontend's
> Route Handler so the key never reaches the browser.
>
> This stack now makes that real on AWS (`modules/careroute_stack/staff_auth.tf`,
> on by default via `enable_staff_api_key`): it generates the key into Secrets
> Manager, injects it into **both** tasks, and adds an ALB rule (priority 5, ahead
> of `/api/*`) sending `/api/escalations*` to the **frontend**. Before this, the
> ALB sent those paths straight to the backend, skipping the Route Handler; and
> since the key was never set, the backend ran in its open demo mode. The
> handler reaches the backend over Cloud Map (`backend.careroute.local:8000`),
> never back through the ALB, which would route it to the frontend again.
>
> Still true: a shared key is not per-user identity. For anything beyond a demo,
> put an identity layer in front (Cloudflare Access, Entra, Keycloak, or an API
> Gateway JWT authorizer once the triage stream no longer needs to pass through it).

---

## 8. CI/CD (GitLab)

`.gitlab-ci.yml` is a deliberately **comprehensive** IaC pipeline — a client-facing
reference for shipping Terraform properly — that still keeps **AWS spend near zero**:
every gate runs in a throwaway CI container and provisions *nothing*; the only job
that creates billable resources is the manual `apply`.

![CareRoute AI IaC CI/CD pipeline — GitLab stages validate, scan, plan, review, deploy, verify, cleanup, where only deploy provisions the billable AWS demo stack: ALB, ECS Fargate with five services and a telemetry sidecar, Secrets Manager, CloudWatch with SNS alarms, the S3 artifacts bucket, and a monitoring tier of Prometheus, Grafana and Alertmanager](./docs/careroute-cicd-pipeline.png)

<sub>**Monitoring is deployed by the pipeline, not bolted on afterwards** — the `apply` job provisions
Prometheus, Alertmanager and Grafana alongside the app, running the same configs the app uses locally.
Diagram source (editable): [`docs/careroute-cicd-pipeline.drawio`](./docs/careroute-cicd-pipeline.drawio).</sub>

| Stage | Jobs | What / why |
|---|---|---|
| validate | `validate`, `tflint` | `fmt` + `terragrunt validate`; **tflint** adds provider-aware linting |
| scan | `checkov`, `gitleaks` | **Checkov** IaC security scan → MR *Security* tab (soft); **gitleaks** secret scan (**hard gate**) |
| plan | `plan` | emits `plan.txt` + machine-readable `plan.json` (feeds the review gates) — **and is itself a hard gate**: the stack's guard preconditions fail the plan for combinations that would apply cleanly and then misbehave (see §1) |
| review | `opa_policy` | **OPA/Conftest** policy-as-code over `plan.json` (Infracost was removed: it needs an account key) |
| deploy | `apply` | **manual** (also when the app pipeline triggers it with `IMAGE_TAG`); gated on `gitleaks`. Then `scripts/verify_rollout.sh` **fails the job if ECS rolled the release back** |
| deploy | `rollback` | re-applies the **previous verified** image tag (SSM release history); manual, or automatic when the app triggers `ROLLBACK_REQUESTED=true` |
| verify | `smoke_test` | **manual**; frontend root + `/api/health` + `/api/fairness` reachability |
| cleanup | `destroy`, `scheduled_destroy` | **manual** teardown + a **scheduled** nightly auto-destroy safety net |

**Design choices worth calling out in a demo:** security/cost/policy gates are
**soft** (report, don't block) so the demo's *intentional* cost trade-offs (public
subnets, no NAT gateway) surface for review rather than failing the run — `gitleaks`
is the one hard gate. Deep model/behaviour/security testing (train → model-gate →
Evidently drift → AI red-team → DAST) lives in the **app repo's** pipeline, not here.

Add masked CI variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_DEFAULT_REGION`, `TF_VAR_openai_api_key`, optionally
`TF_VAR_onemap_email` + `TF_VAR_onemap_password` (real travel times in Care
Routing instead of a labelled straight-line estimate), `TF_VAR_grafana_admin_password`
(only to pick your own; otherwise one is generated), `TF_VAR_onyx_api_key`
(only if you host Onyx). The GitLab *Environment*
links apply↔destroy so you can tear down from the Environments UI; add a **Schedule**
(CI/CD ▸ Schedules) to arm the nightly `scheduled_destroy`.

> **Scope split (MLOps lives in the app repo).** CareRoute *does* own a trained
> severity-classifier model, so it *does* have real MLOps — but that pipeline
> (model training, release/fairness gates, drift monitoring, AI-security scans)
> belongs to `../careroute_ai_app`. This infra repo is IaC-only: it provisions and
> tears down the AWS stack the app runs on.
>
> The two places that split meets are provisioned here rather than there, because
> neither can exist without infrastructure: the **artifacts bucket** that gives
> DVC a remote — with the **telemetry-shipper sidecar** that syncs the inference
> log and clinician labels into it, so drift reports survive a redeploy — and the
> **Prometheus stack** that runs the rules in its `monitoring/alert.rules.yml`
> unchanged. (An ADOT sidecar can mirror those metrics into CloudWatch;
> `enable_app_metrics`, off.)

---

### Releases and rollback

```
app repo   deploy:push-images ─▶ ECR careroute-<env>/{backend,frontend}:<tag>   (app-CI OIDC role)
           deploy:ecs (manual) ─▶ scripts/deploy_ecs.sh: new task-def revision with <tag>
                                  ─▶ update-service ─▶ wait + verify ─▶ record <tag> in SSM
infra repo plan ─▶ apply (manual): infrastructure only; keeps the running tag
```

| What goes wrong | What reverts it | How |
|---|---|---|
| New tasks never become healthy | ECS circuit breaker | automatic; previous task definition comes back |
| Tasks start, then serve 5xx above `deploy_rollback_error_rate` (1%) | ECS, on the `*-deploy-rollback` CloudWatch alarm | automatic, during the rollout **and** its bake period |
| Found bad later | the app's `rollback:production` | rolls out `/careroute/<env>/image-tag/previous` |

In every automatic case the app's `deploy:ecs` job **fails**: `deploy_ecs.sh` checks the
service settled on the tag that was deployed, not merely that it is stable. The app's
`rollback:production` runs the same script onto the previous tag and re-pins the previous
model in MLflow. This repo's `rollback` job (`ROLLBACK_REQUESTED=true`) remains a
Terraform-side fallback.

Three details that are easy to get wrong:

- **IMAGE_TAG is no longer defaulted to this repo's commit.** That SHA names no image.
  With no `IMAGE_TAG`, `plan` keeps the tag the backend is already running
  (`scripts/running_image_tag.sh`), so an infra-only change never swaps the app build.
- **The app pipeline changes only the image tag; Terraform owns everything else.** A deploy
  registers a revision copied from the running one with the tag swapped. The next infra
  `plan` keeps the running tag (`running_image_tag.sh`), so it re-registers the same image
  rather than undoing the deploy. The drift-monitor schedule pins its revision, so it moves
  to a new tag on the next infra `apply`.
- **No blue/green or canary yet.** ECS's native strategies (`deployment_configuration.strategy`)
  need AWS provider 6.x and the stack is pinned to `~> 5.60`. Rolling + circuit breaker +
  alarms is what 5.x supports, and it covers both automatic cases above.

One-time setup:

1. `terragrunt apply` once, then copy `terragrunt output app_ci_variables` into the **app**
   project's CI/CD variables. Protect the app's `main` branch — the push role trusts it.
   In the app project set *Settings ▸ CI/CD ▸ Token Access ▸ ID token subject claim* to
   `project_id`, `ref_type`, `ref`: the role's trust policy is keyed on the app project's
   **numeric id** (`app_ci_project_id`, default `84456994`), not its path, because GitLab
   refuses to mint a path-based token for a project on a previously used path
   (`id_token_burned_project_path`; see `docs/vault/Changelog.md`, 2026-09-21).
2. Optional, for a Terraform-side deploy/rollback started from the app: a pipeline trigger token
   here, given to the app project as `INFRA_TRIGGER_TOKEN` + `INFRA_PROJECT_ID`. Normal deploys
   no longer use it.
3. A second environment in the same AWS account sets `create_gitlab_oidc_provider = false`.

## 9. Cost

See [`COST.md`](./COST.md). Headline: **destroy when done** — a demo is cents-to-~$1;
the only always-on cost left is the ALB (~$0.02/hr) plus five Spot tasks (two app,
three monitoring), about $3/day at on-demand rates and less on Spot. With the
account on the $100 Free Tier credit, that is roughly a month of continuous
running — the *Billing ▸ Credits* page lags usage by about a day.

## 10. Validation

Passes `terraform fmt` and `terraform validate` (all cross-module references, both
the minimal-demo and fully-featured paths). Reproduce:

```bash
cd live/demo && terragrunt init && terragrunt validate
```
