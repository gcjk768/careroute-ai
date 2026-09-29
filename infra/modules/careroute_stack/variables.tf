###############################################################################
# careroute_stack — inputs
# -----------------------------------------------------------------------------
# Everything a single environment needs to stand up the whole CareRoute stack.
# Terragrunt sets the per-environment values (see live/<env>/terragrunt.hcl);
# the defaults here are the "staging / small" baseline.
###############################################################################

# [TF] LEARNER NOTES — how to read this file
# [TF] Each `variable "x" {}` block declares one INPUT of this module; code reads it as `var.x`.
# [TF] No `default` = REQUIRED: plan fails unless the caller supplies it. With a default the caller may omit it.
# [TF] `type` (string / number / bool / list(string) / map(string)) is checked at plan time.
# [TF] Terragrunt turns every key of `inputs = {}` into a TF_VAR_<name> env var, which is how
# [TF] live/<env>/terragrunt.hcl and root.hcl values land here. You can also export TF_VAR_<name>
# [TF] yourself — that is how the secrets (API keys, passwords) arrive without ever being in git.
# Shorthand below:
#   "live/demo" / "live/staging" = the `inputs = {}` map in live/<env>/terragrunt.hcl
#   "main.tf"                    = modules/careroute_stack/main.tf (this module's wiring)
#   "default only"               = no caller overrides it; the default below is what gets deployed

# ---- Identity ------------------------------------------------------------- #
# The environment's short name (demo / staging) — the second half of every resource name.
# Set by: root.hcl inputs (environment = local.environment, read from live/<env>/env.hcl).
# Used in: main.tf local.name_prefix = "<project>-<environment>" (passed as name_prefix to every
#          child module, e.g. "careroute-demo-alb") and local.common_tags Environment tag.
variable "environment" {
  description = "Environment name (staging | production). Used in every resource name."
  type        = string
}

# AWS region string (e.g. ap-southeast-1). NOTE: this does NOT choose where resources are
# created — the generated provider "aws" block does (root.hcl generate "provider"). This copy is
# for places that must spell the region out, so the two must agree.
# Set by: root.hcl inputs (region = local.aws_region, from live/<env>/env.hcl).
# Used in: main.tf → `region` input of modules backend / frontend / sub_agents / monitoring / mlflow /
#          drift_monitor / observability; the "awslogs-region" log option of the ADOT and telemetry
#          sidecars; and the log-group ARN in data.aws_iam_policy_document.otel_emf.
variable "region" {
  description = "AWS region (must match the Terragrunt-generated provider)."
  type        = string
}

# Project name, the first half of every resource name.
# Set by: default only.
# Used in: main.tf local.name_prefix ("careroute-<env>") and the Cloud Map private DNS namespace
#          name "<project>.local" (aws_service_discovery_private_dns_namespace.this, and the
#          namespace_name input of modules monitoring and mlflow).
variable "project" {
  type    = string
  default = "careroute"
}

# ---- Networking ----------------------------------------------------------- #
# The VPC's IPv4 CIDR (/16 = 65,536 addresses). Subnet CIDRs below must fall inside it.
# Set by: live/staging ("10.30.0.0/16", so demo and staging never overlap); demo uses the default.
# Used in: main.tf module "networking" vpc_cidr → aws_vpc.this.cidr_block (modules/networking/main.tf).
variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

# Availability Zones to spread subnets over. REQUIRED (no default). An ALB needs subnets in >= 2 AZs.
# Set by: live/demo, live/staging (["ap-southeast-1a","ap-southeast-1b"]).
# Used in: main.tf module "networking" azs → aws_subnet.public / aws_subnet.private availability_zone
#          via element(var.azs, count.index), i.e. subnet #i lands in AZ #i.
variable "azs" {
  description = "AZs to spread subnets across (2 is enough for a small region)."
  type        = list(string)
}

# Public subnets (route table has 0.0.0.0/0 → Internet Gateway), one /24 (256 IPs) per AZ.
# Set by: live/staging (10.30.x); demo uses the default.
# Used in: main.tf module "networking" → one aws_subnet.public per list entry (count = length(list));
#          also main.tf local.alb_subnet_cidrs (the trusted-proxy list) when the ALB is internet-facing.
variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.20.0.0/24", "10.20.1.0/24"]
}

# Private subnets (no IGW route; outbound only via NAT, when NAT exists), one /24 per AZ.
# Set by: live/staging (10.30.1x); demo uses the default.
# Used in: main.tf module "networking" → one aws_subnet.private per entry; also
#          local.alb_subnet_cidrs when alb_internal = true (an internal ALB sits in private subnets).
variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.20.10.0/24", "10.20.11.0/24"]
}

# true = one NAT Gateway shared by all AZs (cheaper; an AZ outage cuts egress for the others);
# false = one NAT per AZ (HA). Irrelevant when public_networking = true, because then there is no NAT.
# Set by: default only.
# Used in: main.tf module "networking" single_nat_gateway → local.nat_count and the private route
#          tables in modules/networking/main.tf.
variable "single_nat_gateway" {
  type    = bool
  default = true
}

# ---- Cost-minimal demo toggles -------------------------------------------- #
# The cheapest deployable shape: run the two Fargate tasks in PUBLIC subnets
# (public IP for egress), so there is NO NAT gateway, and make the ALB itself
# the public entry point, so there is NO API Gateway. Combined with enable_rds
# = false (the app is in-memory) this strips every always-on cost down to just
# the ALB + two Spot tasks.
# Set by: live/demo (true), live/staging (false).
# Used in: main.tf module "networking" enable_nat_gateway = !var.public_networking (no NAT when true);
#          local.task_subnet_ids (public vs private subnets for EVERY Fargate task) and
#          local.assign_public_ip → assign_public_ip in each aws_ecs_service network_configuration.
variable "public_networking" {
  description = "Run tasks in public subnets with public IPs and no NAT gateway (cheapest)."
  type        = bool
  default     = false
}

# Scheme of the Application Load Balancer: internal (private IPs only) vs internet-facing.
# Set by: live/demo, live/staging (all false).
# Used in: main.tf module "alb" internal → aws_lb.this.internal (modules/alb/main.tf), and picks the
#          ALB's subnet_ids (private vs public); module "security" alb_internet_facing = !var.alb_internal
#          (ALB security-group ingress); local.alb_subnet_cidrs (trusted proxies).
variable "alb_internal" {
  description = "true = internal ALB behind API Gateway; false = internet-facing ALB as the public entry. Defaults to false to match enable_api_gateway = false — an internal ALB with nothing in front of it is unreachable."
  type        = bool
  default     = false
}

# Creates an API Gateway HTTP API + VPC Link in front of the ALB when true.
# Set by: live/demo, live/staging (all false).
# Used in: main.tf module "api_gateway" (count = var.enable_api_gateway ? 1 : 0); local.app_base_url and
#          local.computed_cors_origins; terraform_data.guards (precondition); module "observability"
#          api_id; outputs.tf app_url and api_endpoint.
variable "enable_api_gateway" {
  description = <<-EOT
    Front the ALB with API Gateway (routing/auth/rate-limit).

    INCOMPATIBLE WITH THE TRIAGE UI as the app stands. The patient console's
    only path to a result is POST /api/triage/stream, a Server-Sent Events
    response that stays open for the whole run (measured 24-30 s of silence in
    the Care Routing and red-flag paths, and longer end to end). An HTTP API
    integration is capped at 30 s and buffers the response rather than passing
    it through, so the stream is cut and the UI hangs on "Triaging...".

    Turn it on only for a non-streaming surface, or after the app grows a
    poll-based triage endpoint. The stack refuses to plan the combination
    unless `accept_api_gateway_sse_break` is also set.
  EOT
  type        = bool
  default     = false
}

# A plan-time "are you sure" flag; creates nothing by itself.
# Set by: default only.
# Used in: main.tf terraform_data.guards lifecycle precondition (plan fails if API GW is on and this is false).
variable "accept_api_gateway_sse_break" {
  description = "Acknowledge that enabling API Gateway breaks the SSE triage stream (see enable_api_gateway) and plan anyway."
  type        = bool
  default     = false
}

