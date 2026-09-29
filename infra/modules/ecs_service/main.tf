###############################################################################
# Module: ecs_service  (reusable — one instance per agent/service)
# -----------------------------------------------------------------------------
# Turns a container image into a running Fargate service. The whole CareRoute
# stack is built by calling this module repeatedly:
#
#   * frontend        -> ALB frontend target group, port 3000 (Next.js server)
#   * backend         -> ALB backend target group, port 8000 (Symptom-Intake,
#                        which is ALSO the orchestrator — see app agents/orchestration.py)
#   * sub-agent tier  -> internal topology placeholder, Cloud Map DNS (off by default)
#
# Optional bits are all driven by flags so one module covers every case:
#   - target_group_arn        attach to an ALB (public-facing services)
#   - enable_service_discovery register a private DNS name (internal services)
#   - enable_autoscaling      target-tracking autoscaling on CPU, and optionally
#                             memory and ALB requests per task (incl. scale-to-zero)
#   - on_demand_base          an on-demand floor under FARGATE_SPOT burst capacity
#   - sidecar_containers      extra containers in the task (ADOT metrics collector)
###############################################################################

# --------------------------------------------------------------------------- #
# WHO CALLS THIS MODULE — it is instantiated six+ times, once per service:
#   modules/careroute_stack/main.tf : module "backend", module "frontend",
#                                     module "sub_agents" (for_each: one per name
#                                     in var.sub_agents, only if enable_subagent_tier)
#   modules/monitoring_stack/main.tf: module "prometheus", "alertmanager", "grafana"
# Each call gets its own copy of every resource below. "Set by" notes list what
# each caller passes; an input a caller omits falls back to its `default`.
# [TF] `variable "x" { type = string }` with no default = REQUIRED input.
# --------------------------------------------------------------------------- #

# Service / container / task-definition family / log-stream-prefix / Cloud Map name.
# Set by: "backend", "frontend", each.value for sub_agents (e.g.
#         "severity-classifier"), "prometheus" / "alertmanager" / "grafana".
# ---- Identity / placement ---- #
variable "name" { type = string }
# ECS cluster to run in. Set by: module.ecs_cluster.cluster_id (careroute_stack),
# or var.cluster_id in monitoring_stack (itself module.ecs_cluster.cluster_id).
# Used in: aws_ecs_service.this.cluster.
variable "cluster_id" { type = string }
# Cluster NAME (not id) — Application Auto Scaling identifies a service as
# "service/<cluster>/<service>". Set by: module.ecs_cluster.cluster_name.
# Used in: aws_appautoscaling_target.this.resource_id.
variable "cluster_name" { type = string }
# AWS region for the awslogs driver. Set by: var.region (root.hcl <- env.hcl aws_region).
variable "region" { type = string }
# Subnets the task ENIs land in. Set by: local.task_subnet_ids = public subnets
# when var.public_networking (demo) else private subnets (module.networking).
# Used in: aws_ecs_service.this.network_configuration.subnets.
variable "subnet_ids" { type = list(string) }
# Set by: [module.security.ecs_sg_id] for every caller. Used in: network_configuration.
variable "security_group_ids" { type = list(string) }
# Give each task a public IP (needed to reach ECR/internet from a public subnet
# with no NAT). Set by: local.assign_public_ip = var.public_networking (demo: true).
variable "assign_public_ip" {
  type    = bool
  default = false
}
# Set by: var.use_fargate_spot (live/demo: true, live/staging: false) for every caller.
# Used in: aws_ecs_service.this launch_type / capacity_provider_strategy.
variable "use_fargate_spot" {
  description = "Run on FARGATE_SPOT (~70%% cheaper, interruptible) instead of on-demand FARGATE."
  type        = bool
  default     = false
}

# ---- IAM ---- #
# Role ECS itself uses to pull the image, write logs and resolve `secrets`.
# Set by: module.ecs_cluster.execution_role_arn. Used in: task definition execution_role_arn.
variable "execution_role_arn" { type = string }
# Role the app code runs as. Set by: module.ecs_cluster.task_role_arn.
# Used in: task definition task_role_arn.
variable "task_role_arn" { type = string }

