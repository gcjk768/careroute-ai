###############################################################################
# Module: ecs_cluster
# -----------------------------------------------------------------------------
# The shared ECS Fargate cluster that hosts every agent task, plus the two IAM
# roles and the CloudWatch log group they all use:
#
#   * execution role -> used by the ECS agent to pull images from ECR, write
#                       logs and read secrets from Secrets Manager (control plane)
#   * task role      -> the identity the app code itself runs as (data plane).
#                       Kept minimal here; extend per least-privilege as agents
#                       gain AWS tool scopes (proposal: "per-agent least-privilege
#                       tool scopes").
#
# Container Insights is on so per-agent CPU/memory/duration land in CloudWatch.
###############################################################################

# --------------------------------------------------------------------------- #
# Inputs. [TF] A `variable` block is this module's input parameter; inside the
# module it is read as var.<name>. The caller is `module "ecs_cluster"` in
# modules/careroute_stack/main.tf, which sets each one as `<name> = <value>`.
# A variable with a `default` is optional for the caller; one without is required.
# --------------------------------------------------------------------------- #

# Prefix for every AWS name here (cluster, roles, log group, secret ARN pattern).
# Set by: module "ecs_cluster" -> local.name_prefix = "${var.project}-${var.environment}"
#         in careroute_stack (e.g. "careroute-demo"; environment from live/<env>/env.hcl).
# Used in: every `name`, the log group path, and the secrets_read ARN pattern.
variable "name_prefix" { type = string }

# How long CloudWatch Logs keeps the shared /ecs/<prefix> log group's events.
# Set by: module "ecs_cluster" -> var.log_retention_days in careroute_stack
#         (live/demo/terragrunt.hcl: 3, live/staging: 14; stack default 30).
# Used in: aws_cloudwatch_log_group.this.retention_in_days.
variable "log_retention_days" {
  type    = number
  default = 30
}

# Registers FARGATE_SPOT on the cluster so services MAY choose it.
# Set by: NOBODY — module "ecs_cluster" does not pass it, so it is always the
#         default (true). The per-service Spot choice is var.use_fargate_spot,
#         passed to ecs_service / scheduled_task / monitoring_stack instead.
# Used in: aws_ecs_cluster_capacity_providers.this.capacity_providers.
variable "enable_fargate_spot" {
  description = "Add the FARGATE_SPOT capacity provider (cheap, interruptible) for non-critical/staging tasks."
  type        = bool
  default     = true
}

# Tag map merged onto every taggable resource here.
# Set by: module "ecs_cluster" -> var.enable_container_insights (default false).
# Used in: aws_ecs_cluster.this setting "containerInsights".
variable "enable_container_insights" {
  description = <<-EOT
    Container Insights: per-cluster/service/task CPU, memory and network metrics
    in the ECS/ContainerInsights namespace. They bill as CloudWatch custom metrics
    (~$0.30 each per month) plus the performance-log ingest behind them, so a
    stack of six services runs roughly $15-20/month when left up. Nothing in
    this repo reads them: every alarm uses the free AWS/ECS and AWS/ApplicationELB
    metrics, and per-task CPU/memory is already on the Grafana dashboard from the
    processes' own /metrics. Off unless you want the CloudWatch console view.
  EOT
  type        = bool
  default     = false
}

# Set by: module "ecs_cluster" -> local.common_tags (var.tags + project/env tags).
variable "tags" {
  type    = map(string)
  default = {}
}

# --------------------------------------------------------------------------- #
# Cluster + log group
# --------------------------------------------------------------------------- #
# ECS Cluster = the logical grouping every Fargate service/task runs in. With
# Fargate there are no EC2 instances in it — it is just a namespace + settings.
# Referenced by: capacity providers below; outputs cluster_id/arn/name.
resource "aws_ecs_cluster" "this" {
  #checkov:skip=CKV_AWS_65:Toggle exists (var.enable_container_insights); off by default because it bills ~$15-20/month as custom metrics and nothing reads them (alarms use the free AWS/ECS + ALB metrics).
  name = "${var.name_prefix}-cluster"

  # Container Insights = extra per-task/service CPU, memory, network metrics in
  # CloudWatch (the ECS/ContainerInsights namespace). Billed; see the variable.
  # [TF] `setting { ... }` is a nested BLOCK (no `=`), not an argument.
  setting {
    name  = "containerInsights"
    value = var.enable_container_insights ? "enabled" : "disabled"
  }

  # [TF] merge() combines maps left-to-right, later keys win: all common tags
  # plus a Name tag (which shows as the resource's name in the console).
  tags = merge(var.tags, { Name = "${var.name_prefix}-cluster" })
}