# Creates an RDS for PostgreSQL instance (+ subnet group + Secrets Manager credential) when true.
# Set by: live/demo, live/staging (all false).
# Used in: main.tf module "rds" count; backend env DB_HOST/PORT/NAME/USER and secret DB_PASSWORD;
#          module "mlflow" backend_store_uri and aws_secretsmanager_secret.mlflow_store count;
#          outputs.tf rds_endpoint and db_secret_arn.
variable "enable_rds" {
  description = <<-EOT
    Provision RDS Postgres.

    NOTE (verified against the app on 2026-09-11, branch
    integration-all-agents-2026-08-31): the backend has NO database client at
    all — no psycopg, no SQLAlchemy, and it reads no DB_* environment variable.
    backend/app/store.py is an in-process dict. So this provisions a database
    nothing connects to; it is kept as the migration target for when store.py
    grows a real backing store, and the DB_* vars are injected so the app can
    pick them up the day it does. Leave it OFF until then.
  EOT
  type        = bool
  default     = false
}

# Set by: default only.
# Used in: main.tf terraform_data.guards — the first lifecycle precondition (plan-time check, no AWS resource).
variable "allow_multi_task_backend" {
  description = <<-EOT
    Escape hatch for the single-task guard.

    backend/app/store.py keeps cases, escalations, session history and the
    fairness/audit trail in PROCESS memory. Two backend tasks therefore serve
    two disjoint worlds: a clinician polling /api/escalations reaches task A
    while the patient whose case escalated was served by task B, so the case
    never appears; a patient answering a clarifying question gets a 404 on
    resume if the ALB lands them on the other task. That is silent clinical
    data loss, not a performance quirk, so the stack REFUSES to plan more than
    one backend task (see terraform_data.stateful_backend_guard).

    Set true only after store.py is backed by a shared store, AND that store
    is on in this stack: enable_shared_state_store (DynamoDB, the intended
    one) or enable_rds. The guard refuses the flag without one of them.
  EOT
  type        = bool
  default     = false
}

# ---- Images --------------------------------------------------------------- #
# The container image TAG (the part after ':') for every app container.
# Set by: live/demo get_env("IMAGE_TAG", "latest"); live/staging get_env("IMAGE_TAG") (no fallback,
#         so Terragrunt errors if IMAGE_TAG is unset).
# Used in: main.tf image = "<ECR repo URL>:<image_tag>" for modules backend, sub_agents and
#          drift_monitor (backend repo) and frontend (frontend repo) → the container image in
#          each aws_ecs_task_definition.
variable "image_tag" {
  description = <<-EOT
    Tag to deploy for both backend and frontend. Pass a COMMIT SHA, not a
    floating tag.

    `latest` is the default only because that is what README.md's manual
    `docker build && docker push` produces today, and it has a cost worth
    stating: because the tag never changes, the rendered task definition is
    byte-identical between builds, so ECS registers no new revision and `apply`
    after a push starts NO DEPLOYMENT. The tag also no longer identifies which
    build is serving, and there is no earlier tag to roll back to.

    Set it from CI instead — `TF_VAR_image_tag=$CI_COMMIT_SHA` — at which point
    ecr_image_tag_mutability should go back to its IMMUTABLE default.
  EOT
  type        = string
  default     = "latest"
}

# ---- ML telemetry durability ----------------------------------------------- #
# Set by: live/staging (true); default elsewhere.
# Used in: main.tf local.telemetry_enabled (= this AND enable_artifacts_bucket) → adds the shipper
#          sidecar (local.telemetry_sidecar), a shared task volume and telemetry env vars to the
#          backend task; outputs.tf ml_telemetry_prefix.
variable "enable_ml_telemetry_shipping" {
  description = <<-EOT
    Run a sidecar that syncs the backend's inference log, drift reports and
    fairness output to the artifacts bucket on a timer — the local equivalent of
    SageMaker's DataCaptureConfig (Serving p26-28, MonitoringWithSageMaker
    p12-14). Requires enable_artifacts_bucket.

    Without it those files live on Fargate task storage, which every deploy,
    Spot reclaim and crash destroys. The drift evidence that the MLOps story is
    told with does not survive the demo it is told in.

    A sidecar rather than an S3 write in the app because the backend has no
    boto3 client at all; see the block comment in main.tf.
  EOT
  type        = bool
  default     = false
}

# Set by: default only.
# Used in: main.tf local.telemetry_sidecar — the `sleep <n>` in the sidecar's sync loop.
variable "ml_telemetry_sync_seconds" {
  description = "Seconds between telemetry syncs. The sync is additive, so this is the worst-case data loss when a task dies — not a delay before anything is written."
  type        = number
  default     = 300
}

# Set by: default only.
# Used in: main.tf local.telemetry_sidecar image, and the S3 restore/archive containers of module "drift_monitor".
variable "aws_cli_image" {
  description = "Image for the telemetry shipper sidecar and the scheduled monitor's S3 steps. Pinned rather than :latest for the same reason the observability images are."
  type        = string
  default     = "public.ecr.aws/aws-cli/aws-cli:2.17.62"
}

# Set by: default only (true) — no live env overrides it.
# Used in: main.tf module "observability" enable_saved_queries → CloudWatch Logs Insights saved
#          query definitions in modules/observability.
variable "enable_saved_log_queries" {
  description = "Create saved CloudWatch Logs Insights queries that pivot on the app's correlation_id / case_id. Free, and the thing that makes the single ECS log group answerable rather than merely aggregated."
  type        = bool
  default     = true
}

# ---- MLflow tracking server ------------------------------------------------ #
# Set by: live/staging (false); default elsewhere.
# Used in: main.tf module "mlflow" count (= this AND enable_artifacts_bucket) and
#          aws_secretsmanager_secret.mlflow_store count; outputs.tf mlflow_tracking_uri.
variable "enable_mlflow_server" {
  description = <<-EOT
    Run an MLflow tracking server on Fargate with S3 as the artifact root —
    "store ML runs by syncing the mlruns folder; centralized model registry",
    which is the first instruction on the Platform deck's AWS slide and is
    repeated for Azure and GCP.

    Requires enable_artifacts_bucket. Two caveats, both in modules/mlflow's
    header: the server has NO AUTHENTICATION (so it is internal-only by
    default), and internal-only means GitLab CI — where training actually runs —
    cannot reach it without a VPC-resident runner.

    Without RDS the run metadata lives in SQLite on ephemeral task storage and
    is lost on every redeploy while the S3 artifacts survive. That combination
    looks like it works, so turn RDS on with it or accept a demo-only registry.
  EOT
  type        = bool
  default     = false
}

# Set by: default only.
# Used in: main.tf module "mlflow" image → the container image of its ECS task definition (modules/mlflow).
variable "mlflow_image" {
  description = "MLflow server image. MUST match the client version the app pins (backend/requirements.txt: mlflow==3.13.0) — a 3.x client against a 2.x server fails on the registry API, which is the half this exists to enable."
  type        = string
  default     = "ghcr.io/mlflow/mlflow:v3.13.0"
}

# Fargate task size for MLflow: CPU in units (1024 = 1 vCPU), memory in MiB. Fargate only accepts
# certain CPU/memory pairs (e.g. 512 CPU → 1024-4096 MiB).
# Set by: default only.
# Used in: main.tf module "mlflow" cpu / memory → aws_ecs_task_definition cpu/memory in modules/mlflow.
variable "mlflow_cpu" {
  type    = number
  default = 512
}

# Set by: default only.  Used in: module "mlflow" memory (see mlflow_cpu above).
variable "mlflow_memory" {
  type    = number
  default = 1024
}

