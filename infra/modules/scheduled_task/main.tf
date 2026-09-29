###############################################################################
# Module: scheduled_task
# -----------------------------------------------------------------------------
# An EventBridge Scheduler rule that runs a one-shot ECS Fargate task on a cron,
# on the cluster that is already there.
#
# WHY THIS EXISTS. Platform p15 says "schedule model retraining via serverless
# such as AWS Step Functions"; Platform p22 says "schedule heavy jobs (e.g.
# retraining) in off-peak hours"; Serving p31 and MonitoringWithSageMaker p17
# both build an HOURLY monitoring schedule as the worked example of the whole
# module. Before this module the repo had no EventBridge, Lambda, Step Functions
# or Scheduler resource of any kind, so the app's drift->retrain loop existed
# only as a CI job and never fired in the cloud at all.
#
# WHY NOT STEP FUNCTIONS, WHICH IS WHAT THE SLIDE NAMES. A state machine earns
# its keep when there are several steps to sequence, branch between and
# compensate for — the Saga pattern on Platform p18-21. This is one container
# running one command. Step Functions would add a second service, a second IAM
# role and a state-machine definition in order to express a single step, and
# would still call ecs:RunTask underneath. The moment the loop grows a second
# step (monitor -> retrain -> gate -> register), that trade flips; the note in
# docs/vault says so rather than pretending this is the final shape.
#
# COST. Nothing is always-on. The schedule itself is free at this volume (the
# first 14M EventBridge Scheduler invocations a month are), and the task bills
# per second while it runs — a drift report is minutes, so a daily schedule is
# cents a month on Spot.
###############################################################################

# --------------------------------------------------------------------------- #
# Inputs. Only caller: `module "drift_monitor"` in modules/careroute_stack/main.tf,
# created only when enable_scheduled_monitor && enable_artifacts_bucket (both
# true in live/staging/terragrunt.hcl; off in live/demo).
# [TF] Because that module block has `count`, its outputs are read as
# module.drift_monitor[0].<output>.
# --------------------------------------------------------------------------- #

# Set by: "drift-monitor". Used in: local.family, container name, log stream prefix.
variable "name" {
  description = "Job name; used for the task family, role and schedule names."
  type        = string
}

# Set by: local.name_prefix ("careroute-<env>"). Used in: local.family.
variable "name_prefix" { type = string }
# Set by: var.region. Used in: awslogs-region of the main container.
variable "region" { type = string }

# Cluster ARN (not id): EventBridge Scheduler's ECS target is the cluster ARN.
# Set by: module.ecs_cluster.cluster_arn. Used in: schedule target.arn and the
# ecs:cluster condition on the RunTask permission.
variable "cluster_arn" { type = string }
# Set by: module.ecs_cluster.execution_role_arn / task_role_arn (the SAME shared
# roles the services use). Used in: task definition roles + iam:PassRole resources.
variable "execution_role_arn" { type = string }
variable "task_role_arn" { type = string }
# Set by: module.ecs_cluster.log_group_name. Used in: awslogs-group of the main container.
variable "log_group_name" { type = string }

# Set by: "${module.ecr.repository_urls["backend"]}:${var.image_tag}".
variable "image" {
  description = "Image to run. Normally the SAME backend image the service runs, so the job executes the code that was actually released."
  type        = string
}

# Set by: ["python", "-m", "app.ml.monitor"]. Used in: local.wrapped_command.
variable "command" {
  description = "Container command, e.g. [\"python\",\"-m\",\"app.ml.monitor\"]."
  type        = list(string)
}

# Set by: merge(local.safety_env, local.telemetry_path_env) — deliberately no LLM key.
# Used in: main container environment.
variable "environment" {
  type    = map(string)
  default = {}
}

# Set by: nobody — default {} (the drift job needs no secrets).
variable "secrets" {
  type    = map(string)
  default = {}
}

# Fargate size in CPU units / MiB. Set by: var.monitor_task_cpu (512) and
# var.monitor_task_memory (1024) — stack defaults, not overridden in live/.
variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