# Which capacity providers the cluster can use (console: Cluster > Infrastructure).
# FARGATE = on-demand; FARGATE_SPOT = spare capacity, ~70% cheaper, can be
# reclaimed with a 2-minute warning.
resource "aws_ecs_cluster_capacity_providers" "this" {
  # [TF] aws_ecs_cluster.this.name is a REFERENCE to another resource's
  # attribute: <resource type>.<local name>.<attribute>. It also makes Terraform
  # create the cluster first (implicit dependency — no depends_on needed).
  cluster_name = aws_ecs_cluster.this.name
  # [TF] Ternary: condition ? value_if_true : value_if_false.
  capacity_providers = var.enable_fargate_spot ? ["FARGATE", "FARGATE_SPOT"] : ["FARGATE"]

  # Fallback used only when a service/task names NEITHER a launch_type NOR a
  # capacity_provider_strategy. This repo's services always name one (see
  # modules/ecs_service: launch_type = "FARGATE", or a FARGATE_SPOT strategy when
  # use_fargate_spot), so this default is effectively a safety net.
  # base = 1: the first task always goes on this provider; weight = share after that.
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# CloudWatch Logs group "/ecs/<prefix>" shared by EVERY container (backend,
# frontend, sub-agents, Prometheus, MLflow, drift monitor): each writes its own
# stream prefix via the awslogs driver. Exposed as output log_group_name.
resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${var.name_prefix}"
  retention_in_days = var.log_retention_days
  tags              = merge(var.tags, { Name = "${var.name_prefix}-logs" })
}

# --------------------------------------------------------------------------- #
# Task execution role — ECR pull + CloudWatch logs (AWS-managed policy) and a
# small inline policy to read the app's secrets from Secrets Manager.
# --------------------------------------------------------------------------- #
# [TF] A `data` block READS/COMPUTES something instead of creating it.
# data "aws_iam_policy_document" builds IAM policy JSON from HCL; read it via
# data.aws_iam_policy_document.<name>.json. Nothing is created in AWS.
# This one is the TRUST policy: "the ECS tasks service may assume this role".
# Used by BOTH roles below (execution + task) as their assume_role_policy.
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    # sts:AssumeRole is what a trust policy grants; the principal is who may call it.
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Task EXECUTION role — used by the ECS agent/Fargate itself BEFORE and AROUND
# your code: pull the image from ECR, create log streams, fetch `secrets` from
# Secrets Manager. Your application code never sees these credentials.
# Consumed via output execution_role_arn -> executionRoleArn in every task definition.
resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-ecs-exec-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = var.tags
}

# Attaches the AWS-managed AmazonECSTaskExecutionRolePolicy (ECR pull +
# CloudWatch Logs write) to the execution role. `role` takes the role NAME.
resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow the execution role to pull secret values referenced by container `secrets`.
# Inline permission: read any Secrets Manager secret whose name starts with
# "<name_prefix>/". That matches the secrets careroute_stack creates
# ("<prefix>/openai-api-key", "/onemap", "/onyx-api-key", "/grafana-admin-password",
# "/mlflow-backend-store-uri") and modules/rds ("<prefix>/db-credentials").
# The `*:*` wildcards are region and account.
data "aws_iam_policy_document" "secrets_read" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:*:*:secret:${var.name_prefix}/*"]
  }
}

# aws_iam_role_policy = an INLINE policy embedded in the role (vs a managed
# policy + attachment). `role` here is the role id (= its name for IAM roles).
resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${var.name_prefix}-exec-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.secrets_read.json
}

# --------------------------------------------------------------------------- #
# Task role — identity the running containers assume. Base role only; attach
# tightly-scoped policies per agent as they gain real AWS tool access.
# --------------------------------------------------------------------------- #
# Task role — the IAM identity your APPLICATION code gets (AWS SDK calls from
# inside the container use it automatically). Starts with no permissions;
# careroute_stack attaches the artifacts-bucket policy (task_artifacts) and the
# ADOT CloudWatch policy (otel_emf) to it by name when those features are on.
resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-ecs-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = var.tags
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# [TF] An `output` is this module's return value, read by the caller as
# module.ecs_cluster.<output name>. Consumers (all in careroute_stack/main.tf):
#   cluster_id   -> module backend / frontend / sub_agents / monitoring / mlflow
#   cluster_arn  -> module drift_monitor (EventBridge Scheduler targets need the ARN)
#   cluster_name -> the same ECS modules + module observability (dashboards/alarms)
output "cluster_id" { value = aws_ecs_cluster.this.id }
output "cluster_arn" { value = aws_ecs_cluster.this.arn }
output "cluster_name" { value = aws_ecs_cluster.this.name }
# execution_role_arn / task_role_arn -> every ECS module above (backend, frontend,
# sub_agents, monitoring, mlflow, drift_monitor) for their task definitions.
output "execution_role_arn" { value = aws_iam_role.execution.arn }
output "task_role_arn" { value = aws_iam_role.task.arn }

# The NAME is what aws_iam_role_policy/attachment take. Exposed so the stack can
# bolt on per-feature, least-privilege grants (ADOT metric publishing, the
# artifacts bucket) only when those features are switched on, instead of this
# module carrying permissions for things that may never be deployed.
# Consumers: aws_iam_role_policy_attachment.task_artifacts and
# aws_iam_role_policy.otel_emf in careroute_stack/main.tf.
output "task_role_name" { value = aws_iam_role.task.name }
# log_group_name -> every ECS module (awslogs-group), the ADOT exporter config,
# the drift monitor's init/finalizer containers, and module observability.
output "log_group_name" { value = aws_cloudwatch_log_group.this.name }