# ---- Scheduled drift monitor ----------------------------------------------- #
# Set by: live/staging (true); default elsewhere.
# Used in: main.tf module "drift_monitor" (source ../scheduled_task) count = this AND
#          enable_artifacts_bucket; outputs.tf drift_monitor_schedule.
variable "enable_scheduled_monitor" {
  description = <<-EOT
    Run `python -m app.ml.monitor` on a schedule as a one-shot Fargate task —
    the cloud rendering of the hourly Model Monitor schedule the SageMaker decks
    build (Serving p31, MonitoringWithSageMaker p17, Platform p15/p22).

    Requires enable_artifacts_bucket: the job restores the inference log from S3
    before it runs and archives its drift report back afterwards. Without that,
    every run starts on an empty filesystem and reports NO DRIFT — which reads
    exactly like a healthy model and is the one wrong answer nobody chases.

    Nothing is always-on: the schedule is free at this volume and the task bills
    per second while it runs, so a daily run is cents a month on Spot.
  EOT
  type        = bool
  default     = false
}

# EventBridge Scheduler cron has SIX fields: minute hour day-of-month month day-of-week year.
# Set by: default only.
# Used in: main.tf module "drift_monitor" schedule_expression → aws_scheduler_schedule.this
#          .schedule_expression (modules/scheduled_task/main.tf).
variable "monitor_schedule_expression" {
  description = "EventBridge Scheduler expression. NOTE the six-field cron form: the decks' hourly example is `cron(0 * * * ? *)`. Daily by default, because a demo does not generate enough traffic in an hour for PSI to say anything meaningful."
  type        = string
  default     = "cron(0 3 * * ? *)"
}

# Set by: default only.
# Used in: main.tf module "drift_monitor" schedule_timezone → aws_scheduler_schedule.this
#          .schedule_expression_timezone.
variable "monitor_schedule_timezone" {
  description = "IANA timezone for the schedule. Explicit because 'off-peak' is a local-time idea and the UTC default puts 03:00 in the middle of the Singapore working day."
  type        = string
  default     = "Asia/Singapore"
}

# Fargate size of the one-shot drift-monitor task (CPU units / MiB).
# Set by: default only.
# Used in: main.tf module "drift_monitor" cpu / memory → aws_ecs_task_definition.this in modules/scheduled_task.
variable "monitor_task_cpu" {
  type    = number
  default = 512
}

# Set by: default only.
# Used in: main.tf module "drift_monitor" memory (see monitor_task_cpu above).
variable "monitor_task_memory" {
  description = "The monitor loads the same scikit-learn/numpy stack as the backend, so it needs the backend's memory floor, not a job-sized one."
  type        = number
  default     = 1024
}

# ---- TLS / edge ------------------------------------------------------------ #
# ARN of an ACM (Certificate Manager) cert, e.g. arn:aws:acm:<region>:<acct>:certificate/<id>.
# Set by: default only ("" = no TLS).
# Used in: main.tf module "alb" certificate_arn → creates aws_lb_listener.https (:443) and turns :80
#          into a redirect (modules/alb/main.tf); module "security" alb_tls_enabled (opens 443 on the
#          ALB security group); surfaces as outputs.tf tls_enabled.
variable "acm_certificate_arn" {
  description = "ACM certificate for the ALB, in THIS region. Set it and the ALB serves HTTPS on :443 with :80 redirecting; leave it empty and the stack stays HTTP-only. Empty is the demo default only because a certificate needs a domain, which the demo has none of — see modules/alb for how to get one, and docs/vault/Lecture Alignment.md §9 for why it matters."
  type        = string
  default     = ""
}

# Which TLS versions/ciphers the HTTPS listener accepts (TLS13-1-2 = TLS 1.3 and 1.2 only).
# Set by: default only.
# Used in: main.tf module "alb" ssl_policy → aws_lb_listener.https.ssl_policy.
variable "alb_ssl_policy" {
  description = "ELB security policy for the HTTPS listener. Ignored without a certificate."
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-2021-06"
}

# Set by: live/staging (true); default elsewhere.
# Used in: main.tf module "alb" access_logs_bucket (only when enable_artifacts_bucket is also true)
#          → the access_logs block of aws_lb.this; module "artifacts" alb_access_logs_prefix
#          → the bucket-policy statement that lets the ELB service write logs.
variable "enable_alb_access_logs" {
  description = "Write ALB access logs to the artifacts bucket (requires enable_artifacts_bucket). Logging is free; only the S3 storage bills. This is the cheap part of LLMSecOps Pillar 3 — who called what, when, from where."
  type        = bool
  default     = false
}

# ---- Network privacy / audit ----------------------------------------------- #
# Set by: default only.
# Used in: main.tf module "networking" → aws_vpc_endpoint.s3 (a GATEWAY endpoint: a route-table
#          entry to S3, no ENI, no hourly charge).
variable "enable_s3_gateway_endpoint" {
  description = "Gateway VPC endpoint for S3. Free, so on by default; it keeps ECR layer pulls off NAT when NAT exists."
  type        = bool
  default     = true
}

# Set by: default only.
# Used in: main.tf module "networking" → aws_vpc_endpoint.interface (for_each over the service list;
#          INTERFACE endpoints = ENIs in your subnets, billed per hour per AZ) — only when NAT is on.
variable "enable_interface_endpoints" {
  description = "PrivateLink endpoints for ECR/logs/Secrets Manager so a private-subnet task needs no internet path. ~$58/month for four across two AZs, so OFF by default and ignored unless NAT is on."
  type        = bool
  default     = false
}

# Set by: default only.
# Used in: main.tf module "networking" → aws_flow_log.this + its CloudWatch log group and IAM role.
variable "enable_flow_logs" {
  description = "VPC flow logs to CloudWatch. Off by default — it bills per ingested GB, and a demo's evidence is in the application logs."
  type        = bool
  default     = false
}

# Set by: default only (0).
# Used in: main.tf recovery_window_in_days on every aws_secretsmanager_secret it creates (openai,
#          onemap, onyx, grafana, mlflow_store) and module "rds" secret_recovery_window_days.
variable "secret_recovery_window_days" {
  description = <<-EOT
    Days AWS keeps a deleted Secrets Manager secret recoverable. **0 means
    delete immediately, and 0 is what this create-and-destroy stack needs.**

    AWS defaults to 30, and a scheduled-for-deletion secret KEEPS ITS NAME
    reserved for the whole window. Every secret here has a fixed name
    (`careroute-demo/openai-api-key`, `…/grafana-admin-password`, …), so with the
    default the sequence this repo is built around — apply, demo, destroy, apply
    again tomorrow — fails on the second apply with:

      InvalidRequestException: You can't create this secret because a secret
      with this name is already scheduled for deletion.

    It fails at APPLY, in front of whoever you are demoing to, and it reads like
    an AWS problem rather than a 30-day timer you started yesterday. The nightly
    `scheduled_destroy` safety net makes it a near-certainty rather than a risk.

    Raise it to 7-30 for any environment holding a secret you would be sorry to
    lose — that is what the window is for. It is wrong only here, where the
    secrets are re-created from CI variables on every apply.
  EOT
  type        = number
  default     = 0

  # [TF] `validation {}` runs at plan time: if `condition` is false, plan stops with `error_message`.
  # [TF] Since Terraform 1.9 a condition may also read other variables; this one checks only itself.
  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "secret_recovery_window_days must be 0 (immediate) or between 7 and 30 — AWS rejects everything in between."
  }
}

# Set by: live/demo (MUTABLE when IMAGE_TAG is unset, IMMUTABLE otherwise).
# Used in: main.tf module "ecr" image_tag_mutability → aws_ecr_repository.this.image_tag_mutability
#          (modules/ecr/main.tf, which also validates the value).
variable "ecr_image_tag_mutability" {
  description = "IMMUTABLE (module default) or MUTABLE. MUTABLE is required while images are pushed by hand to a fixed `:latest` tag; see the ecr module for why IMMUTABLE is the right end state."
  type        = string
  default     = "IMMUTABLE"
}

# Set by: default only.
# Used in: main.tf local.otel_sidecar image (only when enable_app_metrics = true).
variable "adot_image" {
  description = "AWS Distro for OpenTelemetry collector, run as a sidecar to scrape the backend's Prometheus /metrics. Public ECR, so it pulls with no credentials."
  type        = string
  default     = "public.ecr.aws/aws-observability/aws-otel-collector:latest"
}

