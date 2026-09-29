###############################################################################
# Module: mlflow
# -----------------------------------------------------------------------------
# An MLflow tracking server on Fargate, backed by S3 for artifacts and by
# Postgres (or, for a demo, ephemeral SQLite) for run metadata.
#
# WHY. "Set up an S3 bucket — store ML runs by syncing the local mlruns folder;
# centralized model registry" is the FIRST instruction on the AWS slide of the
# Platform deck, and it repeats verbatim for Azure (p8) and GCP (p11). It is the
# most-repeated infrastructure instruction in that deck, and this repo had no
# implementation of it at all.
#
# The application half already exists and is waiting for this: ml/train.py and
# ml/lifecycle.py both read MLFLOW_TRACKING_URI, and lifecycle.pin_tracking_uri()
# falls back to a local store when it is unset. Without a server, the
# champion/challenger promotion the app implements is inert — runs registered by
# one CI job are invisible to the next, because they were written to a container
# filesystem that no longer exists.
#
# ── TWO THINGS TO KNOW BEFORE ENABLING ──────────────────────────────────────
#
# 1. THE TRACKING SERVER HAS NO AUTHENTICATION. MLflow ships an optional
#    basic-auth app, but it needs its own user database and is not wired here.
#    So this module defaults to INTERNAL ONLY: reachable through Cloud Map from
#    inside the VPC, with no ALB route. `expose_publicly = true` adds the route
#    and puts an unauthenticated model registry on the internet — acceptable
#    only behind TLS and a restricted source range, and never with real data.
#
# 2. INTERNAL-ONLY MEANS GITLAB CI CANNOT REACH IT, and CI is where training
#    actually runs. That is a genuine limitation of the cheap shape, not an
#    oversight: closing it properly needs either a VPC-resident runner or a
#    private link from the runner network. Until one of those exists, this
#    server serves in-VPC consumers (the scheduled monitor, the backend) and the
#    CI story stays what it is today.
###############################################################################

# ---- Inputs ---------------------------------------------------------------- #
# [TF] Each `variable` is an input, set by `module "mlflow" { ... }` in
#      modules/careroute_stack/main.tf and read here as var.<name>. That block
#      has `count = var.enable_mlflow_server && var.enable_artifacts_bucket ? 1 : 0`,
#      and no live/ environment turns enable_mlflow_server on today, so this
#      module is currently not deployed anywhere.
#
# name_prefix: local.name_prefix ("careroute-<env>"). Used in: task def family, Name tag.
# region: var.region (root.hcl <- env.hcl). Used in: awslogs-region.
variable "name_prefix" { type = string }
variable "region" { type = string }

# cluster/roles/log group, all from module.ecs_cluster outputs:
#   cluster_id -> aws_ecs_service.this.cluster.
#   execution_role_arn (pulls image, fetches the backend-store secret) and
#   task_role_arn (what `mlflow server` itself runs as; it gets S3 access
#   because the stack attaches the artifacts read/write policy to this role)
#   -> the task definition. log_group_name -> awslogs-group.
variable "cluster_id" { type = string }
variable "execution_role_arn" { type = string }
variable "task_role_arn" { type = string }
variable "log_group_name" { type = string }

# subnet_ids: local.task_subnet_ids. security_group_ids: [module.security.ecs_sg_id]
# (its self-referencing rule lets other tasks reach :5000). Both -> network_configuration.
variable "subnet_ids" { type = list(string) }
variable "security_group_ids" { type = list(string) }

# Set by: local.assign_public_ip (= var.public_networking). -> network_configuration.
variable "assign_public_ip" {
  type    = bool
  default = false
}

# Set by: var.use_fargate_spot. Used in: launch_type + the capacity provider block.
variable "use_fargate_spot" {
  type    = bool
  default = true
}

# Set by: var.mlflow_image (same default). Used in: local.container.image.
variable "image" {
  description = <<-EOT
    MLflow server image. **Must match the client version the app pins** —
    backend/requirements.txt has `mlflow==3.13.0`, and a 3.x client against a
    2.x server fails on the registry API, which is precisely the half this
    module exists to enable. Pinned, not `:latest`, for the same reason the
    observability images are.
  EOT
  type        = string
  default     = "ghcr.io/mlflow/mlflow:v3.13.0"
}

