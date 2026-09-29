###############################################################################
# Module: ci_oidc
# -----------------------------------------------------------------------------
# Lets the APP repo's GitLab pipeline push images to this stack's ECR repos
# AND roll them out on ECS, with a short-lived OIDC token instead of stored
# AWS access keys.
#
#   app CI job ──(id_token, aud = gitlab url)──▶ sts:AssumeRoleWithWebIdentity
#                                                  └─▶ this role: ECR push +
#                                                      ECS image rollout
#
# Split of ownership: Terraform (THIS repo) owns the infrastructure; the app
# pipeline (deploy:ecs, rollback:production -> scripts/deploy_ecs.sh) owns
# which image tag the services run. It registers a new task-definition
# revision with the new tag and updates the service. An infra-only apply keeps
# that tag (scripts/running_image_tag.sh), so the two never fight.
#
# Still NARROW: update services in THIS stack's cluster only, pass only the
# stack's two ECS roles and only to ECS tasks, and write only this env's
# release history. It cannot create services, change IAM, or touch other infra.
# The trade-off: a pipeline on the protected `main` branch of the app project
# can now put an image into service, so protect that branch.
###############################################################################

# Set by: careroute_stack/main.tf (module "app_ci") local.name_prefix. Used in: the role name.
variable "name_prefix" { type = string }

# Set by: careroute_stack var.gitlab_url (default https://gitlab.com).
# Used in: the OIDC provider URL, its audience, and the trust-policy condition keys.
variable "gitlab_url" {
  type    = string
  default = "https://gitlab.com"
}

# Set by: careroute_stack var.app_ci_project_id. Used in: the `sub` condition.
#
# The project's NUMERIC id, not its path. GitLab keeps a tombstone of every
# project path that a different project once held, and refuses to mint an ID
# token for a project sitting on such a path while its sub claim is path-based
# (failure_reason: id_token_burned_project_path) — the defence against someone
# reclaiming a freed path to mint a `sub` that an existing trust policy accepts.
# This project's path is burned, so a `project_path:` sub can never work here.
# The numeric id is globally unique, never reassigned, and survives every rename
# or group transfer, so it is the better anchor regardless.
#
# MUST match the app project's CI/CD -> "ID token subject claim" setting, which
# has to be ["project_id", "ref_type", "ref"] for this to line up.
variable "project_id" {
  description = "Numeric GitLab project id of the APP project whose pipelines may assume the role, e.g. 84456994."
  type        = string
}

# Set by: careroute_stack var.app_ci_ref. Used in: the `sub` condition.
variable "ref" {
  description = "Branch whose pipelines may push. Protect it in GitLab: anyone who can push to it can publish images."
  type        = string
  default     = "main"
}

# Set by: careroute_stack var.app_ci_extra_refs. Used in: the `sub` condition.
variable "extra_refs" {
  description = "Further branches trusted like `ref` (e.g. the app's deploy-only branch). Protect each one in GitLab for the same reason."
  type        = list(string)
  default     = []
}

# Set by: careroute_stack var.create_gitlab_oidc_provider.
variable "create_provider" {
  description = <<-EOT
    An AWS account holds ONE OIDC provider per issuer URL. The first environment
    applied into an account creates it; any other environment in the same
    account must set this false and look the existing one up instead, or its
    apply fails with EntityAlreadyExists.
  EOT
  type        = bool
  default     = true
}

# Set by: careroute_stack -> values(module.ecr.repository_arns). Used in: the push statement.
variable "repository_arns" { type = list(string) }

# Set by: careroute_stack (module.artifacts[0].bucket_arn when the artifacts
# bucket is on, "" otherwise). Used in: the two S3 statements below, which
# exist only when it is non-empty. The app pipeline's `data:version` job runs
# `dvc push` against s3://<bucket>/dvc over this same role, so the grant is
# scoped to that prefix: the role can publish a dataset snapshot and nothing
# else in the bucket (the ML telemetry the backend ships lives under a
# different prefix and is the task role's business).
variable "artifacts_bucket_arn" {
  description = "ARN of the artifacts bucket whose dvc/ prefix this role may read and write; empty = no S3 access."
  type        = string
  default     = ""
}

# Set by: careroute_stack -> module.ecs_cluster.cluster_arn. Used in: the
# EcsRollout statement (services under this cluster only) and, via its
# region/account fields, the release-history parameter ARN.
variable "cluster_arn" { type = string }