# ---- Service sizing / scaling --------------------------------------------- #
# Sizing note: the backend image is NOT a thin API. It carries scikit-learn,
# numpy and SHAP, loads a severity model baked in at build time, and warms that
# model at boot; the app's own docker-compose gives it 1.5 vCPU / 1536 MB. Below
# 1024 MB the warmup and the first SHAP explanation are what OOM-kills the task,
# which presents as an ALB health check that never turns green. Fargate only
# allows 512/1024/2048 MB at 256 CPU units, so 1024 is the floor worth using.
# Backend Fargate task CPU in units (1024 = 1 vCPU).
# Set by: live/demo, live/staging (all 512).
# Used in: main.tf module "backend" cpu → aws_ecs_task_definition.this.cpu (modules/ecs_service/main.tf).
variable "backend_cpu" {
  type    = number
  default = 512
}
# Backend task memory in MiB (hard limit; exceeding it OOM-kills the task).
# Set by: live/demo, live/staging (all 1024).
# Used in: main.tf module "backend" memory → aws_ecs_task_definition.this.memory; also a
#          terraform_data.guards precondition when enable_app_metrics is on.
variable "backend_memory" {
  type    = number
  default = 1024

  validation {
    condition     = var.backend_memory >= 1024
    error_message = "backend_memory below 1024 MB OOM-kills the severity-model warmup (sklearn + SHAP + the baked model). Raise it, or change the app before lowering this."
  }
}
# How many backend tasks the ECS service keeps running.
# Set by: live/demo, live/staging (1).
# Used in: main.tf module "backend" desired_count → aws_ecs_service.this.desired_count, and
#          min_capacity (autoscaling floor); terraform_data.guards (single-task guard).
variable "backend_desired_count" {
  type    = number
  default = 1
}
# Set by: default only.
# Used in: main.tf module "backend" max_capacity → aws_appautoscaling_target.this.max_capacity
#          (only exists when enable_autoscaling = true); terraform_data.guards.
variable "backend_max_count" {
  description = "Autoscaling ceiling. Capped at 1 unless allow_multi_task_backend is set — the backend keeps clinical state in process memory."
  type        = number
  default     = 1
}

# Frontend (Next.js) Fargate CPU units — same meaning as backend_cpu.
# Set by: live/demo, live/staging (256).
# Used in: main.tf module "frontend" cpu → aws_ecs_task_definition.this.cpu in modules/ecs_service.
variable "frontend_cpu" {
  type    = number
  default = 256
}
# Set by: live/demo, live/staging (512).  Used in: module "frontend" memory (see frontend_cpu).
variable "frontend_memory" {
  type    = number
  default = 512
}
# Set by: live/demo (1); staging uses the default.
# Used in: module "frontend" desired_count → aws_ecs_service.this.desired_count (see frontend_cpu).
variable "frontend_desired_count" {
  type    = number
  default = 1
}

# Target-tracking autoscaling (Application Auto Scaling) for the BACKEND service.
# Set by: live/demo, live/staging (all false).
# Used in: main.tf module "backend" enable_autoscaling → aws_appautoscaling_target / _policy count in
#          modules/ecs_service; terraform_data.guards. The frontend has its own switch,
#          frontend_enable_autoscaling.
variable "enable_autoscaling" {
  type    = bool
  default = false
}

# Set by: default only. Used in: main.tf module "ecs_cluster" enable_container_insights.
variable "enable_container_insights" {
  description = "ECS Container Insights (billed CloudWatch custom metrics, ~$15-20/month for this stack if left up). Nothing in the stack reads them; Grafana already shows per-task CPU and memory."
  type        = bool
  default     = false
}

# ---- Scaling (see scaling.tf for the whole picture) ------------------------ #
# Target-tracking autoscaling for the FRONTEND. The Next.js server holds no
# state, so unlike the backend it can scale freely.
# Set by: live/staging (true); demo leaves the default.
# Used in: main.tf module "frontend" enable_autoscaling.
variable "frontend_enable_autoscaling" {
  description = "Autoscale the (stateless) frontend between frontend_desired_count and frontend_max_count."
  type        = bool
  default     = false
}
# Set by: live/staging (3). Used in: module "frontend" max_capacity; terraform_data.guards.
variable "frontend_max_count" {
  description = "Frontend autoscaling ceiling. Also a cost ceiling: the most frontend tasks a traffic spike can bill for."
  type        = number
  default     = 4
}
# Set by: default only. Used in: module "backend" and module "frontend" cpu_target.
variable "autoscaling_cpu_target" {
  description = "Average CPU %% each autoscaled service is held near."
  type        = number
  default     = 60

  validation {
    condition     = var.autoscaling_cpu_target > 0 && var.autoscaling_cpu_target <= 100
    error_message = "autoscaling_cpu_target is a percentage: 1-100."
  }
}
# Set by: default only. Used in: module "backend" and module "frontend" memory_target.
variable "autoscaling_memory_target" {
  description = "Average memory %% each autoscaled service is held near. null = no memory policy."
  type        = number
  default     = null
}
# Set by: live/staging. Used in: module "backend" requests_per_target.
variable "backend_requests_per_target" {
  description = <<-EOT
    ALB requests per backend task per minute to hold. null = CPU only.

    The backend's real bottleneck is concurrent triage runs, most of whose time
    is spent waiting on the hosted LLM rather than burning CPU, so this is the
    signal that actually tracks its load. Size it from a load test: roughly the
    requests one task serves per minute at the p95 latency you will accept. The
    app's own rate limit (rate_limit_per_min per client) is a floor on how much
    one busy client can generate.
  EOT
  type        = number
  default     = null
}
# Set by: live/staging. Used in: module "frontend" requests_per_target.
variable "frontend_requests_per_target" {
  description = "ALB requests per frontend task per minute to hold. null = CPU only."
  type        = number
  default     = null
}
# Set by: default only. Used in: module "backend" and module "frontend" on_demand_base.
variable "fargate_on_demand_base" {
  description = <<-EOT
    With use_fargate_spot, how many tasks of each app service stay on on-demand
    FARGATE; everything above runs on Spot. 0 = all Spot (the demo). 1 is the
    production shape: a Spot reclaim can take burst capacity, never the last
    task. Ignored when use_fargate_spot = false (everything is on-demand).
  EOT
  type        = number
  default     = 0

  validation {
    condition     = var.fargate_on_demand_base >= 0
    error_message = "fargate_on_demand_base cannot be negative."
  }
}

# The shared state table (modules/state_store) that would let the backend run
# more than one task. Off by default: the app has no DynamoDB client yet.
# Set by: default only.
# Used in: scaling.tf module "state_store" count, the task-role attachment, the
#          CAREROUTE_STATE_* env vars; module "networking" DynamoDB endpoint;
#          terraform_data.guards (allow_multi_task_backend needs a shared store).
variable "enable_shared_state_store" {
  description = "Create the DynamoDB state table and hand it to the backend (CAREROUTE_STATE_BACKEND / CAREROUTE_STATE_TABLE). The app must implement the store before the single-task guard can be lifted."
  type        = bool
  default     = false
}
# Set by: default only. Used in: module "state_store" point_in_time_recovery.
variable "state_store_point_in_time_recovery" {
  type    = bool
  default = true
}
# Set by: default only. Used in: module "state_store" deletion_protection.
variable "state_store_deletion_protection" {
  description = "Refuse to delete the state table. false by default because this stack is create-and-destroy; true for any environment whose data must outlive it."
  type        = bool
  default     = false
}

# Set by: live/demo (true), live/staging (false).
# Used in: main.tf use_fargate_spot on modules backend, frontend, sub_agents, monitoring, mlflow,
#          drift_monitor → in modules/ecs_service, aws_ecs_service.this uses a FARGATE_SPOT
#          capacity_provider_strategy when true, launch_type = "FARGATE" when false.
# (Note: the description's "%%" prints literally as "%%" — HCL only treats "%%{" as an escape.)
variable "use_fargate_spot" {
  description = "Cost lever: run all Fargate services on FARGATE_SPOT (~70%% cheaper, interruptible). Great for staging/demos."
  type        = bool
  default     = false
}