# Set by: local.artifacts_bucket_name = module.artifacts[0].bucket_name.
# Used in: local.artifact_root (s3://<bucket>/mlruns).
variable "artifact_bucket" {
  description = "Bucket holding the artifact root. The task role must already be able to read/write it — the stack attaches the artifacts bucket policy."
  type        = string
}

# Default-only (the stack does not pass it). Used in: local.artifact_root.
variable "artifact_prefix" {
  type    = string
  default = "mlruns"
}

# Set by: `var.enable_rds ? "" : "sqlite:////tmp/mlflow.db"` in careroute_stack.
# Used in: the MLFLOW_BACKEND_STORE_URI env var, only when no secret is given.
variable "backend_store_uri" {
  description = <<-EOT
    Run-metadata store. A Postgres URI (`postgresql://user:pass@host:5432/db`)
    when RDS is on; otherwise the stack passes a SQLite path on the task's own
    filesystem.

    SQLite here is DEMO ONLY and worth naming plainly: Fargate task storage is
    ephemeral, so every redeploy or Spot reclaim empties the registry while the
    artifacts in S3 survive — leaving orphaned artifacts and a registry that has
    forgotten the runs that produced them. That is a worse failure than having no
    server, because it looks like it is working.
  EOT
  type        = string
}

# Set by: `var.enable_rds ? aws_secretsmanager_secret.mlflow_store[0].arn : ""`
# (that secret holds a postgresql:// URI built from module.rds outputs).
# Used in: local.uses_secret -> the container's `secrets` list.
variable "backend_store_secret_arn" {
  description = "Optional Secrets Manager ARN holding the backend store URI, used instead of the plain variable so a database password never lands in the task definition. When set, backend_store_uri is ignored at runtime."
  type        = string
  default     = ""
}

# port / static_prefix / target_group_arn are default-only: the stack passes none
# of them, and modules/alb has no MLflow route, so the service stays internal.
# (The header's `expose_publicly` flag does not exist as a variable.)
# port -> portMappings, the --port flag, health check, tracking URI output.
variable "port" {
  type    = number
  default = 5000
}

# -> --static-prefix, the health-check URL and the tracking URI output.
variable "static_prefix" {
  description = "Sub-path MLflow serves from, so it can share the app's ALB rather than needing a second one. Must match the ALB listener rule."
  type        = string
  default     = "/mlflow"
}

# null = "not set". Used in: the `load_balancer` dynamic block and
# health_check_grace_period_seconds (both only apply with a target group).
variable "target_group_arn" {
  description = "ALB target group to register with. null = internal only (the default and the safe choice — see the module header)."
  type        = string
  default     = null
}

# Set by: `local.need_cloud_map ? aws_service_discovery_private_dns_namespace.this[0].id : null`.
# Used in: aws_service_discovery_service.this dns_config.namespace_id.
# NOTE: need_cloud_map only covers the sub-agent tier and the Prometheus stack,
# so MLflow enabled without either would receive null here.
variable "service_discovery_namespace_id" { type = string }

# Set by: "${var.project}.local". Used in: the internal_tracking_uri output only.
variable "namespace_name" {
  description = "Cloud Map private DNS namespace name (e.g. careroute.local). Used to build the in-VPC tracking URI."
  type        = string
}

# Fargate task size (512 = 0.5 vCPU, 1024 MiB). Set by: var.mlflow_cpu / mlflow_memory.
variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

# Set by: local.common_tags. Used in: task definition + ECS service tags.
variable "tags" {
  type    = map(string)
  default = {}
}

