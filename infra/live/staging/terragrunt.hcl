###############################################################################
# STAGING environment — the promotion target
# -----------------------------------------------------------------------------
# WHY THIS FOLDER EXISTS, given that the project runs one demo.
#
# CD p8 lists "staging deployment — prove deployability in a production-like
# environment" as a standard pipeline stage. Agentic p15/p16 go further: Dev ->
# Test -> Staging -> approval gate -> production canary, with staging as the
# place end-to-end agent runs, RAG validation and safety checks happen before
# anyone approves a release. Platform p15 asks for dev/test/prod segregation
# outright.
#
# This was the courseware requirement the repo met least, because `live/` held
# exactly one environment. It now holds two, and the difference is real rather
# than cosmetic: separate state key, separate name prefix, therefore separate
# VPC, cluster, ALB, secrets and ECR tags. Promotion means applying the SAME
# image tag here first and then in demo/prod — "deploy the same image across
# environments", Agentic p8.
#
# ── NOTHING HERE IS APPLIED BY DEFAULT ──────────────────────────────────────
# `apply` in .gitlab-ci.yml is pinned to TG_ENV=demo and is manual. Standing
# this up costs a second ALB (~$16/mo) on top of its tasks, which is real money
# for a self-funded student project. It exists so the promotion path is IN THE
# CODE and reviewable, and so the report can describe a pipeline that is one
# `TG_ENV=staging` away from being real rather than one that was never written.
#
# To use it:
#   cd live/staging && terragrunt apply
# or in CI, run the pipeline with TG_ENV=staging.
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
  # -> var.azs -> module "networking" (one public + one private subnet per AZ).
  azs = ["ap-southeast-1a", "ap-southeast-1b"]

  # A non-overlapping CIDR. Staging and demo are separate VPCs today, so this is
  # not strictly required — it is required the moment anyone peers them or
  # attaches both to a transit gateway, and renumbering a live VPC is not a
  # thing anyone does twice.
  # -> var.vpc_cidr / public_subnet_cidrs / private_subnet_cidrs -> module
  #    "networking" (aws_vpc cidr_block, one aws_subnet per CIDR). The ALB's
  #    subnet CIDRs also become CAREROUTE_TRUSTED_PROXIES for the backend.
  vpc_cidr             = "10.30.0.0/16"
  public_subnet_cidrs  = ["10.30.0.0/24", "10.30.1.0/24"]
  private_subnet_cidrs = ["10.30.10.0/24", "10.30.11.0/24"]

  # ── Posture: closer to production than demo is, which is the point ────────
  # Staging exists to prove deployability in a PRODUCTION-LIKE environment. A
  # staging box configured exactly like the cheap demo proves only that the
  # cheap demo works. So the two deliberate differences:
  # -> public_networking = false: networking creates a NAT gateway + private
  #    route tables, and every ECS task runs in the private subnets, no public IP.
  # -> use_fargate_spot = false: every ECS service uses on-demand FARGATE.
  public_networking = false # private subnets + NAT, i.e. the real network shape
  use_fargate_spot  = false # on-demand, so a Spot reclaim cannot be mistaken
  # for an application fault during validation

  # Same toggles as live/demo (see there for where each lands): internet-facing
  # ALB, and no API Gateway / RDS modules (count = 0).
  alb_internal       = false
  enable_api_gateway = false # still breaks the SSE triage stream; see the guard
  enable_rds         = false

  # Same single-task constraint as everywhere else: the backend holds clinical
  # state in process memory. This is a property of the application, not of the
  # environment, so staging cannot "test" its way out of it.
  # -> module "backend": enable_autoscaling and the ECS service desired_count.
  enable_autoscaling    = false
  backend_desired_count = 1
  # What IS scale-ready for the backend is still configured here, so the day
  # the app has a shared store only the three switches in scaling.tf's header
  # change: LOR routing (module "alb" default) and the request-count signal
  # below are already in place. 120 req/task/min is a placeholder until a load
  # test says otherwise — see backend_requests_per_target in variables.tf.
  # -> module "backend" requests_per_target (inert while enable_autoscaling = false).
  backend_requests_per_target = 120

  # ── The stateless half scales now ────────────────────────────────────────
  # The Next.js frontend holds no state, so staging exercises real autoscaling
  # on it: 1 task idle, up to 3 under load, on CPU or ALB requests per task,
  # whichever asks first. Idle, this costs nothing over a fixed single task.
  # -> module "frontend" enable_autoscaling / max_capacity / requests_per_target.
  frontend_enable_autoscaling  = true
  frontend_max_count           = 3
  frontend_requests_per_target = 600

  # -> module "backend" / module "frontend" task-definition cpu (1024 = 1 vCPU)
  #    and memory (MiB).
  backend_cpu    = 512
  backend_memory = 1024
  frontend_cpu   = 256
  frontend_memory = 512

  # ── The MLOps plumbing that demo leaves off ──────────────────────────────
  # Staging is where a scheduled monitor and a durable telemetry archive are
  # worth paying for, because runs here are meant to accumulate evidence across
  # days rather than exist for one afternoon.
  # -> enable_artifacts_bucket: count on module "artifacts" (S3). The next three
  #    only take effect when it is true:
  #    enable_ml_telemetry_shipping -> telemetry sidecar in the backend task;
  #    enable_scheduled_monitor     -> module "drift_monitor" (EventBridge Scheduler
  #                                    schedule that launches an ECS task);
  #    enable_alb_access_logs       -> module "alb" access_logs_bucket.
  enable_artifacts_bucket      = true
  enable_ml_telemetry_shipping = true
  enable_scheduled_monitor     = true
  enable_alb_access_logs       = true

  # -> count on module "monitoring" + ADOT sidecar, as in live/demo.
  enable_prometheus_stack = true
  enable_app_metrics      = true # both paths on: EMF survives teardown, Prometheus
  # has the real p95 rules. See docs/vault/App Overview.md

  # MLflow needs RDS to keep its registry across redeploys; with enable_rds
  # false above it would run on ephemeral SQLite and silently forget every run.
  # Left off rather than deployed in the shape that looks like it works.
  # -> count on module "mlflow" (also requires enable_artifacts_bucket).
  enable_mlflow_server = false

  # ── Release identity ─────────────────────────────────────────────────────
  # No `latest` fallback here. Staging's whole job is to validate a SPECIFIC
  # build before it is promoted, so an unset tag should fail loudly rather than
  # quietly validate whatever happens to be sitting on a floating tag.
  # [TG] get_env("IMAGE_TAG") with NO default: Terragrunt stops with an error if
  #      the variable is unset. -> var.image_tag -> image of every task definition.
  #      ecr_image_tag_mutability is not set here, so the stack default
  #      "IMMUTABLE" (ecr_image_tag_mutability default in variables.tf) applies.
  image_tag = get_env("IMAGE_TAG")

  # -> local.llm_env -> backend LLM_PROVIDER_ORDER / OPENAI_MODEL env vars.
  #    The key itself comes from TF_VAR_openai_api_key, as in live/demo.
  llm_provider_order = "openai"
  openai_model       = "gpt-4o-mini"

  # -> CAREROUTE_RATE_LIMIT_PER_MIN in the backend; CloudWatch retention (days).
  rate_limit_per_min = 30
  log_retention_days = 14
}
