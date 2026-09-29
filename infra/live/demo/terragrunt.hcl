###############################################################################
# DEMO environment — single, absolute-minimum-cost stack
# -----------------------------------------------------------------------------
# Tuned for a master's-degree presentation paid out of pocket. Every always-on
# cost is stripped out; what remains is one public ALB + two tiny Fargate Spot
# tasks, plus a hosted Claude API call. Run it only for the demo, then destroy:
#
#   cd live/demo
#   terragrunt apply           # ~a few minutes to stand up
#   terragrunt output app_url  # open this in the browser for the demo
#   terragrunt destroy         # <<< do this the moment the demo is over
#
# Ballpark cost: cents-to-~$1 for a couple of hours; ~$25/mo only if left on.
###############################################################################

# [TG] `include` pulls root.hcl in and merges its remote_state, generate and
#      inputs blocks into this file. find_in_parent_folders("root.hcl") walks
#      UP from this folder until it finds that file (here: the repo root).
#      "root" is just a label for the include.
include "root" {
  path = find_in_parent_folders("root.hcl")
}

# [TG] Which Terraform code to run. Terragrunt copies the source into a cache
#      dir (.terragrunt-cache/) and runs terraform there, with backend.tf and
#      provider.tf generated next to it. The `//` splits "what to copy"
#      (all of modules/) from "which subfolder to run" (careroute_stack), so the
#      stack's `source = "../networking"` etc. still resolve inside the cache.
terraform {
  source = "${get_terragrunt_dir()}/../../modules//careroute_stack"
}