# ---- Container ---- #
# Full image URI. Set by: "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
# for backend + sub_agents, repository_urls["frontend"] for frontend (image_tag =
# IMAGE_TAG env var in live/<env>/terragrunt.hcl); var.*_image for monitoring.
# Used in: local.base_container.image.
variable "image" { type = string }
# Set by: 8000 (backend, sub_agents), 3000 (frontend, grafana), 9090 (prometheus),
# 9093 (alertmanager). Used in: portMappings and the load_balancer block.
variable "container_port" {
  description = "Port the container listens on. Use 0 for a task with no inbound port."
  type        = number
  default     = 0
}
# Fargate task size. cpu is in CPU UNITS (1024 = 1 vCPU); memory in MiB. Only
# certain cpu/memory pairs are valid on Fargate (e.g. 256/512, 512/1024).
# Set by: var.backend_cpu (demo 512) / var.frontend_cpu (demo 256) / 256 for
# sub_agents / var.cpu in monitoring_stack (= var.monitoring_cpu, default 256).
# Used in: aws_ecs_task_definition.this.cpu.
variable "cpu" {
  type    = number
  default = 256
}
# Set by: var.backend_memory (demo 1024) / var.frontend_memory (demo 512) / 512
# for sub_agents / monitoring_stack var.memory (= var.monitoring_memory).
# Used in: aws_ecs_task_definition.this.memory.
variable "memory" {
  type    = number
  default = 512
}
# Plain (non-secret) env vars, visible in the task definition in the console.
# Set by: backend -> merge(local.llm_env, local.safety_env, ... DB_*),
#         frontend -> { PORT, HOSTNAME }, sub_agents -> merge(... AGENT_ROLE),
#         monitoring -> config files as env vars (PROMETHEUS_CONFIG etc).
# Used in: local.base_container.environment.
variable "environment" {
  description = "Plain env vars injected into the container."
  type        = map(string)
  default     = {}
}
# Secret env vars: ECS fetches the value at task start using the EXECUTION role,
# so the plaintext never appears in the task definition or Terraform config.
# Set by: backend -> merge(DB_PASSWORD when RDS, local.llm_secrets, onemap, onyx);
#         sub_agents -> the same minus DB; grafana -> GF_SECURITY_ADMIN_PASSWORD.
# Used in: local.base_container.secrets.
variable "secrets" {
  description = "Secret env vars: name => Secrets Manager/SSM valueFrom ARN."
  type        = map(string)
  default     = {}
}
# Overrides the image CMD. Set by: the three monitoring_stack services only
# (a shell script that writes the config file then execs the binary). Others: null.
# [TF] default = null means "argument not set" — jsonencode writes it as null,
# which ECS treats as "use the image's own CMD".
variable "command" {
  description = "Optional container command override."
  type        = list(string)
  default     = null
}

# Overrides the image ENTRYPOINT. Set by: monitoring_stack services (["/bin/sh","-c"]).
# Used in: local.base_container.entryPoint.
variable "entry_point" {
  description = <<-EOT
    Optional container entryPoint override.

    Used by the monitoring stack to drop a config file into the container at
    boot: Prometheus, Alertmanager and Grafana all need one, and on Fargate
    there is no bind mount to supply it. Overriding the entryPoint to a shell
    that writes the file from an environment variable and then execs the real
    binary avoids both EFS (an always-on cost for three throwaway containers)
    and custom images (a second build pipeline for config that changes more
    often than code). `command` alone cannot do this: it replaces CMD, not
    ENTRYPOINT, so the image's own binary would still run first.
  EOT
  type        = list(string)
  default     = null
}
# Set by: backend only (python urllib call to /api/health). Others: null -> no healthCheck key.
# Used in: local.container (adds the healthCheck key when set).
variable "health_check_command" {
  description = "Optional container-level health check (CMD-SHELL string)."
  type        = string
  default     = null
}

