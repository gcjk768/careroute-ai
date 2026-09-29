###############################################################################
# Module: monitoring_stack
# -----------------------------------------------------------------------------
# Prometheus + Alertmanager + Grafana on ECS Fargate — the SAME three tools the
# application runs in its docker-compose, deployed to AWS so the architecture
# that gets presented is the architecture that actually runs.
#
# WHY THIS EXISTS. The app exports a rich Prometheus surface
# (backend/app/metrics.py: escalations, guardrail blocks, per-agent step
# latency, served acuity mix, clinician agreement) and ships alert rules and a
# Grafana dashboard for it. The infra-native alternative in this repo
# (an ADOT sidecar publishing to CloudWatch) is cheaper and needs no extra
# tasks, but it renders a Prometheus histogram as a StatisticSet — so the p95
# rules in alert.rules.yml cannot be expressed, and the dashboard the team
# actually demonstrates does not exist in the cloud. This module closes that
# gap: same configs, same queries, same dashboard, running in AWS.
#
#   Prometheus  scrapes backend.<ns>:8000/metrics  (Cloud Map, in-VPC)
#               evaluates alert.rules.yml
#               pushes firing alerts to alertmanager.<ns>:9093
#   Grafana     queries prometheus.<ns>:9090, exposed at <alb>/grafana
#
# CONFIG DELIVERY. Fargate has no bind mount, and these are stock upstream
# images, so each container's entryPoint is overridden to write its config from
# an environment variable and then exec the real binary. The alternatives were
# EFS (an always-on charge for three throwaway containers) and custom images (a
# second build pipeline for files that change more often than code). Configs go
# to /tmp because all three images run as non-root and cannot write /etc.
#
# CONFIG DRIFT — READ THIS. config/ holds a COPY of the app repo's
# monitoring/*.yml. They must be kept in step by hand; a cross-repo file
# reference would break this repo's CI, which checks out only this repo.
# alert.rules.yml, alertmanager.yml and careroute-triage.json are verbatim
# copies. prometheus.yml and the Grafana datasource are templated, because
# their targets are Cloud Map DNS names rather than compose service names.
#
# This is not a theoretical risk: on 2026-09-17 alert.rules.yml here was found
# to be 74 lines behind the app repo, missing the whole `careroute-llm` group
# (LLM cost, token-quota, p95 latency and provider-failure alerts). Prometheus
# loads a file with a group missing WITHOUT complaining, so the stack claimed
# LLM cost alerting it did not have. The `config_drift` job in .gitlab-ci.yml
# now diffs the two copies on every pipeline; keep it green.
###############################################################################

# ---- Inputs ---------------------------------------------------------------- #
# [TF] Each `variable` is an input of this module, set by
#      `module "monitoring" { ... }` in modules/careroute_stack/main.tf and read
#      here as var.<name>. That module block has `count = var.enable_prometheus_stack
#      ? 1 : 0` (true in live/demo and live/staging), so this whole module is
#      optional; the stack reads its outputs as module.monitoring[0].<name>.
#
# cluster_id / cluster_name: the ECS cluster the three services run in.
#   Set by: module.ecs_cluster.cluster_id / .cluster_name. Used in: passed
#   straight through to each module "prometheus"/"alertmanager"/"grafana" below.
variable "cluster_id" { type = string }
variable "cluster_name" { type = string }
# region: Set by var.region (root.hcl <- live/<env>/env.hcl). Used in: passed to
#   each ecs_service, which uses it for the awslogs log-driver region.
variable "region" { type = string }

# execution_role_arn: the role the ECS AGENT uses to pull the image, write logs
#   and fetch `secrets` (the Grafana password) — module.ecs_cluster.execution_role_arn.
# task_role_arn: the role the containers THEMSELVES assume (module.ecs_cluster.task_role_arn).
# log_group_name: module.ecs_cluster.log_group_name. All three passed through.
variable "execution_role_arn" { type = string }
variable "task_role_arn" { type = string }
variable "log_group_name" { type = string }

# subnet_ids: Set by local.task_subnet_ids — public subnets when
#   var.public_networking (live/demo), private otherwise (live/staging).
# security_group_ids: [module.security.ecs_sg_id], the SAME SG as the backend.
#   Its self-referencing ingress rule is what lets Prometheus reach
#   backend:8000, Grafana reach prometheus:9090 and Prometheus reach
#   alertmanager:9093 without any extra SG rules.
variable "subnet_ids" { type = list(string) }
variable "security_group_ids" { type = list(string) }
# Set by: local.assign_public_ip (= var.public_networking). Tasks in public
# subnets with no NAT need a public IP to pull images from Docker Hub.
variable "assign_public_ip" {
  type    = bool
  default = false
}
# Set by: var.use_fargate_spot (live/demo true, live/staging false). Passed through.
variable "use_fargate_spot" {
  type    = bool
  default = true
}