# ---- Sub-agent tier ------------------------------------------------------- #
# The proposal shows a "Sub-agent tier - ECS Fargate (one task per agent)".
#
# READ THIS BEFORE TURNING IT ON. In the app as it stands, the agents are Python
# objects inside ONE process: Symptom-Intake is itself the orchestrator
# (backend/app/agents/orchestration.py) and calls the others in-process, over an
# A2A message bus that is a Python object, not a network. The backend reads NO
# `AGENT_ROLE` variable — grep the app; it does not exist. So this tier does not
# split the pipeline: it deploys N extra copies of the whole backend image, each
# idle, each costing money, and the real triage still runs entirely inside the
# `backend` service.
#
# It is kept because it is the deployment TOPOLOGY the proposal's diagram shows
# and the shape the app would move into — one service per agent behind Cloud Map
# DNS — once the agents talk over HTTP. Until then, leave it off.
# Set by: live/demo (false); default elsewhere.
# Used in: main.tf module "sub_agents" for_each (empty set when false = zero services) and
#          local.need_cloud_map (creates the private DNS namespace).
variable "enable_subagent_tier" {
  type    = bool
  default = false
}

# Set by: default only.
# Used in: main.tf module "sub_agents" for_each = toset(var.sub_agents) → one ECS service per name,
#          addressed as module.sub_agents["care-routing"] etc.; each.value becomes the service name
#          and the AGENT_ROLE env var.
# [TF] for_each needs a set or map, hence the toset() in main.tf; unlike count, instances are keyed
# [TF] by name, so removing one list entry does not renumber (and recreate) the others.
variable "sub_agents" {
  description = "Agent names to stand up as separate services when enable_subagent_tier = true. Matches the six workers in backend/app/agents/ on integration-all-agents-2026-08-31 (intake is the orchestrator and stays in the `backend` service)."
  type        = list(string)
  default = [
    "severity-classifier", # agents/classifier.py — the ML severity model + LLM rationale
    "safety-override",     # agents/safety.py     — deterministic red-flag floor
    "care-routing",        # agents/routing.py    — CHAS/GoWhere + OneMap shortlist
    "human-in-the-loop",   # agents/hitl.py       — clinician escalation + resume
    "clinician-handoff",   # agents/handoff.py    — SBAR handoff summary
    "reflection",          # agents/reflection.py — self-critique pass
  ]
}

# ---- RAG memory store (Onyx) ---------------------------------------------- #
# The diagram's "Memory Store" used to be provisioned here as a Chroma Fargate
# task with its URL passed as CHROMA_URL. That was dead wiring: the app has no
# Chroma client and never read CHROMA_URL. Retrieval is backend/app/rag.py
# (in-process TF-IDF over a fixed clinical corpus), and its documented upgrade
# path is backend/app/rag_onyx.py — Onyx (formerly Danswer), self-hosted, which
# takes over automatically when BOTH variables below are set and otherwise
# silently falls back to TF-IDF.
#
# Onyx is NOT provisioned by this stack: a real instance is Postgres + Vespa +
# several services, far past a cost-minimal demo and a deployment of its own.
# Point these at an existing instance; leave blank for the TF-IDF fallback.
# Set by: default only (set it in live/<env> inputs to use Onyx).
# Used in: main.tf local.onyx_enabled (needs BOTH url and key) and local.rag_env → ONYX_BASE_URL env
#          var on the backend and sub-agent containers.
variable "onyx_base_url" {
  description = "Base URL of a self-hosted Onyx instance (e.g. https://onyx.internal). Blank => the backend uses its in-process TF-IDF retriever. Must be http(s); rag_onyx.py rejects any other scheme."
  type        = string
  default     = ""
}

# Set by: TF_VAR_onyx_api_key env var (masked CI variable) — never in inputs.
# Used in: main.tf local.onyx_enabled; aws_secretsmanager_secret_version.onyx secret_string →
#          injected into containers as ONYX_API_KEY through ECS `secrets` (local.onyx_secrets).
# [TF] `sensitive = true` hides the value in plan/apply output. It is STILL stored in plain text in
# [TF] the state file (the S3 state bucket from root.hcl), which is why that bucket is encrypted.
variable "onyx_api_key" {
  description = "Onyx API key (Admin Panel -> API keys). Stored in Secrets Manager and injected as ONYX_API_KEY; pass via TF_VAR_onyx_api_key, never hardcode. Both this AND onyx_base_url are required before Onyx is used."
  type        = string
  default     = ""
  sensitive   = true
}

# Set by: TF_VAR_langsmith_api_key env var (masked CI variable) — never in inputs.
# Used in: main.tf local.langsmith_enabled; aws_secretsmanager_secret_version.langsmith →
#          injected as LANGSMITH_API_KEY through ECS `secrets` (local.langsmith_secrets).
# Empty (the default) => no secret is created and the app's LangSmith sink stays off.
variable "langsmith_api_key" {
  description = "LangSmith API key for the app's optional LLM tracing (backend/app/tracing.py). Stored in Secrets Manager, injected as LANGSMITH_API_KEY; pass via TF_VAR_langsmith_api_key, never hardcode. Empty => tracing off."
  type        = string
  default     = ""
  sensitive   = true
}

# ---- LLM backend: OpenAI (direct) ------------------------------------------ #
# The realistic cloud default is a hosted API: no GPU to run, the backend just
# needs an API key (stored in Secrets Manager, injected as a container secret).
#
# The app reaches the hosted LLM through its `openai` provider (OpenAI Chat
# Completions REST shape). The app has NO native `anthropic` provider, and its
# `claude_cli` provider needs the local Claude Code binary — absent in a
# container — so the cloud path is always `openai`. To use *Claude*, point
# openai_base_url at an OpenAI-compatible gateway that fronts Claude:
#   - Anthropic's OpenAI-compat endpoint: https://api.anthropic.com/v1
#       + openai_model = "claude-opus-4-8", openai_api_key = an Anthropic key
#   - or OpenRouter, etc.
# No app code change is required either way.
# Set by: live/demo, live/staging ("openai").
# Used in: main.tf local.llm_env LLM_PROVIDER_ORDER → env var on backend and sub-agent containers.
variable "llm_provider_order" {
  description = "App LLM_PROVIDER_ORDER. 'openai' is the only provider that works in the container (point openai_base_url at OpenAI or a Claude-compatible gateway). 'claude_cli' is local-dev only."
  type        = string
  default     = "openai"
}

# Set by: TF_VAR_openai_api_key env var (masked CI variable, or `export` locally).
# Used in: main.tf local.openai_enabled (key != ""); aws_secretsmanager_secret_version.openai
#          secret_string → injected as OPENAI_API_KEY via ECS `secrets` (local.llm_secrets), so the
#          key never appears in the task definition.
variable "openai_api_key" {
  description = "API key for the openai_base_url endpoint (an OpenAI key, or an Anthropic key when base_url is Anthropic's OpenAI-compat endpoint). Pass via TF_VAR_openai_api_key / masked CI variable — never hardcode. Empty => app uses deterministic fallback."
  type        = string
  default     = ""
  sensitive   = true
}

# Set by: live/demo, live/staging ("gpt-4o-mini").
# Used in: main.tf local.llm_env OPENAI_MODEL env var.
variable "openai_model" {
  description = "Model name for the openai provider. e.g. 'gpt-4o-mini' for OpenAI, or 'claude-opus-4-8' against Anthropic's OpenAI-compat endpoint."
  type        = string
  default     = "gpt-4o-mini"
}

# Set by: live/demo. Used in: main.tf local.llm_env -> OPENAI_MODEL_FAST / _DEEP / _MAX.
# The app's LLM router picks a tier per task; empty = that tier uses openai_model.
variable "openai_model_fast" {
  description = "Model for light tasks (intake normalisation, routing, handoff summary, interview question). Empty = openai_model."
  type        = string
  default     = ""
}

