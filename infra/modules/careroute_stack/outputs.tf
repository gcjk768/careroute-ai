###############################################################################
# careroute_stack — outputs
# The handful of values you actually need after an apply: where to reach the
# app, where CI pushes images, and how to find state/secrets.
###############################################################################
# [TF] LEARNER NOTES — an `output` exposes a value from this module to whoever called it:
# [TF]   - via Terragrunt this is the ROOT module, so `terragrunt output [-raw] <name>` prints it
# [TF]     (read from the S3 state file, no AWS calls);
# [TF]   - a parent module calling this one as module "stack" would read module.stack.<name>.
# [TF] `module.x.y` = output `y` of child module `x`; `local.z` = a local from main.tf (all .tf files
# [TF] in one folder are ONE module, so outputs.tf can see main.tf's locals and resources).

# Where to point the browser. Consumers: `terragrunt output -raw app_url` in .gitlab-ci.yml,
# README.md and live/demo.
# [TF] `cond ? a : b` is a ternary. module.api_gateway has `count`, so it is a LIST of instances
# [TF] and [0] picks the only one; the same condition guards it, since [0] on an empty list errors.
output "app_url" {
  description = "The URL to open for the demo — API Gateway endpoint if enabled, otherwise the public ALB."
  value       = var.enable_api_gateway ? module.api_gateway[0].api_endpoint : "http://${module.alb.alb_dns_name}"
}

# The API Gateway stage invoke URL (aws_apigatewayv2_stage.default.invoke_url in modules/api_gateway).
# [TF] `null` = "no value"; the output then shows as absent. Consumer: `terragrunt output` only.
output "api_endpoint" {
  description = "API Gateway endpoint (null when API Gateway is disabled)."
  value       = var.enable_api_gateway ? module.api_gateway[0].api_endpoint : null
}

# aws_lb.this.dns_name from modules/alb (e.g. careroute-demo-alb-123.ap-southeast-1.elb.amazonaws.com).
# Consumer: `terragrunt output` only.
output "alb_dns_name" {
  description = "ALB DNS name (public entry point in demo mode; internal otherwise)."
  value       = module.alb.alb_dns_name
}

# ECR repository URI (<acct>.dkr.ecr.<region>.amazonaws.com/<name>) for `docker push`.
# [TF] repository_urls is a MAP output of modules/ecr keyed by repo name; ["backend"] looks one up.
# Consumer: README.md push instructions (`terragrunt output -raw ecr_backend_repo`).
output "ecr_backend_repo" {
  description = "Push the backend image here in CI: <repo>:<tag>."
  value       = module.ecr.repository_urls["backend"]
}

# Same as above for the frontend image. Consumer: `terragrunt output` only.
output "ecr_frontend_repo" {
  value = module.ecr.repository_urls["frontend"]
}

# aws_db_instance.this.address from modules/rds — hostname only (port is separate).
output "rds_endpoint" {
  description = "RDS endpoint (null when RDS is disabled — the demo runs in-memory)."
  value       = var.enable_rds ? module.rds[0].endpoint : null
}

# aws_secretsmanager_secret.db.arn from modules/rds.
output "db_secret_arn" {
  description = "Secrets Manager ARN holding the DB credentials (null when RDS disabled)."
  value       = var.enable_rds ? module.rds[0].secret_arn : null
}


# Name of aws_cloudwatch_dashboard.this in modules/observability (find it in the CloudWatch console).
output "cloudwatch_dashboard" {
  value = module.observability.dashboard_name
}

# ARN of aws_sns_topic.alerts in modules/observability — the topic every CloudWatch alarm notifies.
output "alerts_sns_topic_arn" {
  value = module.observability.sns_topic_arn
}

# S3 bucket name (aws_s3_bucket.this.id in modules/artifacts).
output "artifacts_bucket" {
  description = "S3 bucket backing the DVC remote + ML telemetry (null when disabled)."
  value       = var.enable_artifacts_bucket ? module.artifacts[0].bucket_name : null
}