# [TF] `locals` = values computed inside this module, read as local.<name>.
locals {
  name = "mlflow"

  artifact_root = "s3://${var.artifact_bucket}/${var.artifact_prefix}"

  uses_secret = var.backend_store_secret_arn != ""

  # The server is started through a shell so the backend store URI can come from
  # an environment variable that ECS populated from Secrets Manager. Passing it
  # as an argv element in the task definition would put a database password in
  # plaintext in the ECS console, in CloudTrail, and in `terraform show`.
  command = ["/bin/sh", "-c", join(" ", [
    "mlflow server",
    "--host 0.0.0.0",
    "--port ${var.port}",
    "--backend-store-uri \"$MLFLOW_BACKEND_STORE_URI\"",
    "--default-artifact-root ${local.artifact_root}",
    "--static-prefix ${var.static_prefix}",
    # Gunicorn defaults to one worker; two keeps a slow artifact listing from
    # blocking the health check and flapping the target group.
    "--workers 2",
  ])]

  # One ECS container definition written as an HCL object; the key names
  # (entryPoint, portMappings, logConfiguration, ...) are the ECS JSON field names
  # because it is jsonencode()d as-is below.
  container = {
    name         = local.name
    image        = var.image
    essential    = true
    entryPoint   = local.command
    portMappings = [{ containerPort = var.port, protocol = "tcp" }]
    # [TF] concat() joins lists; `cond ? [] : [...]` adds the plain env var only
    #      when the value is NOT coming from Secrets Manager.
    environment = concat(
      [{ name = "MLFLOW_S3_IGNORE_TLS", value = "false" }],
      local.uses_secret ? [] : [{ name = "MLFLOW_BACKEND_STORE_URI", value = var.backend_store_uri }],
    )
    # ECS `secrets`: at task start the execution role reads the ARN and injects
    # the value as the env var MLFLOW_BACKEND_STORE_URI.
    secrets = local.uses_secret ? [{ name = "MLFLOW_BACKEND_STORE_URI", valueFrom = var.backend_store_secret_arn }] : []
    healthCheck = {
      # MLflow serves its health endpoint under the static prefix too, so this
      # must carry it — a bare /health 404s and the task never turns healthy.
      command     = ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://localhost:${var.port}${var.static_prefix}/health')\" || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group_name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = local.name
      }
    }
  }
}

# ECS task definition (Fargate + awsvpc = each task gets its own ENI and IP).
# [TF] jsonencode() turns the HCL list into the JSON string that
#      container_definitions expects. merge() combines maps; later keys win, so
#      the Name tag is added on top of var.tags.
# Referenced by: aws_ecs_service.this.task_definition.
resource "aws_ecs_task_definition" "this" {
  family                   = "${var.name_prefix}-${local.name}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn
  container_definitions    = jsonencode([local.container])
  tags                     = merge(var.tags, { Name = "${var.name_prefix}-${local.name}" })
}

# Always in Cloud Map: in-VPC consumers address it by name, and that is the only
# route at all in the default (internal) posture.
# Cloud Map service "mlflow" in the careroute.local namespace: an A record per
# running task IP (MULTIVALUE, 10s TTL). health_check_custom_config means ECS,
# not Route 53, reports task health. Referenced by: service_registries below.
resource "aws_service_discovery_service" "this" {
  name = local.name

  dns_config {
    namespace_id   = var.service_discovery_namespace_id
    routing_policy = "MULTIVALUE"
    dns_records {
      ttl  = 10
      type = "A"
    }
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

# The ECS service: keeps desired_count = 1 task running.
resource "aws_ecs_service" "this" {
  name            = local.name
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = 1

  # [TF] null means "argument omitted". With Spot, launch_type must be unset
  #      because a capacity_provider_strategy is used instead.
  launch_type = var.use_fargate_spot ? null : "FARGATE"

  # [TF] A `dynamic "NAME"` block generates nested NAME blocks: one per element
  #      of for_each, with `content` as the body. for_each = cond ? [1] : [] is
  #      the idiom for "emit this block 0 or 1 times".
  dynamic "capacity_provider_strategy" {
    for_each = var.use_fargate_spot ? [1] : []
    content {
      capacity_provider = "FARGATE_SPOT"
      weight            = 1
    }
  }

  health_check_grace_period_seconds = var.target_group_arn == null ? null : 90

  # Failed deployment (tasks never become healthy) is stopped and rolled back to
  # the previous task definition revision.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = var.assign_public_ip
  }

  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [1]
    content {
      target_group_arn = var.target_group_arn
      container_name   = local.name
      container_port   = var.port
    }
  }

  # Links the service to the Cloud Map service above, so tasks auto-register.
  service_registries {
    registry_arn = aws_service_discovery_service.this.arn
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-${local.name}" })
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# [TF] `output` = values returned to the caller as module.mlflow[0].<name>.
# internal_tracking_uri -> careroute_stack output `mlflow_tracking_uri`, e.g.
#   http://mlflow.careroute.local:5000/mlflow. artifact_root and service_name
#   are not consumed by the stack.
output "internal_tracking_uri" {
  description = "MLFLOW_TRACKING_URI for anything inside the VPC."
  value       = "http://${local.name}.${var.namespace_name}:${var.port}${var.static_prefix}"
}

output "artifact_root" { value = local.artifact_root }
output "service_name" { value = aws_ecs_service.this.name }