# service_discovery_namespace_id: the Cloud Map private DNS namespace.
#   Set by: aws_service_discovery_private_dns_namespace.this[0].id in careroute_stack
#   (created because local.need_cloud_map is true whenever this module is on).
#   Used in: each service's Cloud Map registration -> <name>.careroute.local.
variable "service_discovery_namespace_id" { type = string }
# Set by: "${var.project}.local" = "careroute.local". Used in: the three *_host
# locals, i.e. the DNS names written into the Prometheus/Grafana configs.
variable "namespace_name" {
  description = "Private DNS namespace (e.g. careroute.local) used to build the in-VPC targets."
  type        = string
}

# Set by: the literal "backend" in careroute_stack — it must equal the `name` of
# module "backend" (modules/ecs_service), because that is the Cloud Map name the
# backend registers as. Used in: local.backend_host.
variable "backend_service_name" {
  description = "Cloud Map service name of the backend, i.e. the scrape target host."
  type        = string
  default     = "backend"
}
# Set by: the literal 8000 (the FastAPI port). Used in: the scrape target
# (prometheus.yml.tftpl) and the scrape_target output.
variable "backend_port" {
  type    = number
  default = 8000
}

# Set by: var.prometheus_scrape_interval (default "15s"). Used in:
# templatefile() of prometheus.yml.tftpl (the `scrape_interval` placeholder).
variable "scrape_interval" {
  description = "Prometheus scrape interval. 15s matches the app's compose config; nothing here bills per datapoint, so it can stay fine-grained."
  type        = string
  default     = "15s"
}

# Set by: module.alb.grafana_target_group_arn (exists because the stack passes
# enable_grafana_route = var.enable_prometheus_stack to modules/alb, which adds
# the /grafana* listener rule). Used in: module "grafana" target_group_arn, so
# the ECS service registers Grafana's task IP in that target group.
variable "grafana_target_group_arn" {
  description = "ALB target group that fronts Grafana at /grafana."
  type        = string
  default     = null
}
# Set by: "${local.app_base_url}/grafana" in careroute_stack (ALB origin, or the
# API Gateway endpoint when enabled). Used in: GF_SERVER_ROOT_URL.
variable "grafana_root_url" {
  description = "Absolute URL Grafana believes it is served from. Grafana builds redirects and asset links from this, so a wrong value gives a blank page behind the ALB rather than an error."
  type        = string
}
# Set by: aws_secretsmanager_secret.grafana[0].arn (value = a random_password
# unless var.grafana_admin_password was given). Used in: grafana `secrets`.
variable "grafana_admin_password_secret_arn" {
  description = "Secrets Manager ARN holding the Grafana admin password."
  type        = string
}

# Container images (Docker Hub). Set by: var.prometheus_image / alertmanager_image
# / grafana_image in careroute_stack (same defaults). Used in: each module's `image`.
variable "prometheus_image" {
  type    = string
  default = "prom/prometheus:v3.1.0"
}
variable "alertmanager_image" {
  type    = string
  default = "prom/alertmanager:v0.28.0"
}
variable "grafana_image" {
  type    = string
  default = "grafana/grafana:11.5.1"
}

# Fargate task size, the same for all three tasks (256 = 0.25 vCPU, 512 MiB).
# Set by: var.monitoring_cpu / var.monitoring_memory. Used in: each module's cpu/memory.
variable "cpu" {
  type    = number
  default = 256
}
variable "memory" {
  type    = number
  default = 512
}

# Set by: local.common_tags. Passed through to every ecs_service.
variable "tags" {
  type    = map(string)
  default = {}
}

locals {
  # [TF] `locals` = values computed in this module, read as local.<name>.
  # Cloud Map DNS names: <service>.<namespace>, e.g. prometheus.careroute.local.
  prometheus_host   = "prometheus.${var.namespace_name}"
  alertmanager_host = "alertmanager.${var.namespace_name}"
  backend_host      = "${var.backend_service_name}.${var.namespace_name}"

  # [TF] templatefile(PATH, MAP) reads a file and fills its ${...} placeholders
  #      from MAP. path.module = this module's directory, so the path works no
  #      matter where terraform runs. The result is a plain string that is
  #      passed below as a container environment variable.
  prometheus_config = templatefile("${path.module}/config/prometheus.yml.tftpl", {
    scrape_interval     = var.scrape_interval
    backend_host        = local.backend_host
    backend_port        = var.backend_port
    alertmanager_target = "${local.alertmanager_host}:9093"
    alertmanager_host   = local.alertmanager_host
  })

  # [TF] file(PATH) reads a file verbatim as a string (no placeholders).
  alert_rules       = file("${path.module}/config/alert.rules.yml")
  platform_rules    = file("${path.module}/config/platform.rules.yml")
  alertmanager_conf = file("${path.module}/config/alertmanager.yml")

  grafana_datasource = templatefile("${path.module}/config/grafana-datasource.yml.tftpl", {
    prometheus_url = "http://${local.prometheus_host}:9090"
  })
  grafana_dashboards_provider = file("${path.module}/config/grafana-dashboards.yml")
  # The dashboard covers every metric the app exports (~75 panels): ~95 KB as
  # committed, ~56 KB minified. An ECS task definition is capped at 64 KB in
  # TOTAL, so it cannot ride in an env var as plain JSON. jsondecode/jsonencode
  # minifies it, base64gzip() compresses it to ~10 KB, and the container
  # entrypoint below reverses that with busybox base64 + gunzip (the Grafana
  # image is Alpine). The file itself stays a verbatim copy of the app's, so
  # the config_drift job still diffs it byte-for-byte.
  grafana_dashboard_gz = base64gzip(jsonencode(jsondecode(file("${path.module}/config/careroute-triage.json"))))
}