# Set by: backend only -> concat(local.otel_sidecar, local.telemetry_sidecar):
# the ADOT collector and the telemetry shipper, each only when its feature is on.
# Used in: container_definitions (appended after the main container).
variable "sidecar_containers" {
  description = <<-EOT
    Extra container definitions to place in the SAME task as the app container.
    Used for the ADOT collector that scrapes the backend's Prometheus /metrics
    endpoint: containers in one Fargate task share a network namespace, so the
    sidecar reaches the app at 127.0.0.1:<port> with no service discovery, no
    security-group rule and no extra task.

    Each entry is a raw ECS container definition map. They are marked
    non-essential by the caller so a collector crash cannot kill the triage API.
  EOT
  type        = list(any)
  default     = []
}

# ---- Logging ---- #
# CloudWatch Logs group for the awslogs driver. Set by: module.ecs_cluster.log_group_name.
variable "log_group_name" { type = string }

# ---- Scaling ---- #
# How many tasks ECS keeps running. Set by: var.backend_desired_count (demo 1),
# var.frontend_desired_count (demo 1); others use the default 1.
# Used in: aws_ecs_service.this.desired_count (see the ignore_changes note there).
variable "desired_count" {
  type    = number
  default = 1
}
# Set by: backend -> var.enable_autoscaling (demo/staging false; capped at one
# task by the single-task guard), frontend -> var.frontend_enable_autoscaling
# (staging true). Used in: count on every aws_appautoscaling_* resource.
variable "enable_autoscaling" {
  type    = bool
  default = false
}
# Set by: backend -> var.backend_desired_count, frontend -> var.frontend_desired_count.
# Used in: autoscaling target min.
variable "min_capacity" {
  type    = number
  default = 1
}
# Set by: backend -> var.backend_max_count (default 1), frontend ->
# var.frontend_max_count (default 4). Used in: autoscaling target max.
variable "max_capacity" {
  type    = number
  default = 3
}
# Set by: backend / frontend -> var.autoscaling_cpu_target (default 60).
# Used in: aws_appautoscaling_policy.cpu target_value.
variable "cpu_target" {
  description = "Target average CPU %% for target-tracking autoscaling."
  type        = number
  default     = 60
}
# Set by: backend / frontend -> var.autoscaling_memory_target (default null = off).
# Used in: count on aws_appautoscaling_policy.memory.
variable "memory_target" {
  description = "Target average memory %% for target-tracking autoscaling. null = no memory policy."
  type        = number
  default     = null
}
# Set by: backend -> var.backend_requests_per_target, frontend ->
# var.frontend_requests_per_target (default null = off).
# Used in: count on aws_appautoscaling_policy.requests.
variable "requests_per_target" {
  description = <<-EOT
    Target ALB requests per task per minute (ALBRequestCountPerTarget). null = no
    request policy. Needs target_group_arn and alb_resource_label.

    Why a second signal at all: the backend spends most of a triage run WAITING
    on the hosted LLM, so CPU stays low while requests queue. CPU alone scales
    out late, if ever, for an I/O-bound service.
  EOT
  type        = number
  default     = null
}
# "app/<alb-name>/<id>/targetgroup/<tg-name>/<id>" — the label
# ALBRequestCountPerTarget is keyed on. Set by: careroute_stack from
# module.alb.alb_arn_suffix + module.alb.<svc>_target_group_arn_suffix.
variable "alb_resource_label" {
  type    = string
  default = null
}
# Set by: default only. Used in: every aws_appautoscaling_policy below.
variable "scale_in_cooldown" {
  description = "Seconds after a scale-in before another scale-in. Longer than scale-out on purpose: a task removed mid-stream cuts that task's open SSE triage runs, so shrinking should be reluctant."
  type        = number
  default     = 300
}
variable "scale_out_cooldown" {
  type    = number
  default = 60
}