variable "openai_model_deep" {
  description = "Model for heavy tasks (acuity classification, semantic red-flag check, Reflection critic). Empty = openai_model."
  type        = string
  default     = ""
}

variable "openai_model_max" {
  description = "Model for calls escalated as hard (low confidence, thin evidence, Reflection re-run). Empty = the deep model."
  type        = string
  default     = ""
}

# Set by: default only.
# Used in: main.tf local.llm_env OPENAI_BASE_URL env var.
variable "openai_base_url" {
  description = "OpenAI-compatible endpoint. Default is OpenAI; set to https://api.anthropic.com/v1 (or an OpenRouter URL) to use Claude through the 'openai' provider with no code change."
  type        = string
  default     = "https://api.openai.com/v1"
}

# ---- Backend runtime safety controls (config.py CAREROUTE_*) -------------- #
# Set by: live/demo, live/staging (30).
# Used in: main.tf local.safety_env CAREROUTE_RATE_LIMIT_PER_MIN (tostring(): env values must be strings)
#          → backend and sub-agent container environment.
variable "rate_limit_per_min" {
  description = "Per-client triage requests/min (LLM10 unbounded-consumption guard). Maps to CAREROUTE_RATE_LIMIT_PER_MIN."
  type        = number
  default     = 30
}

# Set by: default only.
# Used in: main.tf local.safety_env CAREROUTE_KILL_SWITCH ("1"/"0" via a ternary).
variable "kill_switch" {
  description = "ASI10 kill switch: when true, LLM providers are short-circuited and the pipeline runs its deterministic, auditable rules only. Maps to CAREROUTE_KILL_SWITCH."
  type        = bool
  default     = false
}

# Set by: default only.
# Used in: main.tf local.trusted_proxies (adds local.alb_subnet_cidrs) → CAREROUTE_TRUSTED_PROXIES env
#          var; echoed by outputs.tf backend_trusted_proxies.
variable "trust_alb_as_proxy" {
  description = <<-EOT
    Tell the backend to believe X-Forwarded-For from the load balancer.

    This is a CORRECTNESS setting behind an ALB, not hardening. The app's rate
    limiter keys on the direct peer address unless that peer is listed in
    CAREROUTE_TRUSTED_PROXIES (main.py::_client_key). Behind an ALB the direct
    peer is always an ALB node, so with the app default (trust nobody) EVERY
    patient in the world shares one 30-requests/minute bucket and the demo
    starts returning 429s to everybody as soon as two people use it.

    Setting this true injects the ALB's own subnet CIDRs, so the limiter keys on
    the real client again. The cost of the wider trust is that anything else in
    those subnets could forge XFF — in this stack that is only our own tasks.
  EOT
  type        = bool
  default     = true
}

# Set by: default only.
# Used in: main.tf local.trusted_proxies (appended; compact() drops it when "").
variable "extra_trusted_proxies" {
  description = "Additional IPs/CIDRs appended to CAREROUTE_TRUSTED_PROXIES (e.g. a CDN in front of the ALB). Comma-separated."
  type        = string
  default     = ""
}

# Set by: default only.
# Used in: main.tf local.cors_origins (this wins when non-empty, else local.computed_cors_origins)
#          → CAREROUTE_CORS_ORIGINS env var; echoed by outputs.tf backend_cors_origins.
variable "cors_origins" {
  description = "CAREROUTE_CORS_ORIGINS — browser origins allowed to call the API. Blank => the stack computes it from the deployed entry point (API Gateway endpoint or ALB DNS). Never \"*\": the app sends allow_credentials."
  type        = string
  default     = ""
}

# Set by: default only.
# Used in: main.tf local.safety_env CAREROUTE_SSE_HEARTBEAT_SECONDS. Keep below alb_idle_timeout.
variable "sse_heartbeat_seconds" {
  description = "CAREROUTE_SSE_HEARTBEAT_SECONDS — cadence of the SSE comment frame the backend emits while the pipeline is silent, so idle timers between it and the browser never fire mid-triage. Must stay comfortably under the smallest idle timeout on the path (Next's 30 s proxyTimeout; alb_idle_timeout here). 0 disables."
  type        = number
  default     = 10
}

# Set by: default only.
# Used in: main.tf local.safety_env — CAREROUTE_REFLECTION_MAX_ITERS is added only when > 0.
variable "reflection_max_iters" {
  description = "CAREROUTE_REFLECTION_MAX_ITERS — how many self-critique passes the Reflection agent may run. Each one is an extra LLM round-trip, so it is both a latency and a spend lever. Blank/0 leaves the app default."
  type        = number
  default     = 0
}

# Set by: default only.
# Used in: main.tf local.safety_env — CAREROUTE_REFLECTION_BUDGET_MS is added only when > 0.
variable "reflection_budget_ms" {
  description = "CAREROUTE_REFLECTION_BUDGET_MS — wall-clock budget for the whole reflection loop. 0 leaves the app default."
  type        = number
  default     = 0
}

# ---- Safety NLP shadow pipeline ------------------------------------------- #
# Phase 4 Safety NLP (backend/app/safety_nlp/) is SHADOW-ONLY: it never moves
# the deterministic red-flag floor, it only records what it would have said.
# Set by: default only.
# Used in: main.tf local.safety_nlp_env CAREROUTE_SAFETY_NLP ("1"/"0") → backend and sub-agent env.
variable "enable_safety_nlp" {
  description = <<-EOT
    Turn on the Safety NLP shadow pipeline (CAREROUTE_SAFETY_NLP).

    Leave this OFF unless the deployed image was built with
    --build-arg WITH_SAFETY_NLP=1 (torch + transformers + the pinned models;
    the app's build:images job does this for careroute-backend). Against an
    image without them the app keeps triaging correctly on its deterministic
    rules, and /api/health reports safetyNlp="rules" instead of "model".
    Needs backend_memory >= 3072 (guarded in main.tf terraform_data.guards).
  EOT
  type        = bool
  default     = false
}

# Set by: live/demo.
# Used in: main.tf local.safety_nlp_env CAREROUTE_SAFETY_NLP_DIRECT_NLI (only with enable_safety_nlp).
variable "safety_nlp_direct_nli" {
  description = "CAREROUTE_SAFETY_NLP_DIRECT_NLI — add the mDeBERTa multilingual NLI comparator to the category stage. Needs an image built with WITH_SAFETY_NLP_DIRECT_NLI=1 and backend_memory >= 4096."
  type        = bool
  default     = false
}

# Set by: live/demo (false for the first rollout).
# Used in: main.tf local.safety_nlp_env CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION.
variable "safety_prototype_semantic_activation" {
  description = <<-EOT
    CAREROUTE_SAFETY_PROTOTYPE_SEMANTIC_ACTIVATION — let the semantic safety
    layers RAISE acuity instead of only recording what they would do (shadow).
    Roll out in two task-definition revisions: first false, verify warm-up and
    shadow telemetry, then true. Rolling back = the previous revision, or false.
  EOT
  type        = bool
  default     = false
}

# Set by: live/demo.
# Used in: main.tf local.safety_nlp_env CAREROUTE_SAFETY_LLM (+ provider order "openai").
variable "safety_llm_model" {
  description = "CAREROUTE_SAFETY_LLM_MODEL — the model the safety LLM adjudicator pins (it bypasses the router). Empty = the app default (OPENAI_MODEL). It runs on every case the rules miss, inside a 5 s budget, so pick for latency as well as accuracy."
  type        = string
  default     = ""
}

# Set by: live/demo.
# Used in: main.tf local.safety_nlp_env CAREROUTE_SAFETY_LLM (+ provider order "openai").
variable "enable_safety_llm" {
  description = "CAREROUTE_SAFETY_LLM — the bounded LLM red-flag adjudicator in the Safety-Override agent. Uses the OPENAI_API_KEY secret the task already receives; the kill switch still disables it."
  type        = bool
  default     = false
}

# Set by: default only.
# Used in: main.tf local.safety_nlp_env CAREROUTE_SAFETY_NLP_TIMEOUT_MS.
variable "safety_nlp_timeout_ms" {
  description = "CAREROUTE_SAFETY_NLP_TIMEOUT_MS — per-request budget for the shadow pipeline. It is shadow work on the critical path, so it must stay small."
  type        = number
  default     = 1000
}

# Set by: default only.
# Used in: main.tf local.safety_nlp_env CAREROUTE_SAFETY_NLP_MAX_INPUT_CHARS.
variable "safety_nlp_max_input_chars" {
  description = "CAREROUTE_SAFETY_NLP_MAX_INPUT_CHARS — input truncation bound for the shadow pipeline."
  type        = number
  default     = 2000
}

# ---- OneMap (Care Routing travel times) ----------------------------------- #
# Without these the Care Routing agent still answers, but with an explicitly
# labelled straight-line estimate instead of a real travel time and route
# geometry — a visibly degraded demo. Free account at onemap.gov.sg.
# Set by: TF_VAR_onemap_email env var (masked CI variable).
# Used in: main.tf local.onemap_enabled (needs email AND password); aws_secretsmanager_secret_version
#          .onemap stores jsonencode({email, password}) → ECS secret ONEMAP_EMAIL ("<arn>:email::").
variable "onemap_email" {
  description = "OneMap account email. Stored in Secrets Manager with the password (one JSON secret) and injected as ONEMAP_EMAIL. Pass via TF_VAR_onemap_email."
  type        = string
  default     = ""
  sensitive   = true
}

# Set by: TF_VAR_onemap_password env var (masked CI variable).
# Used in: main.tf local.onemap_enabled; the same JSON secret → ONEMAP_PASSWORD ("<arn>:password::").
variable "onemap_password" {
  description = "OneMap account password -> ONEMAP_PASSWORD. Pass via a masked CI variable TF_VAR_onemap_password, never hardcode. (The team's shared credential was leaked in chat on 2026-09-01 and is due for rotation — see the app repo's Infra-Dependent Work note.)"
  type        = string
  default     = ""
  sensitive   = true
}

# ---- Database ------------------------------------------------------------- #
# RDS instance size (db.t4g.micro = smallest Graviton burstable class). Only matters when enable_rds.
# Set by: default only.
# Used in: main.tf module "rds" instance_class → aws_db_instance.this.instance_class (modules/rds).
variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}
# Multi-AZ = a synchronous standby in a second AZ with automatic failover (doubles the cost).
# Set by: default only.  Used in: module "rds" multi_az → aws_db_instance.this.multi_az.
variable "db_multi_az" {
  type    = bool
  default = false
}
# Blocks DeleteDBInstance (and so `terraform destroy`) until turned off.
# Set by: default only.  Used in: module "rds" deletion_protection → aws_db_instance.this.deletion_protection.
variable "db_deletion_protection" {
  type    = bool
  default = false
}
# true = no snapshot on destroy; false = take "<prefix>-postgres-final" first.
# Set by: default only.  Used in: module "rds" skip_final_snapshot → aws_db_instance.this.
variable "db_skip_final_snapshot" {
  type    = bool
  default = true
}