# --------------------------------------------------------------------------- #
# Prometheus — scrape + rule evaluation.
#
# --web.external-url keeps the links in a firing alert pointing at something a
# human can open. --storage.tsdb.retention.time is short on purpose: the task
# has only ephemeral storage, so a long retention would just be deleted on the
# next deploy while consuming task memory in the meantime.
# --------------------------------------------------------------------------- #
# [TF] A `module` block calls another module — here the generic
#      modules/ecs_service, which creates a task definition, an ECS service and
#      (enable_service_discovery) a Cloud Map service named `name`. Arguments
#      are that module's variables; its outputs are module.prometheus.<name>.
module "prometheus" {
  source       = "../ecs_service"
  name         = "prometheus"
  cluster_id   = var.cluster_id
  cluster_name = var.cluster_name
  region       = var.region

  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.task_role_arn
  log_group_name     = var.log_group_name

  image          = var.prometheus_image
  container_port = 9090
  cpu            = var.cpu
  memory         = var.memory

  # entry_point/command override the image's ENTRYPOINT/CMD. [TF] join(" ", list)
  # glues the list into ONE string, so `sh -c` receives a single shell script:
  # write the configs from env vars to /tmp, then exec Prometheus.
  entry_point = ["/bin/sh", "-c"]
  command = [join(" ", [
    "printf '%s' \"$PROMETHEUS_CONFIG\" > /tmp/prometheus.yml &&",
    "printf '%s' \"$PROMETHEUS_RULES\" > /tmp/alert.rules.yml &&",
    "printf '%s' \"$PROMETHEUS_PLATFORM_RULES\" > /tmp/platform.rules.yml &&",
    "exec /bin/prometheus",
    "--config.file=/tmp/prometheus.yml",
    "--storage.tsdb.path=/prometheus",
    "--storage.tsdb.retention.time=6h",
    "--web.enable-lifecycle",
  ])]

  # Rendered prometheus.yml + the alert rules, delivered as env vars (container
  # definitions have an overall size limit, so keep these files modest).
  environment = {
    PROMETHEUS_CONFIG = local.prometheus_config
    PROMETHEUS_RULES  = local.alert_rules
    # Infra-owned rules (config/platform.rules.yml), not part of the app copy.
    PROMETHEUS_PLATFORM_RULES = local.platform_rules
  }

  subnet_ids         = var.subnet_ids
  assign_public_ip   = var.assign_public_ip
  security_group_ids = var.security_group_ids
  use_fargate_spot   = var.use_fargate_spot

  # Registers prometheus.careroute.local (A record -> task IP) in Cloud Map.
  enable_service_discovery       = true
  service_discovery_namespace_id = var.service_discovery_namespace_id

  tags = var.tags
}

# --------------------------------------------------------------------------- #
# Alertmanager — turns a FIRING rule into a notification.
#
# Its receivers are intentionally EMPTY, exactly as in the app repo: a real
# webhook belongs in a mounted file (`*_file`), never in committed YAML. So this
# proves the routing tree, grouping and inhibition work; delivery is the step
# that needs a real endpoint and a secret.
# --------------------------------------------------------------------------- #
module "alertmanager" {
  source       = "../ecs_service"
  name         = "alertmanager"
  cluster_id   = var.cluster_id
  cluster_name = var.cluster_name
  region       = var.region

  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.task_role_arn
  log_group_name     = var.log_group_name

  # container_port :9093 is where Prometheus pushes firing alerts (alertmanager_target above).
  image          = var.alertmanager_image
  container_port = 9093
  cpu            = var.cpu
  memory         = var.memory