# Set by: var.fargate_on_demand_base (default 0) for backend and frontend.
# Used in: aws_ecs_service.this capacity_provider_strategy (Spot only).
variable "on_demand_base" {
  description = <<-EOT
    With use_fargate_spot, keep this many tasks on on-demand FARGATE and put
    every task above it on FARGATE_SPOT. 0 = all Spot (the demo's shape).

    This is the production shape for a scaled service: a floor that a Spot
    reclaim cannot take away, and cheap burst capacity on top of it.
  EOT
  type        = number
  default     = 0
}

# ---- ALB attachment (optional) ---- #
# ALB target group to register tasks in. Set by: module.alb.backend_target_group_arn,
# module.alb.frontend_target_group_arn, and module.alb.grafana_target_group_arn (via
# monitoring_stack). sub_agents / prometheus / alertmanager leave it null (no ALB).
# Used in: the load_balancer block + health_check_grace_period_seconds.
variable "target_group_arn" {
  type    = string
  default = null
}

# ---- Service discovery (optional) ---- #
# Set by: backend -> local.backend_service_discovery (= enable_prometheus_stack, so
# Prometheus can scrape backend.<project>.local); sub_agents, prometheus and
# alertmanager -> true. Used in: count on the Cloud Map service + service_registries.
variable "enable_service_discovery" {
  type    = bool
  default = false
}
# The Cloud Map private DNS namespace ("<project>.local") to register into.
# Set by: aws_service_discovery_private_dns_namespace.this[0].id in careroute_stack.
variable "service_discovery_namespace_id" {
  type    = string
  default = null
}

# Set by: backend -> [local.telemetry_volume] ("ml-telemetry") only when telemetry
# is enabled. Used in: the dynamic "volume" block of the task definition.
variable "volumes" {
  description = <<-EOT
    Task-scoped volume names. On Fargate a volume with no configuration block is
    ephemeral scratch shared by every container in the task — which is exactly
    what is needed to hand files from the app container to a sidecar without EFS.

    Ephemeral is the right word and the limitation: the volume dies with the
    task. It is a HANDOFF channel, not storage. Anything that must outlive the
    task has to leave it, which is what the telemetry shipper sidecar exists to
    do.
  EOT
  type        = list(string)
  default     = []
}

# Set by: backend -> local.service_telemetry_mounts. Used in: base_container.mountPoints.
variable "mount_points" {
  description = "Mount points for the MAIN container, as raw ECS mountPoint maps ({ sourceVolume, containerPath, readOnly }). Sidecars carry their own inside sidecar_containers."
  type        = list(any)
  default     = []
}

# ---- Deployment safety ---- #
# Set by: nobody — always the default (true) for every instance.
# Used in: the dynamic "deployment_circuit_breaker" block.
variable "enable_deployment_rollback" {
  description = <<-EOT
    ECS deployment circuit breaker with automatic rollback.

    Without it, a task that crash-loops on boot — a bad image, a missing secret,
    an OOM during the severity-model warmup — leaves ECS relaunching it forever
    while the old revision drains, and `terragrunt apply` has already returned
    green. The breaker watches consecutive task-launch failures, stops the
    rollout, and puts the LAST WORKING task definition back.

    Leave it on. The one case for turning it off is a service with no healthy
    predecessor to roll back to (a first deploy that is expected to fail while
    you debug it), where the breaker only adds a rollback that cannot happen.
  EOT
  type        = bool
  default     = true
}

# Set by: careroute_stack -> backend / frontend only (local.backend_rollback_alarms,
# local.frontend_rollback_alarms, defined in careroute_stack/deploy_safety.tf).
# Every other caller leaves it empty, so no alarms block is emitted for them.
# Used in: the dynamic "alarms" block of aws_ecs_service.this.
variable "rollback_alarm_names" {
  description = <<-EOT
    CloudWatch alarms ECS watches while a deployment is in flight AND for a bake
    period after it; if any of them enters ALARM, ECS rolls the service back to
    the previous task definition.

    This is the other half of automatic rollback. The circuit breaker above only
    notices tasks that fail to START. A release that boots, passes /api/health
    and then answers real requests with 5xx is invisible to it; an error-rate
    alarm is what catches that. Empty (the default) = no alarm-driven rollback.
  EOT
  type        = list(string)
  default     = []
}

# Set by: local.common_tags (careroute_stack) / var.tags (monitoring_stack).
variable "tags" {
  type    = map(string)
  default = {}
}

# --------------------------------------------------------------------------- #
# Container definition (built as HCL, serialised to JSON for ECS)
# --------------------------------------------------------------------------- #
# [TF] `locals { }` defines named intermediate values, read as local.<name>.
# Here they build the container definition as an HCL object; jsonencode() in the
# task definition turns it into the JSON array ECS expects.
locals {
  base_container = {
    name       = var.name
    image      = var.image
    essential  = true
    command    = var.command
    entryPoint = var.entry_point
    # No port mapping for container_port = 0. In awsvpc mode containerPort is
    # the port on the task's own ENI (no hostPort remapping).
    portMappings = var.container_port == 0 ? [] : [{ containerPort = var.container_port, protocol = "tcp" }]
    mountPoints  = var.mount_points
    # [TF] `for` expression: [for k, v in MAP : EXPR] loops a map and builds a
    # list. ECS wants [{name, value}, ...], but callers pass a simple map.
    environment = [for k, v in var.environment : { name = k, value = v }]
    # `valueFrom` = the Secrets Manager secret ARN (or ARN:json-key::) that ECS
    # resolves at launch, vs `value` above which is stored literally.
    secrets = [for k, v in var.secrets : { name = k, valueFrom = v }]
    # awslogs driver: stdout/stderr -> CloudWatch Logs group, stream named
    # "<name>/<container>/<task-id>".
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group_name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = var.name
      }
    }
  }

  # Only add the healthCheck key when a command was supplied (ECS rejects null).
  # healthCheck is ECS's in-container check (Docker HEALTHCHECK), separate from
  # the ALB target-group health check. startPeriod 60 s = failures ignored while booting.
  # [TF] A `for` expression with an `if` filter, NOT a ternary: both arms of
  #      `cond ? a : b` must have the same object type, and "container" vs
  #      "container + healthCheck" do not, so terraform fails the plan with
  #      "Inconsistent conditional result types" for every caller that passes no
  #      command. The filter yields {} or { healthCheck = ... } with no such rule.
  health_check = { for k, v in {
    healthCheck = {
      command     = ["CMD-SHELL", var.health_check_command]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  } : k => v if var.health_check_command != null }

  # [TF] merge() combines maps/objects; keys in later arguments win.
  container = merge(local.base_container, local.health_check)
}