# ---- API Gateway ---------------------------------------------------------- #
# API Gateway stage throttling: steady-state requests/second and the burst bucket size.
# Set by: default only (both). Only matters when enable_api_gateway = true.
# Used in: main.tf module "api_gateway" throttle_rate_limit / throttle_burst_limit →
#          aws_apigatewayv2_stage.default default_route_settings (modules/api_gateway).
variable "api_throttle_rate_limit" {
  type    = number
  default = 50
}
# Set by: default only.  Used in: module "api_gateway" throttle_burst_limit (see above).
variable "api_throttle_burst_limit" {
  type    = number
  default = 100
}

# ---- Load balancer -------------------------------------------------------- #
# Seconds the ALB keeps an idle connection open (no bytes either way) before closing it.
# Set by: default only.
# Used in: main.tf module "alb" idle_timeout → aws_lb.this.idle_timeout.
variable "alb_idle_timeout" {
  description = "ALB idle timeout (s). POST /api/triage/stream is a long-lived SSE response, so this must exceed the longest silent gap in a triage run. See modules/alb for the measurement."
  type        = number
  default     = 120
}

# ---- Observability -------------------------------------------------------- #
# Email address subscribed to the alarms SNS topic ("" = no subscription). AWS emails a
# confirmation link first; alarms reach the inbox only after it is clicked.
# Set by: default only — no environment sets it.
# Used in: main.tf module "observability" alarm_email → aws_sns_topic_subscription (modules/observability).
variable "alarm_email" {
  type    = string
  default = ""
}
# CloudWatch Logs retention (days) for the app log group.
# Set by: live/demo (3), live/staging (14).
# Used in: main.tf module "ecs_cluster" → aws_cloudwatch_log_group.this.retention_in_days; also
#          module "networking" flow_log_retention_days and module "api_gateway" log_retention_days.
variable "log_retention_days" {
  type    = number
  default = 30
}

# Set by: live/staging (true), live/demo (false).
# Used in: main.tf local.otel_sidecar (ADOT container added to the backend task);
#          data.aws_iam_policy_document.otel_emf + aws_iam_role_policy.otel_emf (lets the task write
#          EMF logs); module "observability" enable_app_metrics (alarms + dashboard rows);
#          terraform_data.guards precondition; outputs.tf app_metrics_namespace.
variable "enable_app_metrics" {
  description = <<-EOT
    Scrape the backend's Prometheus /metrics with an ADOT sidecar and publish it
    to CloudWatch (EMF), then alarm on it.

    This is the cloud half of the app's MLOps monitoring pillar. Without it the
    only thing AWS knows about the triage service is CPU, memory and HTTP
    status: every clinical signal the app actually exports — escalation rate,
    guardrail blocks, per-agent latency, served acuity mix, clinician agreement
    — exists solely inside a container nobody scrapes, and the five rules in the
    app's monitoring/alert.rules.yml have no cloud equivalent at all.

    Costs a handful of custom metrics plus a little log ingest, so it is off by
    default for the throwaway demo and should be ON anywhere that runs longer
    than a presentation.
  EOT
  type        = bool
  default     = false
}

# Set by: default only.
# Used in: main.tf local.otel_config (awsemf exporter namespace); module "observability"
#          (custom-metric alarms and dashboard widgets); outputs.tf app_metrics_namespace.
variable "app_metrics_namespace" {
  description = "CloudWatch namespace for the app's own metrics."
  type        = string
  default     = "CareRoute/App"
}

# ---- Prometheus / Alertmanager / Grafana on ECS ---------------------------- #
# Set by: live/demo, live/staging (all true).
# Used in: main.tf module "monitoring" (source ../monitoring_stack, count); random_password.grafana
#          and aws_secretsmanager_secret(_version).grafana; local.need_cloud_map and
#          local.backend_service_discovery; module "alb" enable_grafana_route (/grafana listener rule);
#          outputs.tf grafana_url, grafana_admin_password, prometheus_*, alertmanager_internal_url.
variable "enable_prometheus_stack" {
  description = <<-EOT
    Deploy Prometheus + Alertmanager + Grafana as Fargate tasks, using the same
    configs and dashboard the application runs in its docker-compose.

    This is the observability path to switch on when the deployed architecture
    has to MATCH the tooling being demonstrated. The ADOT/CloudWatch path
    (enable_app_metrics) is cheaper and needs no extra tasks, but CloudWatch
    renders a Prometheus histogram as a StatisticSet, so alert.rules.yml's p95
    rules cannot be expressed there and the Grafana dashboard does not exist in
    the cloud at all.

    The two are independent and can both be on: Prometheus for the demo and the
    report, CloudWatch for alarms that outlive a throwaway Grafana task.

    Cost: three extra Fargate Spot tasks, ~$0.02/hr together — cents for a demo,
    about $13/mo if left running. It also forces the private DNS namespace on,
    since Prometheus finds the backend through Cloud Map.
  EOT
  type        = bool
  default     = false
}