# s3:// URL from modules/artifacts. Consumer: README.md DVC setup (`terragrunt output -raw dvc_remote_url`).
output "dvc_remote_url" {
  description = "Run this in the app repo to give DVC somewhere to push: cd backend && dvc remote add -d origin <value> && git commit .dvc/config."
  value       = var.enable_artifacts_bucket ? module.artifacts[0].dvc_remote_url : null
}

# local.app_base_url (main.tf: ALB origin, or API GW endpoint) + "/grafana", which the ALB routes
# to Grafana via aws_lb_listener_rule.grafana. Consumer: README.md / live/demo post-apply steps.
# [TF] "${...}" inside a string is interpolation.
output "grafana_url" {
  description = "Grafana, on the same ALB as the app (null when the Prometheus stack is off). Log in as `admin`."
  value       = var.enable_prometheus_stack ? "${local.app_base_url}/grafana" : null
}

# Reads the secret_string of aws_secretsmanager_secret_version.grafana (main.tf) — either
# var.grafana_admin_password or random_password.grafana's result.
# [TF] `sensitive = true` on an output: shown as (sensitive value) in plan and plain `terragrunt output`;
# [TF] `-raw` / `-json` print it. Required here because the source value is itself sensitive.
output "grafana_admin_password" {
  description = "Grafana admin password — generated unless one was supplied. Read with `terragrunt output -raw grafana_admin_password`."
  value       = var.enable_prometheus_stack ? aws_secretsmanager_secret_version.grafana[0].secret_string : null
  sensitive   = true
}

# http://<prometheus Cloud Map name>:9090 from modules/monitoring_stack; resolves only inside the VPC
# (Cloud Map private DNS namespace "<project>.local").
output "prometheus_internal_url" {
  description = "Prometheus, reachable only inside the VPC (no public route — it exposes operational detail and has no auth of its own)."
  value       = var.enable_prometheus_stack ? module.monitoring[0].prometheus_internal_url : null
}

# http://<alertmanager Cloud Map name>:9093 from modules/monitoring_stack.
output "alertmanager_internal_url" {
  description = "Alertmanager, in-VPC only. Its receivers are intentionally empty; see modules/monitoring_stack."
  value       = var.enable_prometheus_stack ? module.monitoring[0].alertmanager_internal_url : null
}

# "<backend Cloud Map name>:<port>" from modules/monitoring_stack — the target in prometheus.yml.
output "prometheus_scrape_target" {
  description = "What Prometheus was told to scrape. If the backend's metrics are missing, check this resolves before anything else."
  value       = var.enable_prometheus_stack ? module.monitoring[0].scrape_target : null
}

# Echoes var.app_metrics_namespace, but only when the ADOT sidecar actually ships metrics.
output "app_metrics_namespace" {
  description = "CloudWatch namespace holding the app's own Prometheus metrics (null when the ADOT sidecar is disabled)."
  value       = var.enable_app_metrics ? var.app_metrics_namespace : null
}

# Effective runtime config, surfaced because both values are computed from the
# deployed topology rather than set by hand, and both are silent when wrong:
# a bad trusted-proxy list collapses every patient into one rate-limit bucket,
# and a bad CORS origin only shows up as a browser console error.
# = local.trusted_proxies in main.tf (the same string injected as the env var).
output "backend_trusted_proxies" {
  description = "What the backend was told to accept X-Forwarded-For from (CAREROUTE_TRUSTED_PROXIES)."
  value       = local.trusted_proxies
}

# = local.cors_origins in main.tf.
output "backend_cors_origins" {
  description = "Browser origins the backend will accept (CAREROUTE_CORS_ORIGINS)."
  value       = local.cors_origins
}

