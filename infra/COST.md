# CareRoute AI — Infrastructure cost guide (demo)

> **Golden rule:** this stack is *create-and-destroy* and you pay AWS **by the
> hour**. Run it for the presentation, then **`terragrunt destroy`**. A two-hour
> demo costs **cents to ~$1**. The scary "per-month" numbers only apply if you
> forget to tear it down.

Prices are rough on-demand `ap-southeast-1` (Singapore), USD, for orientation.

---

## 1. What the demo actually runs

The `demo` environment strips everything optional. What's left:

| Resource | Hourly | ~2-hour demo | If left on 24/7 |
|---|---|---|---|
| ALB (internet-facing) | ~$0.0225/hr + tiny LCU | ~$0.05 | ~$16/mo |
| Backend Fargate **Spot** (0.5 vCPU/1 GB) | ~$0.006/hr | ~$0.02 | ~$4/mo |
| Frontend Fargate **Spot** (0.25 vCPU/0.5 GB) | ~$0.003/hr | ~$0.01 | ~$2/mo |
| Prometheus Fargate **Spot** (0.25 vCPU/0.5 GB) | ~$0.003/hr | ~$0.01 | ~$2/mo |
| Alertmanager Fargate **Spot** (0.25 vCPU/0.5 GB) | ~$0.003/hr | ~$0.01 | ~$2/mo |
| Grafana Fargate **Spot** (0.25 vCPU/0.5 GB) | ~$0.003/hr | ~$0.01 | ~$2/mo |
| Secrets Manager (2–4 secrets) | — | ~$0 | ~$0.80–1.60/mo |
| CloudWatch logs (3-day retention) | usage | cents | ~$1/mo |
| ECR storage (2 small images) | usage | ~$0 | <$1/mo |
| **OpenAI API (ChatGPT)** | per-token | cents (a few triage calls) | usage-based |
| **Total** | | **≈ cents–$1** | **≈ $30–33/mo** |

The three monitoring tasks (`enable_prometheus_stack`, on by default) add about
**2 cents to a two-hour demo**. They exist so the deployed architecture matches
the tooling the demo actually shows — see README §1. Set
`enable_prometheus_stack = false` to drop them and save ~$6/mo of always-on cost.

The backend moved from 0.25 vCPU/0.5 GB to 0.5 vCPU/1 GB — about **1 cent extra
for a whole demo**. It is not a luxury: the image carries scikit-learn and SHAP
and warms a baked-in model at boot, and at 512 MB the task OOMs during warmup and
never passes its health check. A stack that does not start is not cheaper.

On 2026-09-29 the demo backend moved again, to 2 vCPU/4 GB, because it now loads
the four Safety-NLP models (measured peak 2.14 GiB). At ap-southeast-1 Fargate
rates that is roughly **3 cents an hour more on Spot** (about 9 cents on-demand),
so well under 10 cents for a two-hour demo.

**Optional extras, both off by default:**

| Extra | Cost | What you get |
|---|---|---|
| `enable_app_metrics` | ~10–20 custom metrics (~$3–6/mo if left on) + a little log ingest; **cents for a demo** | The app's clinical/ML telemetry in CloudWatch — escalation rate, guardrail blocks, per-agent latency, clinician agreement — plus the alarms mirroring `alert.rules.yml`. The ADOT collector rides in the existing backend task, so there is **no extra task to pay for**. |
| `enable_artifacts_bucket` | S3 storage only; the dataset is ~50 MB → **well under $0.01/mo** | A DVC remote and a home for drift reports. Effectively free; the reason it is off is that a throwaway demo has nothing to keep. |

**Scaling inputs, all off in the demo** (README §5 *Scaling*):

| Input | Cost |
|---|---|
| `frontend_enable_autoscaling` | **$0 while idle**: the floor is still one task. Under load, each extra frontend Spot task is ~$0.003/hr, and `frontend_max_count` caps how many a spike can add. |
| `enable_shared_state_store` | DynamoDB on-demand: **$0 while idle**, then about $1.4 per million writes and $0.28 per million reads. PITR is billed per GB, which is cents at this data size. The gateway endpoint is free. |
| `fargate_on_demand_base = 1` | Takes the floor task off the Spot discount: about +$0.014/hr for the backend (the difference between on-demand and Spot). |

**Container Insights is off** (`enable_container_insights = false`). It was on
unconditionally. It bills as CloudWatch custom metrics plus performance-log ingest,
roughly $15–20/month for this stack if left up. No alarm or dashboard read it, and
Grafana shows per-task CPU and memory for free.

**Not created in the demo** (this is where the savings are):

| Turned off | Would have cost |
|---|---|
| EC2 g5 GPU (Ollama) | ~$884/mo — replaced by the hosted ChatGPT API |
| NAT gateway | ~$43/mo — tasks use public subnets instead |
| RDS Postgres | ~$15/mo — and the app has no database client to use it with |
| API Gateway | per-request — folded into the ALB (and it would break the SSE triage stream) |
| Sub-agent tasks | ~$2/mo each — and they would be idle replicas; the app runs its agents in-process |

---

## 2. The three things that keep it cheap

1. **Destroy when done.** `terragrunt destroy` (or the pipeline's manual `destroy`
   job). Everything above is hourly — off = $0. This is the #1 lever by far.
2. **Hosted ChatGPT API, not a GPU.** `enable_ollama = false` removes the single
   biggest line item. You pay OpenAI only per token — a handful of demo triage
   calls is cents. Leave `TF_VAR_openai_api_key` unset to run the deterministic
   fallback at **$0 LLM spend** (still a working demo). (Swap the base URL to
   Anthropic's OpenAI-compat endpoint to use Claude instead — same key var.)
3. **Everything unnecessary is off.** No NAT, no RDS, no API Gateway, no
   sub-agents; Fargate Spot for ~70%-off compute. Already set in `live/demo/terragrunt.hcl`.
   "Unnecessary" here means *the app does not use it* — the Chroma task was
   removed outright for that reason, not trimmed for cost.

---

## 3. Want it even cheaper?

For a laptop/offline demo you don't need AWS at all — the app ships a
`docker-compose.yml` (`../careroute_ai_app`) that runs the whole thing locally
for **$0**. Use this Terraform stack when the point is to *show the cloud/IaC
deployment*; use docker-compose when the point is just to show the app.

If you do run on AWS, the practical floor while "up" is the ALB (~$0.02/hr). You
could drop even that by exposing the frontend task's public IP directly, but the
ALB gives a stable URL that's worth the ~$0.02/hr during a live presentation.

---

## 4. Safety / no-surprise-bill guardrails already in place

- **No always-on GPU, DB, or NAT** in the demo config.
- **Apply and destroy are manual** in CI — nothing deploys or lingers on a push.
- **`force_delete` on ECR** and **no RDS** mean `destroy` leaves nothing behind.
- **3-day log retention** keeps CloudWatch negligible.
- Set an **AWS Budget alert** (e.g. $5) in the console as a backstop — recommended
  when spending from your own pocket.