# Set by: local.task_subnet_ids / [module.security.ecs_sg_id]. Used in: target
# ecs_parameters.network_configuration.
variable "subnet_ids" { type = list(string) }
variable "security_group_ids" { type = list(string) }

# Set by: local.assign_public_ip (= var.public_networking).
variable "assign_public_ip" {
  type    = bool
  default = false
}

# Set by: var.monitor_schedule_expression (stack default "cron(0 3 * * ? *)" =
# 03:00 daily). Used in: aws_scheduler_schedule.this.schedule_expression.
variable "schedule_expression" {
  description = <<-EOT
    EventBridge Scheduler expression. `rate(1 day)`, or a cron — note Scheduler
    uses the SIX-field `cron(min hour day month weekday year)` form, so the
    hourly example from the SageMaker deck is `cron(0 * * * ? *)`.
  EOT
  type        = string
  default     = "rate(1 day)"
}

# Set by: var.monitor_schedule_timezone (stack default "Asia/Singapore").
variable "schedule_timezone" {
  description = "IANA timezone the expression is evaluated in. Set it explicitly: 'off-peak' is a local-time idea, and the default of UTC makes an off-peak window land in the middle of the Singapore working day."
  type        = string
  default     = "Asia/Singapore"
}

# Set by: nobody — always true (ENABLED). Used in: aws_scheduler_schedule.this.state.
variable "enabled" {
  type    = bool
  default = true
}

# Set by: var.use_fargate_spot (demo true, staging false). Used in: the schedule's
# ecs_parameters launch_type / capacity_provider_strategy.
variable "use_fargate_spot" {
  type    = bool
  default = true
}

# Set by: nobody — default 60. Used in: local.wrapped_command (timeout seconds).
variable "task_timeout_minutes" {
  description = "Upper bound on a single run, enforced by the container command wrapper rather than by ECS (which has no task-level timeout). Mirrors DefaultModelMonitor's max_runtime_in_seconds."
  type        = number
  default     = 60
}

# Set by: [local.telemetry_volume] ("ml-telemetry"). Used in: dynamic "volume".
variable "volumes" {
  description = "Task-scoped ephemeral volume names, shared by every container in the task."
  type        = list(string)
  default     = []
}

# Set by: local.telemetry_mounts (the volume at local.telemetry_path "/telemetry").
variable "mount_points" {
  description = "Mount points for the MAIN container (raw ECS mountPoint maps)."
  type        = list(any)
  default     = []
}

# Set by: one "telemetry-restore" container (var.aws_cli_image) that runs
# `aws s3 sync` from the artifacts bucket INTO the shared volume.
# Used in: container_definitions (listed first) + the main container's dependsOn.
variable "init_containers" {
  description = <<-EOT
    Containers that must run to completion BEFORE the main one starts. The main
    container is given `dependsOn: SUCCESS` on each, so a failed restore stops
    the job instead of letting it run against an empty directory.

    That distinction is the whole reason this exists: a drift monitor that
    starts with no inference log does not fail, it reports NO DRIFT — which is
    indistinguishable from a healthy model and is the one wrong answer nobody
    investigates.
  EOT
  type        = list(any)
  default     = []
}

# Set by: one "telemetry-archive" container that `aws s3 sync`s the report back
# to s3://<artifacts bucket>/ml-telemetry/scheduled/.
variable "finalizer_containers" {
  description = "Containers that run AFTER the main one exits 0, given `dependsOn: SUCCESS` on it. Used to archive whatever the job produced before the task's storage disappears with it."
  type        = list(any)
  default     = []
}

# Set by: local.common_tags.
variable "tags" {
  type    = map(string)
  default = {}
}