# Set by: careroute_stack -> the cluster's execution + task role ARNs. A new
# task-definition revision names these roles, so registering it needs PassRole.
variable "pass_role_arns" { type = list(string) }

# Set by: careroute_stack var.environment. Used in: the release-history
# parameter path /careroute/<env>/image-tag/* (scripts/deploy_ecs.sh in the app
# repo, scripts/release_history.sh here).
variable "environment" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

locals {
  # arn:aws:ecs:<region>:<account>:cluster/<name>
  arn_parts   = split(":", var.cluster_arn)
  issuer_host = replace(var.gitlab_url, "https://", "")
  provider_arn = (var.create_provider
    ? aws_iam_openid_connect_provider.gitlab[0].arn
  : data.aws_iam_openid_connect_provider.gitlab[0].arn)
}

# The account's trust anchor for GitLab-issued tokens. No thumbprint: AWS
# validates gitlab.com against its own trusted CA library.
resource "aws_iam_openid_connect_provider" "gitlab" {
  count          = var.create_provider ? 1 : 0
  url            = var.gitlab_url
  client_id_list = [var.gitlab_url]
  tags           = var.tags
}

data "aws_iam_openid_connect_provider" "gitlab" {
  count = var.create_provider ? 0 : 1
  url   = var.gitlab_url
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer_host}:aud"
      values   = [var.gitlab_url]
    }
    # Only pipelines for this ref of this one project — not forks, not other
    # projects in the group, not merge-request pipelines from other branches.
    # Keyed on the numeric project id (see the variable's comment): a renamed or
    # transferred project keeps this trust policy working, and a project that
    # later takes over our old path does NOT inherit it.
    condition {
      test     = "StringEquals"
      variable = "${local.issuer_host}:sub"
      values   = [for r in concat([var.ref], var.extra_refs) : "project_id:${var.project_id}:ref_type:branch:ref:${r}"]
    }
  }
}

resource "aws_iam_role" "push" {
  name                 = "${var.name_prefix}-app-ci-ecr-push"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
  tags                 = var.tags
}

data "aws_iam_policy_document" "push" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action has no resource-level scoping
  }

  statement {
    sid = "EcrPushToTheseReposOnly"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages",
    ]
    resources = var.repository_arns
  }

  # ECS image rollout (app repo scripts/deploy_ecs.sh). Register/Describe
  # task definition have no resource-level scoping; the blast radius is held
  # by UpdateService (this cluster's services) and PassRole (these two roles).
  statement {
    sid       = "EcsTaskDefinitions"
    actions   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"]
    resources = ["*"]
  }
  statement {
    sid     = "EcsRolloutThisClusterOnly"
    actions = ["ecs:DescribeServices", "ecs:UpdateService"]
    # cluster ARN ".../cluster/<name>" -> service ARNs ".../service/<name>/*"
    resources = ["${replace(var.cluster_arn, ":cluster/", ":service/")}/*"]
  }
  statement {
    sid       = "PassOnlyTheStackEcsRoles"
    actions   = ["iam:PassRole"]
    resources = var.pass_role_arns
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    sid     = "ReleaseHistory"
    actions = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = [
      "arn:aws:ssm:${local.arn_parts[3]}:${local.arn_parts[4]}:parameter/careroute/${var.environment}/image-tag/*"
    ]
  }

  # DVC remote: list the bucket (DVC checks what is already there before it
  # uploads) restricted to the dvc/ prefix, and read/write objects under it.
  dynamic "statement" {
    for_each = var.artifacts_bucket_arn != "" ? [1] : []
    content {
      sid       = "DvcRemoteList"
      actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
      resources = [var.artifacts_bucket_arn]
      condition {
        test     = "StringLike"
        variable = "s3:prefix"
        values   = ["dvc/*", "dvc", ""]
      }
    }
  }
  dynamic "statement" {
    for_each = var.artifacts_bucket_arn != "" ? [1] : []
    content {
      sid       = "DvcRemoteObjects"
      actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
      resources = ["${var.artifacts_bucket_arn}/dvc/*"]
    }
  }
}

resource "aws_iam_role_policy" "push" {
  name   = "ecr-push"
  role   = aws_iam_role.push.id
  policy = data.aws_iam_policy_document.push.json
}

# -> careroute_stack output app_ci_variables (AWS_DEPLOY_ROLE_ARN in the app repo).
output "role_arn" { value = aws_iam_role.push.arn }