  entry_point = ["/bin/sh", "-c"]
  command = [join(" ", [
    "printf '%s' \"$ALERTMANAGER_CONFIG\" > /tmp/alertmanager.yml &&",
    "exec /bin/alertmanager",
    "--config.file=/tmp/alertmanager.yml",
    "--storage.path=/alertmanager",
  ])]

  environment = {
    ALERTMANAGER_CONFIG = local.alertmanager_conf
  }

  subnet_ids         = var.subnet_ids
  assign_public_ip   = var.assign_public_ip
  security_group_ids = var.security_group_ids
  use_fargate_spot   = var.use_fargate_spot

  enable_service_discovery       = true
  service_discovery_namespace_id = var.service_discovery_namespace_id

  tags = var.tags
}

# --------------------------------------------------------------------------- #
# Grafana — the dashboard the team demonstrates, provisioned on boot.
#
# GF_PATHS_PROVISIONING is repointed at /tmp because the image runs as uid 472
# and cannot write /etc/grafana. SERVE_FROM_SUB_PATH + ROOT_URL are what make it
# work behind an ALB path rule instead of needing its own load balancer.
# The admin password comes from Secrets Manager, never from a task-definition
# environment variable.
# --------------------------------------------------------------------------- #
module "grafana" {
  source       = "../ecs_service"
  name         = "grafana"
  cluster_id   = var.cluster_id
  cluster_name = var.cluster_name
  region       = var.region

  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.task_role_arn
  log_group_name     = var.log_group_name

  # container_port 3000 = Grafana's HTTP port; the ALB target group forwards /grafana* here.
  image          = var.grafana_image
  container_port = 3000
  cpu            = var.cpu
  memory         = var.memory

  entry_point = ["/bin/sh", "-c"]
  command = [join(" ", [
    "mkdir -p /tmp/provisioning/datasources /tmp/provisioning/dashboards /tmp/dashboards &&",
    "printf '%s' \"$GF_DATASOURCE\" > /tmp/provisioning/datasources/prometheus.yml &&",
    "printf '%s' \"$GF_DASHBOARD_PROVIDER\" > /tmp/provisioning/dashboards/dashboards.yml &&",
    "printf '%s' \"$GF_DASHBOARD_GZ\" | base64 -d | gunzip > /tmp/dashboards/careroute-triage.json &&",
    "exec /run.sh",
  ])]

  # GF_* env vars are Grafana's own config keys (GF_<SECTION>_<KEY>).
  environment = {
    GF_DATASOURCE         = local.grafana_datasource
    GF_DASHBOARD_PROVIDER = local.grafana_dashboards_provider
    GF_DASHBOARD_GZ       = local.grafana_dashboard_gz

    GF_PATHS_PROVISIONING          = "/tmp/provisioning"
    GF_SERVER_ROOT_URL             = var.grafana_root_url
    GF_SERVER_SERVE_FROM_SUB_PATH  = "true"
    GF_SECURITY_ADMIN_USER         = "admin"
    GF_USERS_ALLOW_SIGN_UP         = "false"
    GF_ANALYTICS_REPORTING_ENABLED = "false"
    GF_ANALYTICS_CHECK_FOR_UPDATES = "false"
    # Grafana's own /metrics is on by default and unauthenticated, and this is
    # the one monitoring task with a public ALB route. Nothing scrapes it, so
    # turn it off rather than publish Grafana's internals next to the dashboard.
    GF_METRICS_ENABLED = "false"
  }

  # ECS `secrets`: name => Secrets Manager ARN. At task start the EXECUTION role
  # fetches the value and injects it as an env var; it never appears in the
  # task definition.
  secrets = {
    GF_SECURITY_ADMIN_PASSWORD = var.grafana_admin_password_secret_arn
  }

  # target_group_arn (below) attaches the service to the ALB (only Grafana is public).
  subnet_ids         = var.subnet_ids
  assign_public_ip   = var.assign_public_ip
  security_group_ids = var.security_group_ids
  use_fargate_spot   = var.use_fargate_spot
  target_group_arn   = var.grafana_target_group_arn

  enable_service_discovery       = true
  service_discovery_namespace_id = var.service_discovery_namespace_id

  tags = var.tags
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# Outputs, read in careroute_stack as module.monitoring[0].<name>:
#   prometheus_internal_url   -> outputs.tf prometheus_internal_url + prometheus_url
#   alertmanager_internal_url -> outputs.tf alertmanager_internal_url
#   scrape_target             -> outputs.tf prometheus_scrape_target
#   grafana_service_name      -> not consumed by the stack.
output "prometheus_internal_url" { value = "http://${local.prometheus_host}:9090" }
output "alertmanager_internal_url" { value = "http://${local.alertmanager_host}:9093" }
output "scrape_target" { value = "${local.backend_host}:${var.backend_port}" }
output "grafana_service_name" { value = module.grafana.service_name }