# [TF] locals: computed values reused below, read as local.family etc.
locals {
  family = "${var.name_prefix}-${var.name}"

  # ECS has no native per-task timeout, so the bound is applied where it can be:
  # around the command itself. Without it a hung monitoring run holds Fargate
  # capacity until someone notices, and the next scheduled run stacks on top.
  # [TF] format() is printf-style; "%q" quotes each arg. [for c in LIST : EXPR] maps
  # a list -> list; join(" ", ...) glues it into one string. Result, e.g.:
  #   ["/bin/sh","-c","timeout -s TERM 3600 \"python\" \"-m\" \"app.ml.monitor\""]
  wrapped_command = ["/bin/sh", "-c", format(
    "timeout -s TERM %d %s",
    var.task_timeout_minutes * 60,
    join(" ", [for c in var.command : format("%q", c)]),
  )]
}

# --------------------------------------------------------------------------- #
# The task definition. Separate from the service's, because it runs a different
# command with different sizing — but the SAME image, so the scheduled job and
# the serving path can never drift to different model code.
# --------------------------------------------------------------------------- #
# ECS Task Definition for the job (a one-off task, NOT a service: it runs, exits,
# and is gone). Referenced by: the scheduler's RunTask permission + schedule target.
resource "aws_ecs_task_definition" "this" {
  family = local.family
  # Fargate-only; awsvpc = own ENI per task (required on Fargate). cpu in CPU units, memory in MiB.
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  # [TF] jsonencode(concat(A, B, C)) = one JSON array of container definitions:
  #   A = init containers, B = [the main container], C = finalizers.
  # merge() below adds a `dependsOn` key to the main container only when there
  # are init containers (ternary: empty map {} contributes nothing).
  container_definitions = jsonencode(concat(
    var.init_containers,
    [merge(
      {
        name  = var.name
        image = var.image
        # ECS rejects an essential container that another container depends on
        # with SUCCESS/COMPLETE ("A dependency container with SUCCESS or COMPLETE
        # condition cannot be an essential container"). With finalizers, the
        # main container IS such a dependency, so the finalizers carry
        # `essential` instead (below) and the task ends when they exit.
        essential   = length(var.finalizer_containers) == 0
        entryPoint  = local.wrapped_command
        environment = [for k, v in var.environment : { name = k, value = v }]
        # valueFrom = Secrets Manager ARN, resolved by ECS at start via the execution role.
        secrets     = [for k, v in var.secrets : { name = k, valueFrom = v }]
        mountPoints = var.mount_points
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = var.log_group_name
            "awslogs-region"        = var.region
            "awslogs-stream-prefix" = var.name
          }
        }
      },
      length(var.init_containers) == 0 ? {} : {
        # dependsOn condition SUCCESS = wait for that container to exit 0 before starting.
        dependsOn = [for c in var.init_containers : { containerName = c.name, condition = "SUCCESS" }]
      },
    )],
    # Finalizers depend on the MAIN container succeeding, so nothing is archived
    # from a run that crashed half-way and would otherwise publish a truncated
    # report over a complete one. They are the essential containers (a task
    # needs at least one): if the main container fails, the finalizer's
    # dependency can never be met and ECS stops the task, which is the intent.
    [for c in var.finalizer_containers : merge(c, {
      essential = true
      dependsOn = [{ containerName = var.name, condition = "SUCCESS" }]
    })],
  ))

  # [TF] dynamic "volume" emits one nested volume {} block per list element
  # (volume.value = the element). No host/EFS config = Fargate ephemeral storage.
  dynamic "volume" {
    for_each = toset(var.volumes)
    content {
      name = volume.value
    }
  }

  tags = merge(var.tags, { Name = local.family })
}

# --------------------------------------------------------------------------- #
# Scheduler role. iam:PassRole is the part that is easy to get wrong: the
# scheduler does not just call RunTask, it hands ECS the task's two roles, and
# without permission to pass them the schedule fails at INVOCATION time with a
# message that never reaches a human. Scoped to exactly those two role ARNs.
# --------------------------------------------------------------------------- #
# Trust policy: only EventBridge Scheduler (scheduler.amazonaws.com) may assume
# the scheduler role. [TF] data "aws_iam_policy_document" renders IAM JSON from
# HCL, read as .json — no AWS resource is created.
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Stops this role being assumable on behalf of another account's scheduler.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# [TF] data "aws_caller_identity" looks up the account id of the credentials
# Terraform is running with. [TF] Order in the file does not matter — it is
# referenced above before it is declared.
data "aws_caller_identity" "current" {}