# --------------------------------------------------------------------------- #
# MLOps plumbing. Each of these is null until its feature is switched on, which
# is itself the answer to "is this actually deployed?" — a question the report
# needs to be able to answer honestly.
# --------------------------------------------------------------------------- #
# Built from main.tf locals telemetry_enabled and artifacts_bucket_name. (modules/artifacts has its
# own ml_telemetry_prefix output, which this stack does not use.)
output "ml_telemetry_prefix" {
  description = "Where the shipper sidecar archives the inference log and drift reports. Per-task subkeys, so two tasks cannot overwrite each other's evidence."
  value       = local.telemetry_enabled ? "s3://${local.artifacts_bucket_name}/ml-telemetry" : null
}

# internal_tracking_uri output of modules/mlflow.
# [TF] length(module.mlflow) > 0 is another way to ask "was the counted module created?" — a module
# [TF] with count = 0 is an empty list — so the condition lives in one place (main.tf's count).
output "mlflow_tracking_uri" {
  description = "MLFLOW_TRACKING_URI for anything INSIDE the VPC. GitLab CI cannot reach this without a VPC-resident runner — see modules/mlflow."
  value       = length(module.mlflow) > 0 ? module.mlflow[0].internal_tracking_uri : null
}

# aws_scheduler_schedule.this.name from modules/scheduled_task (EventBridge Scheduler console).
output "drift_monitor_schedule" {
  description = "Name of the EventBridge schedule running app.ml.monitor, or null if no scheduled monitor is deployed."
  value       = length(module.drift_monitor) > 0 ? module.drift_monitor[0].schedule_name : null
}

# Same value as prometheus_internal_url, under the name the app repo's CI jobs use (PROMETHEUS_URL).
# Consumer: copied by hand into the app repo's CI variables; nothing in this repo reads it.
output "prometheus_url" {
  description = <<-EOT
    In-VPC Prometheus base URL, for anything that queries the app's own metrics
    from inside the VPC. Deployment rollback does NOT use it: ECS rolls back on
    the CloudWatch alarms in deploy_safety.tf, which GitLab's shared runners
    (outside the VPC) could not have reached Prometheus to evaluate anyway.
  EOT
  value       = var.enable_prometheus_stack ? module.monitoring[0].prometheus_internal_url : null
}

# aws_ecs_service.this.name from modules/ecs_service (module "backend" in main.tf).
# Consumers: scripts/verify_rollout.sh and scripts/running_image_tag.sh (.gitlab-ci.yml).
output "backend_service_name" {
  description = "ECS service name of the backend."
  value       = module.backend.service_name
}

# modules/alb local.tls_enabled (true when var.acm_certificate_arn is non-empty).
output "tls_enabled" {
  description = "Whether the ALB terminates TLS. false means the demo is HTTP-only; see docs/vault/Lecture Alignment.md §9."
  value       = module.alb.tls_enabled
}

# modules/ecs_cluster cluster name. Consumers: scripts/verify_rollout.sh and
# scripts/running_image_tag.sh in .gitlab-ci.yml (apply / plan / rollback).
output "ecs_cluster_name" {
  description = "ECS cluster the app services run in."
  value       = module.ecs_cluster.cluster_name
}

# Consumer: scripts/verify_rollout.sh (apply / rollback jobs).
output "frontend_service_name" {
  description = "ECS service name of the frontend."
  value       = module.frontend.service_name
}

# Consumer: a human, once — paste into the APP project's CI/CD variables. The
# names match what the app's deploy:push-images, deploy:ecs and
# rollback:production jobs read.
output "app_ci_variables" {
  description = "CI/CD variables the app repo's deploy:push-images job needs (null when enable_app_ci_role = false)."
  value = var.enable_app_ci_role ? {
    AWS_DEPLOY_ROLE_ARN = module.app_ci[0].role_arn
    AWS_DEFAULT_REGION  = var.region
    ECR_REGISTRY        = split("/", module.ecr.repository_urls["backend"])[0]
    ECR_NAMESPACE       = local.name_prefix
  } : null
}

# The shared state table (scaling.tf), or null when enable_shared_state_store is off.
output "state_table_name" {
  description = "DynamoDB table the backend is told to keep its state in (CAREROUTE_STATE_TABLE)."
  value       = var.enable_shared_state_store ? module.state_store[0].table_name : null
}
