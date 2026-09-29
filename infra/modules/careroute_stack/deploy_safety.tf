###############################################################################
# careroute_stack — CD rollback: the alarms ECS rolls back on, and the app CI role
# -----------------------------------------------------------------------------
# How a release reaches production and how it comes back out:
#
#   app CI  deploy:push-images ─▶ ECR <tag>        (module "app_ci" role)
#           deploy:ecs (manual) ─▶ new task-def revision per service with <tag>
#                                ─▶ update-service ─▶ wait + verify ─▶ record tag in SSM
#                     │
#                     └─ ECS rolls the service; while it does, and for a bake
#                        period after, it watches the alarms below. ALARM =
#                        automatic rollback to the previous task definition.
#   app CI  rollback:production: same script, onto the PREVIOUS tag in SSM.
#   infra   plan ─▶ apply (manual): infrastructure changes; keeps the running
#           tag (scripts/running_image_tag.sh). The `rollback` job here still
#           works as a Terraform-side fallback.
#
# The alarms live here rather than in modules/observability on purpose:
# observability reads module.backend.service_name, and the backend service
# needs these alarm names, so putting them there would be a dependency cycle.
# They also carry no SNS action — they are rollback triggers, not pages; the
# paging alarms stay in observability.
###############################################################################

locals {
  # One alarm per ALB-fronted service: 5xx share of that service's requests.
  rollback_alarm_targets = {
    backend  = module.alb.backend_target_group_arn_suffix
    frontend = module.alb.frontend_target_group_arn_suffix
  }

  backend_rollback_alarms  = var.enable_deploy_rollback_alarms ? [aws_cloudwatch_metric_alarm.deploy_rollback["backend"].alarm_name] : []
  frontend_rollback_alarms = var.enable_deploy_rollback_alarms ? [aws_cloudwatch_metric_alarm.deploy_rollback["frontend"].alarm_name] : []
}

# [TF] for_each over a map creates one alarm per key: deploy_rollback["backend"], ["frontend"].
resource "aws_cloudwatch_metric_alarm" "deploy_rollback" {
  for_each = var.enable_deploy_rollback_alarms ? local.rollback_alarm_targets : {}

  alarm_name          = "${local.name_prefix}-${each.key}-deploy-rollback"
  alarm_description   = "${each.key} 5xx share above ${var.deploy_rollback_error_rate * 100}% of requests. Watched by the ECS service during and after a deployment; ALARM rolls the deployment back."
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.deploy_rollback_error_rate
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  # No traffic is not an error. Without this an idle demo would sit in
  # INSUFFICIENT_DATA, which ECS does not treat as ALARM - but a reader would.
  treat_missing_data = "notBreaching"

  # [TF] A metric-MATH alarm: the queries below fetch the raw series, `rate`
  # combines them and is the only one with return_data = true (the one alarmed on).
  # The request floor stops one failed request out of three from rolling back a
  # release on a quiet demo: below it the expression reports 0. FILL(err, 0)
  # because a minute with no 5xx publishes NO datapoint, not a zero.
  metric_query {
    id          = "rate"
    expression  = "IF(req >= ${var.deploy_rollback_min_requests}, FILL(err, 0) / req, 0)"
    label       = "${each.key} 5xx rate"
    return_data = true
  }

  metric_query {
    id = "err"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      period      = 60
      stat        = "Sum"
      dimensions = {
        LoadBalancer = module.alb.alb_arn_suffix
        TargetGroup  = each.value
      }
    }
  }

  metric_query {
    id = "req"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      period      = 60
      stat        = "Sum"
      dimensions = {
        LoadBalancer = module.alb.alb_arn_suffix
        TargetGroup  = each.value
      }
    }
  }

  tags = local.common_tags
}

# OIDC role for the app repo's deploy:push-images (ECR push) and deploy:ecs /
# rollback:production (ECS image rollout) jobs (modules/ci_oidc).
module "app_ci" {
  source = "../ci_oidc"
  count  = var.enable_app_ci_role ? 1 : 0

  name_prefix     = local.name_prefix
  gitlab_url      = var.gitlab_url
  project_id      = var.app_ci_project_id
  ref             = var.app_ci_ref
  extra_refs      = var.app_ci_extra_refs
  create_provider = var.create_gitlab_oidc_provider
  repository_arns = values(module.ecr.repository_arns)
  cluster_arn     = module.ecs_cluster.cluster_arn
  pass_role_arns  = [module.ecs_cluster.execution_role_arn, module.ecs_cluster.task_role_arn]
  environment     = var.environment
  # ← module.artifacts[0].bucket_arn when enable_artifacts_bucket: lets the app
  #   pipeline's data:version job `dvc push` to s3://<bucket>/dvc.
  artifacts_bucket_arn = var.enable_artifacts_bucket ? module.artifacts[0].bucket_arn : ""
  tags                 = local.common_tags
}