# ECS Task Definition = the container "recipe": image(s), CPU/memory, env,
# secrets, logs, roles. Every change registers a new REVISION of the family.
# Referenced by: aws_ecs_service.this.task_definition; output task_definition_arn.
resource "aws_ecs_task_definition" "this" {
  family = var.name
  # Fargate-only task definition (not EC2-launch compatible).
  requires_compatibilities = ["FARGATE"]
  # awsvpc = each task gets its own ENI + private IP in your subnet, so security
  # groups apply per task. Required on Fargate.
  network_mode       = "awsvpc"
  cpu                = var.cpu
  memory             = var.memory
  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.task_role_arn
  # [TF] jsonencode() converts an HCL value to a JSON string. concat() joins
  # lists: [main container] + any sidecars -> one containerDefinitions array.
  container_definitions = jsonencode(concat([local.container], var.sidecar_containers))

  # [TF] A `dynamic "volume"` block generates zero or more nested `volume {}`
  # blocks, one per element of for_each; inside, volume.value is the element.
  # toset() turns the list into a set (for_each needs a set or map).
  dynamic "volume" {
    for_each = toset(var.volumes)
    content {
      name = volume.value
    }
  }

  tags = merge(var.tags, { Name = var.name })
}

# --------------------------------------------------------------------------- #
# Private DNS name for internal (non-ALB) services — Cloud Map.
# backend reaches e.g. severity-classifier.careroute.local via this.
# --------------------------------------------------------------------------- #
# Cloud Map service = a DNS name "<name>.<namespace>" (e.g. prometheus.careroute.local)
# whose A records ECS keeps updated with the task IPs.
# [TF] `count = cond ? 1 : 0` is the "optional resource" idiom: 0 copies = not
# created. A counted resource is a LIST, so it is referenced as ...this[0].
resource "aws_service_discovery_service" "this" {
  count = var.enable_service_discovery ? 1 : 0
  name  = var.name

  dns_config {
    namespace_id = var.service_discovery_namespace_id
    # MULTIVALUE: return every healthy task's IP (simple client-side spreading).
    routing_policy = "MULTIVALUE"
    dns_records {
      ttl  = 10
      type = "A"
    }
  }

  # Health comes from ECS (task running/healthy), not Route 53 health checks.
  health_check_custom_config {
    failure_threshold = 1
  }
}

# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #
# ECS Service = keeps desired_count copies of the task running, replaces failed
# ones, rolls out new task-definition revisions, and registers tasks with the
# ALB target group / Cloud Map. Output service_name -> module.backend.service_name
# (module observability alarms + careroute_stack outputs.tf).
resource "aws_ecs_service" "this" {
  name    = var.name
  cluster = var.cluster_id
  # .arn includes the revision number, so a new revision = a rolling deployment.
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count

  # Compute placement: FARGATE_SPOT is ~70% cheaper but interruptible (fine for
  # stateless agents / staging). launch_type and capacity_provider_strategy are
  # mutually exclusive, so we set exactly one based on the toggle.
  launch_type = var.use_fargate_spot ? null : "FARGATE"

  # With Spot on: an optional on-demand FARGATE floor (`base` tasks, weight 0 so
  # it never takes more), then FARGATE_SPOT for everything above it. With
  # on_demand_base = 0 this is the single FARGATE_SPOT block it always was.
  # [TF] for_each = cond ? [1] : [] is the dynamic-block version of the
  # optional-resource idiom: one block or none.
  dynamic "capacity_provider_strategy" {
    for_each = var.use_fargate_spot && var.on_demand_base > 0 ? [1] : []
    content {
      capacity_provider = "FARGATE"
      base              = var.on_demand_base
      weight            = 0
    }
  }
  dynamic "capacity_provider_strategy" {
    for_each = var.use_fargate_spot ? [1] : []
    content {
      capacity_provider = "FARGATE_SPOT"
      weight            = 1
    }
  }

  # Give ALB-fronted tasks time to boot before health checks count against them.
  # Grace period only makes sense with an ALB (null = argument omitted).
  health_check_grace_period_seconds = var.target_group_arn == null ? null : 60

  # Automatic rollback to the last working task definition when a rollout fails.
  # This is the "automatic rollback based on monitoring thresholds" half of
  # progressive delivery that the deployed stack was missing entirely — see
  # docs/vault/Lecture Alignment.md §3. Costs nothing.
  #
  # Worth knowing with FARGATE_SPOT: a Spot reclaim DURING a deployment counts
  # as a failed task launch, so a very unlucky rollout can trip the breaker and
  # revert to a revision identical in everything but the image tag. That is the
  # safe direction to fail, and it is loud (a rolled-back deployment event)
  # rather than silent (a service stuck relaunching).
  dynamic "deployment_circuit_breaker" {
    for_each = var.enable_deployment_rollback ? [1] : []
    content {
      enable   = true
      rollback = true
    }
  }

  # Metric-driven rollback: the circuit breaker catches a release that cannot
  # start, these alarms catch one that starts and then serves errors. ECS keeps
  # watching them for a bake period after the new tasks are healthy, so the
  # rollback still fires if the errors only begin once real traffic arrives.
  # A native blue/green or canary strategy (deployment_configuration.strategy)
  # would keep the old tasks warm through that window; it needs AWS provider
  # 6.x, and this stack is pinned to 5.x (careroute_stack/versions.tf).
  dynamic "alarms" {
    for_each = length(var.rollback_alarm_names) > 0 ? [1] : []
    content {
      enable      = true
      rollback    = true
      alarm_names = var.rollback_alarm_names
    }
  }

  # Required with awsvpc: which subnets/SGs the task ENIs use.
  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = var.assign_public_ip
  }

  # Registers each task's IP:container_port in the ALB target group. container_name
  # must match a container in the task definition (the main one is named var.name).
  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [1]
    content {
      target_group_arn = var.target_group_arn
      container_name   = var.name
      container_port   = var.container_port
    }
  }

  # Links the service to the Cloud Map service above so tasks self-register in DNS.
  dynamic "service_registries" {
    for_each = var.enable_service_discovery ? [1] : []
    content {
      registry_arn = aws_service_discovery_service.this[0].arn
    }
  }

  # When autoscaling owns desired_count, stop Terraform fighting it on every apply.
  # [TF] lifecycle { ignore_changes = [...] } tells Terraform not to "correct"
  # drift on those attributes after creation. Note it applies even when
  # autoscaling is OFF, so changing desired_count later is ignored by apply.
  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = merge(var.tags, { Name = var.name })
}