# [TG] Each key below is exported as TF_VAR_<key> and becomes var.<key> in
#      modules/careroute_stack (declared in variables.tf). Keys not listed take
#      that variable's `default`. Merged with root.hcl's inputs (environment,
#      region). Secrets are NOT set here: export TF_VAR_openai_api_key etc. in
#      the shell / CI and Terraform reads them directly.
inputs = {
  # Two AZs because an ALB requires subnets in two AZs (subnets are free).
  # -> var.azs -> module "networking" (one public + one private subnet per AZ).
  azs = ["ap-southeast-1a", "ap-southeast-1b"]

  # ── Cost-minimal posture: strip every always-on charge ──────────────────
  # Where each toggle lands (careroute_stack/main.tf):
  #   public_networking    -> networking enable_nat_gateway = !this (no NAT), and
  #                           local.task_subnet_ids / assign_public_ip for every ECS service
  #   alb_internal         -> module "alb" internal + subnet_ids; security alb_internet_facing
  #   enable_api_gateway   -> count on module "api_gateway" (0 = not created) + a guard check
  #   enable_rds           -> count on module "rds"
  #   enable_subagent_tier -> for_each on module "sub_agents"
  #   use_fargate_spot     -> every ECS service module (FARGATE_SPOT capacity provider)
  #   enable_autoscaling   -> module "backend" enable_autoscaling
  public_networking    = true  # tasks in PUBLIC subnets w/ public IP -> NO NAT gateway (~$43/mo saved)
  alb_internal         = false # internet-facing ALB IS the entry point -> NO API Gateway layer
  enable_api_gateway   = false # also: API GW would cut the SSE triage stream
  enable_rds           = false # the app has no DB client at all -> NO database (~$15/mo saved)
  enable_subagent_tier = false # the agents run in-process in the backend task
  use_fargate_spot     = true  # ~70% off container compute
  enable_autoscaling   = false # the backend holds clinical state in memory — one task only

  # ── Task sizes ───────────────────────────────────────────────────────────
  # The backend is NOT a thin API: scikit-learn + numpy + SHAP, with a severity
  # model baked into the image and warmed at boot (the app's own compose file
  # gives it 1.5 vCPU / 1536 MB). It was previously set to 256/512 here, which
  # OOM-kills the warmup and shows up as an ALB health check that never turns
  # green. 512 CPU / 1024 MB is the smallest size that actually serves.
  # The frontend really is thin — a Next.js standalone server — so it stays at
  # the floor, and the difference is a few cents an hour on Spot.
  # -> var.backend_cpu / backend_memory / backend_desired_count -> module "backend"
  #    (ecs_service): task-definition cpu (1024 = 1 vCPU) and memory (MiB), and the
  #    ECS service desired_count. frontend_* -> module "frontend" the same way.
  # Raised from 512 / 1024 on 2026-09-29 for Safety-NLP (four transformer models
  # in the backend process, see enable_safety_nlp below). Measured locally on the
  # release image with all four loaded: 1.85 GiB idle, 2.14 GiB peak over 20
  # triages, so 4 GB is ~1.9x headroom. 2 vCPU is the smallest Fargate CPU that
  # allows 4 GB+ and keeps CPU inference inside the 1 s shadow budget. Re-measure
  # peak memory and p95 latency under a concurrent load test on AWS.
  backend_cpu    = 2048
  backend_memory = 4096
  # Safety-NLP, first rollout: models loaded and reporting in SHADOW (they never
  # move acuity). Verify /api/health safetyNlp="model" and the shadow telemetry,
  # then apply a second revision with safety_prototype_semantic_activation = true.
  enable_safety_nlp     = true
  safety_nlp_direct_nli = true
  # ON since 2026-09-29: the safety LLM may RAISE acuity (never lower it) above
  # its 0.85 threshold. It caught a paraphrased stroke the rules missed, which in
  # shadow mode was still routed as P3, not escalated. Rollback = false + apply.
  safety_prototype_semantic_activation = true
  enable_safety_llm                    = true
  # gpt-5.4-mini: measured 4/4 correct at 0.97-0.99 confidence in 1.7-3.4 s;
  # gpt-4o-mini (the old default) scored 0.90, and gpt-5.4 took up to 4.2 s of
  # the 5 s budget. Timings in docs/vault/Changelog.md (2026-09-29).
  safety_llm_model       = "gpt-5.4-mini"
  frontend_cpu           = 256
  frontend_memory        = 512
  backend_desired_count  = 1
  frontend_desired_count = 1
  # Scaling is off here on purpose: a demo has one audience, and a fixed task
  # count is a fixed bill. Everything that scales is one input away — see
  # modules/careroute_stack/scaling.tf and live/staging for the scaled shape:
  #   frontend_enable_autoscaling = true, frontend_max_count = 3

  # ── Off for a throwaway demo; turn on for anything longer ────────────────
  # Both are cheap (a few custom metrics; a near-empty S3 bucket) but neither
  # earns its keep in a 2-hour presentation. enable_app_metrics is what gives
  # AWS any visibility of escalations / guardrail blocks / per-agent latency;
  # enable_artifacts_bucket is what finally gives DVC a remote to push to.
  # -> enable_app_metrics: ADOT sidecar in the backend task + its IAM policy (count).
  # -> enable_artifacts_bucket: count on module "artifacts" (S3) + task-role policy.
  enable_app_metrics = false
  # ON since 2026-09-22: the app repo's DVC remote (s3://<bucket>/dvc, pushed
  # by its data:version job over the app CI role) and the backend's inference
  # log / drift reports (enable_ml_telemetry_shipping below). ~50 MB of S3 -
  # cents. Still `destroy`-able: the bucket is force_destroy = true.
  enable_artifacts_bucket = true
  # Sidecar in the backend task that syncs the inference log, ground-truth
  # labels and drift reports to s3://<bucket>/ml-telemetry/ on a timer, so the
  # drift evidence survives a redeploy or a Spot reclaim. No extra task.
  enable_ml_telemetry_shipping = true
  # ON since 2026-09-24: the drift monitor (python -m app.ml.monitor) as a
  # scheduled one-shot Fargate task. Restores the inference log from the bucket
  # above, writes its drift report back. Cents a month; nothing always-on.
  # -> count on module "drift_monitor" (modules/scheduled_task).
  enable_scheduled_monitor = true

  # CloudWatch alarm emails (SNS topic in modules/observability). AWS emails a
  # confirmation link after apply; nothing is delivered until it is clicked.
  # -> var.alarm_email -> aws_sns_topic_subscription.email[0].
  alarm_email = "alerts@example.com"

  # ── Observability the way the report describes it ────────────────────────
  # Prometheus + Alertmanager + Grafana on Fargate, running the SAME configs,
  # alert rules and dashboard the app uses locally — so the deployed
  # architecture matches the tooling that gets demonstrated, rather than the
  # demo showing Grafana while the cloud diagram shows CloudWatch.
  #
  # Costs three extra Spot tasks (~$0.02/hr together — cents for a demo). ON by
  # default here because a presentation that cannot show its own monitoring is
  # a worse outcome than two cents; set false to strip it back.
  #
  # After apply:  terragrunt output -raw grafana_url
  #               terragrunt output -raw grafana_admin_password   (user: admin)
  # -> count on module "monitoring" (modules/monitoring_stack) and module "alb"
  #    enable_grafana_route (the /grafana target group + listener rule).
  enable_prometheus_stack = true

  # ── LLM: ChatGPT via the app's `openai` provider ────────────────────────
  # The app's `openai` provider speaks the OpenAI Chat Completions REST shape.
  # openai_base_url defaults to OpenAI, so we only pick the model here.
  # -> var.llm_provider_order / var.openai_model -> local.llm_env -> the backend
  #    container's LLM_PROVIDER_ORDER / OPENAI_MODEL environment variables.
  llm_provider_order = "openai"
  openai_model       = "gpt-4o-mini"
  # Model by task weight (backend/app/llm.py ROUTES): light tasks stay on the cheap
  # model, heavy ones get a stronger one, and only calls escalated as hard get the top one.
  # Matches the app's code defaults (backend/app/llm.py _TIER_DEFAULTS), chosen by the
  # 100-scenario head-to-head of 2026-09-27; gpt-4o-mini / gpt-4.1-mini / gpt-4.1 is the
  # cheaper, faster, more conservative alternative.
  openai_model_fast = "gpt-5.4-mini"
  openai_model_deep = "gpt-5.4"
  openai_model_max  = "gpt-5.5"
  # The key is NOT in code. Set it as a masked CI variable TF_VAR_openai_api_key
  # (an OpenAI key, sk-...), or locally:
  #   export TF_VAR_openai_api_key=sk-...   before apply.
  # Leave it unset to run in deterministic-fallback mode with $0 LLM spend.
  # For Claude instead: set openai_base_url = "https://api.anthropic.com/v1",
  # openai_model = "claude-opus-4-8", and use an Anthropic key.
  # TF_VAR_openai_api_key -> var.openai_api_key -> aws_secretsmanager_secret "openai"
  # (created only when non-empty) -> injected into the backend task as a secret.

  # ── OneMap (Care Routing travel times + route geometry) ─────────────────
  # Free account at onemap.gov.sg. WITHOUT these the routing agent still
  # answers, but every clinic distance is an explicitly labelled straight-line
  # estimate — a visibly weaker demo of the one agent that does real-world
  # lookups. Set them as masked CI variables, never here:
  #   TF_VAR_onemap_email / TF_VAR_onemap_password
  # -> var.onemap_email / onemap_password -> aws_secretsmanager_secret "onemap"
  #    -> backend task secrets. Both must be set or neither is used.

  # ── RAG ─────────────────────────────────────────────────────────────────
  # Left blank: the backend falls back to its in-process TF-IDF retriever over
  # the built-in clinical corpus, which is the right call for a demo. Set
  # onyx_base_url + TF_VAR_onyx_api_key to point at a self-hosted Onyx.

  # ── HTTP edge ───────────────────────────────────────────────────────────
  # trust_alb_as_proxy defaults to true, which is what stops every patient in
  # the demo sharing one 30/min rate-limit bucket keyed on the ALB's address.
  # cors_origins is computed from the ALB DNS name.
  # -> var.rate_limit_per_min -> CAREROUTE_RATE_LIMIT_PER_MIN (local.safety_env)
  #    in the backend container.
  rate_limit_per_min = 30

  # ── Image identity ───────────────────────────────────────────────────────
  # Pass IMAGE_TAG (a commit SHA) and both lines below become correct:
  #
  #   IMAGE_TAG=$(git -C ../../../careroute_ai_app rev-parse --short HEAD) terragrunt apply
  #
  # The `latest` fallback is what README.md's manual `docker push` produces, and
  # it is a real limitation, not just untidiness: a fixed tag renders a
  # byte-identical task definition, so ECS registers no new revision and an
  # apply after a push deploys NOTHING. It also leaves no earlier tag to roll
  # back to, which is the one thing the new deployment circuit breaker needs in
  # order to have somewhere to roll back to.
  #
  # MUTABLE is therefore tied to the fallback: overwriting `:latest` on the
  # second push is exactly what IMMUTABLE forbids. Both revert to the module
  # defaults the day the app pipeline pushes `$CI_COMMIT_SHA` to ECR.
  # See docs/vault/Lecture Alignment.md §2.
  # [TG] get_env("IMAGE_TAG", "latest") reads a shell environment variable when
  #      Terragrunt runs, with a fallback. -> var.image_tag -> "<ECR repo URL>:<tag>"
  #      in the backend, frontend and drift-monitor task definitions.
  # [TF] The next line is a ternary: cond ? a : b.
  #      -> var.ecr_image_tag_mutability -> module "ecr" (the repositories' tag setting).
  image_tag                = get_env("IMAGE_TAG", "latest")
  ecr_image_tag_mutability = get_env("IMAGE_TAG", "latest") == "latest" ? "MUTABLE" : "IMMUTABLE"

  # -> var.log_retention_days -> ECS log group (ecs_cluster), API Gateway access
  #    logs and VPC flow logs (all CloudWatch retention_in_days).
  log_retention_days = 3 # short retention = negligible CloudWatch cost
}