# IAM role EventBridge Scheduler assumes when the schedule fires.
# Output scheduler_role_arn (no caller reads it today).
resource "aws_iam_role" "scheduler" {
  name               = "${local.family}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = var.tags
}

# Permission policy for that role: RunTask this task family (any revision) on
# this cluster only, and pass the task's execution/task roles to ECS.
data "aws_iam_policy_document" "scheduler" {
  statement {
    actions = ["ecs:RunTask"]
    # The task definition ARN is versioned; the wildcard covers future revisions
    # so a new deploy does not silently break the schedule's permission.
    # arn_without_revision = "arn:...:task-definition/<family>"; ":*" = any revision.
    resources = ["${aws_ecs_task_definition.this.arn_without_revision}:*"]
    condition {
      test     = "ArnLike"
      variable = "ecs:cluster"
      values   = [var.cluster_arn]
    }
  }

  statement {
    actions = ["iam:PassRole"]
    # [TF] distinct() removes duplicates (the two role ARNs could be the same).
    resources = distinct([var.execution_role_arn, var.task_role_arn])
    condition {
      test     = "StringLike"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Inline policy attached to the scheduler role.
resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.family}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

# --------------------------------------------------------------------------- #
# The schedule
# --------------------------------------------------------------------------- #
# EventBridge Scheduler schedule (console: Amazon EventBridge > Scheduler >
# Schedules) — the cron trigger that calls ecs:RunTask. Output schedule_name ->
# module.drift_monitor[0].schedule_name -> careroute_stack output drift_monitor_schedule.
resource "aws_scheduler_schedule" "this" {
  #checkov:skip=CKV_AWS_297:Schedule is encrypted at rest with an AWS-owned key; the payload is only RunTask parameters (ARNs, subnet/SG ids), no secrets. A CMK is the same cost trade as CKV_AWS_145 in .checkov.yaml.
  name       = local.family
  group_name = "default"
  state      = var.enabled ? "ENABLED" : "DISABLED"

  # OFF, not a window. A flexible window lets AWS move the invocation by up to
  # the configured minutes, which is fine for a cleanup job and wrong for one
  # whose output is timestamped evidence compared run-to-run.
  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  # Target = the ECS cluster; Scheduler calls RunTask on it with ecs_parameters.
  target {
    arn      = var.cluster_arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.this.arn
      task_count          = 1
      launch_type         = var.use_fargate_spot ? null : "FARGATE"
      # Copy the task definition's tags onto the launched task (cost allocation).
      propagate_tags = "TASK_DEFINITION"

      # FARGATE_SPOT when use_fargate_spot; otherwise launch_type = "FARGATE" above
      # (the two are mutually exclusive, hence null on one side).
      dynamic "capacity_provider_strategy" {
        for_each = var.use_fargate_spot ? [1] : []
        content {
          capacity_provider = "FARGATE_SPOT"
          weight            = 1
        }
      }

      network_configuration {
        subnets          = var.subnet_ids
        security_groups  = var.security_group_ids
        assign_public_ip = var.assign_public_ip
      }
    }

    # One retry covers a Spot capacity blip. Beyond that the run is genuinely
    # broken, and retrying for hours turns one failure into a wall of identical
    # ones — the maximum_event_age bound is what stops a backlog draining all at
    # once after an outage and running the monitor twelve times in a minute.
    retry_policy {
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 3600
    }
  }
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# Consumers: schedule_name -> careroute_stack outputs.tf (drift_monitor_schedule).
# schedule_arn, task_definition_arn, scheduler_role_arn: not read by any caller today.
output "schedule_name" { value = aws_scheduler_schedule.this.name }
output "schedule_arn" { value = aws_scheduler_schedule.this.arn }
output "task_definition_arn" { value = aws_ecs_task_definition.this.arn }
output "scheduler_role_arn" { value = aws_iam_role.scheduler.arn }