# --------------------------------------------------------------------------- #
# Autoscaling (optional). min_capacity = 0 gives the "scale-to-zero" behaviour
# named in the proposal's deploy stage.
# --------------------------------------------------------------------------- #
# Application Auto Scaling: registers the ECS service's DesiredCount as a
# scalable target with min/max bounds. Only when var.enable_autoscaling.
resource "aws_appautoscaling_target" "this" {
  count              = var.enable_autoscaling ? 1 : 0
  max_capacity       = var.max_capacity
  min_capacity       = var.min_capacity
  resource_id        = "service/${var.cluster_name}/${aws_ecs_service.this.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# Target-tracking policies. Several may be attached to one target: Application
# Auto Scaling scales OUT when any one of them asks and scales IN only when all
# of them agree, so adding a signal can only make the service more eager to grow
# and more careful to shrink. Cooldowns (s) damp flapping.

# Hold average service CPU near var.cpu_target (60%).
resource "aws_appautoscaling_policy" "cpu" {
  count              = var.enable_autoscaling ? 1 : 0
  name               = "${var.name}-cpu-target-tracking"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.this[0].resource_id
  scalable_dimension = aws_appautoscaling_target.this[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.this[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = var.cpu_target
    scale_in_cooldown  = var.scale_in_cooldown
    scale_out_cooldown = var.scale_out_cooldown
  }
}

# Hold average service memory near var.memory_target. Optional.
resource "aws_appautoscaling_policy" "memory" {
  count              = var.enable_autoscaling && var.memory_target != null ? 1 : 0
  name               = "${var.name}-memory-target-tracking"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.this[0].resource_id
  scalable_dimension = aws_appautoscaling_target.this[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.this[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
    target_value       = var.memory_target
    scale_in_cooldown  = var.scale_in_cooldown
    scale_out_cooldown = var.scale_out_cooldown
  }
}

# Hold ALB requests per task per minute near var.requests_per_target. Optional;
# only meaningful for an ALB-fronted service.
resource "aws_appautoscaling_policy" "requests" {
  count              = var.enable_autoscaling && var.requests_per_target != null && var.alb_resource_label != null ? 1 : 0
  name               = "${var.name}-requests-target-tracking"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.this[0].resource_id
  scalable_dimension = aws_appautoscaling_target.this[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.this[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = var.alb_resource_label
    }
    target_value       = var.requests_per_target
    scale_in_cooldown  = var.scale_in_cooldown
    scale_out_cooldown = var.scale_out_cooldown
  }
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# Consumers: service_name -> module.backend.service_name (module observability's
# backend_service_name; careroute_stack outputs.tf) and
# module.grafana.service_name (monitoring_stack output grafana_service_name).
output "service_name" { value = aws_ecs_service.this.name }
# task_definition_arn: not read by any caller today (available for CI/debugging).
output "task_definition_arn" { value = aws_ecs_task_definition.this.arn }