# Set by: default only.
# Used in: main.tf module "monitoring" prometheus_image → the Prometheus task definition's container
#          image in modules/monitoring_stack (alertmanager_image / grafana_image below work the same way).
variable "prometheus_image" {
  description = "Pinned, not :latest — an observability stack that silently changes version between applies is not observable."
  type        = string
  default     = "prom/prometheus:v3.1.0"
}

# Set by: default only.  Used in: module "monitoring" alertmanager_image (see prometheus_image).
variable "alertmanager_image" {
  type    = string
  default = "prom/alertmanager:v0.28.0"
}

# Set by: default only.  Used in: module "monitoring" grafana_image (see prometheus_image).
variable "grafana_image" {
  type    = string
  default = "grafana/grafana:11.5.1"
}

# Set by: default only.
# Used in: main.tf module "monitoring" scrape_interval → global.scrape_interval in the generated
#          prometheus.yml (modules/monitoring_stack).
variable "prometheus_scrape_interval" {
  description = "Prometheus scrape interval. Unlike the CloudWatch path nothing here bills per datapoint, so this matches the app's compose config."
  type        = string
  default     = "15s"
}

# Set by: optional TF_VAR_grafana_admin_password env var; otherwise empty.
# Used in: main.tf random_password.grafana (count: generated only when this is "");
#          aws_secretsmanager_secret_version.grafana secret_string (this value, or the random one);
#          read back by outputs.tf grafana_admin_password.
variable "grafana_admin_password" {
  description = "Grafana admin password. Leave blank and a strong one is generated and stored in Secrets Manager — read it back with `terragrunt output -raw grafana_admin_password`. Never hardcode; pass via TF_VAR_grafana_admin_password if you want a specific value."
  type        = string
  default     = ""
  sensitive   = true
}

# Fargate size used for EACH of the Prometheus, Alertmanager and Grafana tasks.
# Set by: default only.
# Used in: main.tf module "monitoring" cpu / memory → the three task definitions in modules/monitoring_stack.
variable "monitoring_cpu" {
  type    = number
  default = 256
}

# Set by: default only.  Used in: module "monitoring" memory (see monitoring_cpu above).
variable "monitoring_memory" {
  type    = number
  default = 512
}

# Set by: default only.
# Used in: main.tf local.otel_config (prometheus receiver scrape_interval of the ADOT sidecar).
variable "app_metrics_scrape_interval" {
  description = "How often the ADOT sidecar scrapes /metrics. Each scrape is a CloudWatch datapoint per series, so this is the main cost dial; 60s is plenty for alerting."
  type        = string
  default     = "60s"
}

# ---- Artifacts bucket (DVC remote + ML telemetry) ------------------------- #
# Set by: live/staging (true), live/demo (false).
# Used in: main.tf module "artifacts" (count) = the S3 bucket; aws_iam_role_policy_attachment.task_artifacts
#          (task role may read/write it); gates telemetry shipping, mlflow, drift_monitor and ALB access
#          logs; outputs.tf artifacts_bucket and dvc_remote_url.
variable "enable_artifacts_bucket" {
  description = "Provision the S3 bucket that backs the app repo's DVC remote (backend/.dvc/config is empty today, so the dataset is reproducible but not versioned) and receives the backend's inference/drift telemetry, which Fargate's ephemeral disk otherwise destroys on every deploy. Storage-only: costs ~nothing when near-empty."
  type        = bool
  default     = false
}

# ---- Extra backend env ---------------------------------------------------- #
# Set by: default only.
# Used in: main.tf module "backend" environment = merge(..., var.extra_backend_env) — merged LAST.
# [TF] merge() combines maps left to right and later keys win, so an entry here overrides any
# [TF] computed env var of the same name.
variable "extra_backend_env" {
  description = "Additional plain env vars for the backend (e.g. LLM_PROVIDER_ORDER)."
  type        = map(string)
  default     = {}
}

# Extra tags for every resource.
# Set by: default only.
# Used in: main.tf local.common_tags = merge(var.tags, {Project, Environment, ManagedBy}) → `tags` of
#          every resource/module. The fixed keys come second in merge(), so they win on a clash.
#          (root.hcl's provider default_tags add the same keys again at the provider level.)
variable "tags" {
  type    = map(string)
  default = {}
}

# ---- CD: deployment rollback + app CI access ------------------------------ #
# All four below are read in deploy_safety.tf.

# Set by: default only (true). Used in: aws_cloudwatch_metric_alarm.deploy_rollback
# and the rollback_alarm_names input of module "backend" / module "frontend".
variable "enable_deploy_rollback_alarms" {
  description = "Create the 5xx-rate alarms that ECS watches during a deployment, and roll back on. Two alarms, no SNS action: ~USD 0.20/month."
  type        = bool
  default     = true
}

# Set by: default only. Used in: the alarm threshold.
variable "deploy_rollback_error_rate" {
  description = <<-EOT
    5xx share of a service's requests that rolls a deployment back. 0.01 is the
    course gate the app repo enforces in backend/app/ml/canary.py
    (MAX_ERROR_RATE); keep the two equal, or the rollback rule in AWS and the
    one the app's tests describe will disagree.
  EOT
  type        = number
  default     = 0.01
}

# Set by: default only. Used in: the IF() floor in the alarm expression.
variable "deploy_rollback_min_requests" {
  description = "Requests per minute below which the rollback alarm reports 0. Stops one failed request on an idle demo from reverting a release."
  type        = number
  default     = 10
}

# Set by: default only (true). Used in: count on module "app_ci".
variable "enable_app_ci_role" {
  description = "Create the push-only IAM role the app repo's publish:images job assumes over GitLab OIDC."
  type        = bool
  default     = true
}

# Set by: default only. Used in: module "app_ci".
variable "gitlab_url" {
  type    = string
  default = "https://gitlab.com"
}

# Set by: default only. Used in: module "app_ci" trust policy.
# The NUMERIC id (Settings -> General -> "Project ID" in the app repo), not the
# path: this project's path is on GitLab's burned-path tombstone list, so a
# path-based `sub` claim is never issued for it. See modules/ci_oidc/main.tf.
variable "app_ci_project_id" {
  description = "Numeric GitLab project id of the app project allowed to push images."
  type        = string
  default     = "84456994" # teamproject4040840/nus-iss3/careroute_ai/careroute_ai_app
}

# Set by: default only. Used in: module "app_ci" trust policy.
variable "app_ci_ref" {
  description = "Only pipelines on this (protected) app branch may push."
  type        = string
  default     = "main"
}

# Set by: default. Used in: module "app_ci" (deploy_safety.tf) trust policy.
variable "app_ci_extra_refs" {
  description = "Extra app branches that may push and deploy: release/deploy runs the build/deploy path without the scanners. Protect it in GitLab."
  type        = list(string)
  default     = ["release/deploy"]
}

# Set by: live/<env> when a second environment shares the account.
# Used in: module "app_ci" (create vs look up the account-wide OIDC provider).
variable "create_gitlab_oidc_provider" {
  description = "Create the account's GitLab OIDC provider. Set false in every environment but the first one applied into an AWS account."
  type        = bool
  default     = true
}

# ---- Staff API key (clinical escalation endpoints) ------------------------ #
# Set by: default only (true). Used in: staff_auth.tf, the backend/frontend
# secrets, Cloud Map membership of the backend, and the ALB escalation rule.
variable "enable_staff_api_key" {
  description = "Generate CAREROUTE_STAFF_API_KEY, inject it into backend + frontend, and route /api/escalations* through the frontend's server-side proxy that attaches it. false = the escalation endpoints are open to anyone who can reach the ALB."
  type        = bool
  default     = true
}

# Set by: masked CI variable TF_VAR_staff_password (never in code).
variable "staff_password" {
  description = "Staff-portal password -> Secrets Manager -> frontend CAREROUTE_STAFF_PASSWORD. The frontend checks it server-side and issues the session cookie the escalation proxy requires. Empty = a random password nobody knows, so the portal stays locked (fails closed) until the variable is set."
  type        = string
  default     = ""
  sensitive   = true
}
