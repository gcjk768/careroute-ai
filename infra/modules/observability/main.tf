###############################################################################
# Module: observability
# -----------------------------------------------------------------------------
# The "Observability (logs - metrics)" store from the diagram, on the AWS side.
# Application/agent logs already flow to CloudWatch (set up in ecs_cluster); this
# module adds the metric layer: a dashboard, alarms and an SNS topic for alerts.
#
# The app also exposes Prometheus /metrics (backend/app/metrics.py) and ships
# Prometheus + Alertmanager + Grafana in its docker-compose for local work. In
# AWS there are three ways to keep those signals:
#
#   (a) run Prometheus + Grafana as ECS services       — most faithful, most $$$
#   (b) Amazon Managed Prometheus + Managed Grafana     — least ops, ~$9/mo floor
#   (c) scrape /metrics with an ADOT sidecar and publish
#       to CloudWatch as EMF                            — <<< what this does
#
# (c) is chosen because it adds NO always-on service and no new network path:
# the collector shares the backend task's network namespace and scrapes
# 127.0.0.1:8000/metrics. The alarms below are the CloudWatch translation of
# the app's monitoring/alert.rules.yml, so the same five conditions page in the
# cloud as page locally. Enabled by var.enable_app_metrics (off by default —
# it costs a few custom metrics plus a little log ingest).
#
# HONEST LIMITATION: EMF turns a Prometheus histogram into a StatisticSet
# (min/max/sum/count). CloudWatch can therefore alarm on the MEAN of
# careroute_model_predict_seconds, not on its p95 the way
# histogram_quantile(0.95, ...) does in alert.rules.yml. The p95 rules stay
# exact only on path (a)/(b). Where that matters the alarm below says so.
###############################################################################

# ---- Inputs ---------------------------------------------------------------- #
# [TF] A `variable` block is an input parameter of this module. The caller sets
#      it — here `module "observability" { ... }` in
#      modules/careroute_stack/main.tf — and this file reads it as var.<name>.
#      A variable with no `default` is REQUIRED: the caller must pass it.
#
# name_prefix: "<project>-<environment>", e.g. "careroute-demo"; keeps every
#   topic/alarm/dashboard/query name unique per environment.
#   Set by: local.name_prefix in careroute_stack (var.project default
#   "careroute" + var.environment, which root.hcl takes from live/<env>/env.hcl).
#   Used in: every name / alarm_name / dashboard_name below.
variable "name_prefix" { type = string }
# cluster_name: name of the ECS cluster. Set by: module.ecs_cluster.cluster_name.
#   Used in: backend_cpu alarm `dimensions` and the CPU/Memory dashboard widget.
variable "cluster_name" { type = string }
# backend_service_name: name of the backend ECS service. Set by:
#   module.backend.service_name (modules/ecs_service output). Used in: the same
#   two places — AWS/ECS service metrics are keyed by (ClusterName, ServiceName).
variable "backend_service_name" { type = string }

# The "arn suffix" is the app/<name>/<id> tail of the ALB ARN — the exact value
# CloudWatch uses for the AWS/ApplicationELB `LoadBalancer` dimension.
# Set by: module.alb.alb_arn_suffix. Used in: alb_5xx `dimensions` and the
# "ALB request count & 5xx" widget.
variable "alb_arn_suffix" {
  description = "ALB arn_suffix for 5xx alarms (empty to skip)."
  type        = string
  default     = ""
}

# Set by: `var.enable_api_gateway ? module.api_gateway[0].api_id : ""` — so ""
# in live/demo and live/staging (both set enable_api_gateway = false).
# Used in: api_5xx `dimensions` (ApiId is the HTTP API / apigatewayv2 dimension).
variable "api_id" {
  description = "API Gateway id for 5xx alarms (empty to skip)."
  type        = string
  default     = ""
}

# Set by: var.alarm_email in careroute_stack (default ""; neither live/demo nor
# live/staging sets it, so by default NO email subscription is created).
# Used in: local.email_sub -> `count` of the SNS subscription, and its endpoint.
variable "alarm_email" {
  description = "Optional email subscribed to the alerts SNS topic."
  type        = string
  default     = ""
}

# Percent CPU (service average) that trips backend_cpu. Default-only: the
# careroute_stack module block does not pass it, so it is always 80.
# Used in: aws_cloudwatch_metric_alarm.backend_cpu.threshold.
variable "cpu_alarm_threshold" {
  type    = number
  default = 80
}

# region: e.g. "ap-southeast-1". Set by: var.region <- root.hcl `inputs`
#   <- live/<env>/env.hcl aws_region. Used in: only the dashboard widgets'
#   `region` property (alarms/topics use the provider's region implicitly).
variable "region" { type = string }

# Set by: module.ecs_cluster.log_group_name (the one group every container logs
# to). Used in: local.log_groups -> log_group_names of the saved queries.
variable "log_group_name" {
  description = "The single ECS log group every task writes to. Saved Logs Insights queries are scoped to it."
  type        = string
}

# Set by: var.enable_saved_log_queries (default true; no live env overrides it).
# Used in: `for_each` of aws_cloudwatch_query_definition.this.
variable "enable_saved_queries" {
  description = "Create the saved Logs Insights queries. Free — a saved query is metadata, and running one is billed per GB scanned whether it was saved or typed."
  type        = bool
  default     = true
}

# ---- Application (Prometheus -> EMF) metrics ------------------------------- #
# Set by: var.enable_app_metrics (live/demo false, live/staging true). The same
# variable adds the ADOT sidecar to the backend, so metrics + alarms move together.
# Used in: local.app_alarms -> `count` of the five app alarms + app_widgets.
variable "enable_app_metrics" {
  description = "Alarm + graph the app's own Prometheus metrics, published to CloudWatch by the ADOT sidecar. Requires the sidecar to be enabled on the backend service."
  type        = bool
  default     = false
}

# A CloudWatch "namespace" is the top-level folder custom metrics live under
# (AWS services use AWS/ECS, AWS/ApplicationELB, ...).
# Set by: var.app_metrics_namespace (default "CareRoute/App", not overridden in
# live/). careroute_stack feeds the SAME var into the ADOT awsemf exporter
# (local.otel_config), which is what keeps writer and alarms in agreement.
# Used in: `namespace` of every app alarm and the app dashboard widgets.
variable "app_metrics_namespace" {
  description = "CloudWatch namespace the ADOT awsemf exporter writes to. Must match the collector config in careroute_stack."
  type        = string
  default     = "CareRoute/App"
}

# Set by: local.app_metrics_service_label = "careroute-backend" in careroute_stack
# (that same local is the scrape label in the collector config).
# Used in: local.app_dims (alarm dimensions) and the app widgets' metric arrays.
variable "app_metrics_service_label" {
  description = "Value of the `service` label the collector attaches to every scraped series; it becomes the CloudWatch dimension. Must match monitoring/prometheus.yml in the app repo so local and cloud dashboards read the same."
  type        = string
  default     = "careroute-backend"
}

# Default-only (not passed by careroute_stack). Used in: inference_latency.threshold.
variable "inference_latency_threshold_seconds" {
  description = "Mean severity-model inference latency (incl. SHAP) that trips the alarm. alert.rules.yml uses p95 > 1.5s; the mean equivalent is set lower because a mean is a weaker signal than a p95."
  type        = number
  default     = 1.0
}

# Default-only. Used in: escalation_rate.threshold (0.6 = 60% of requests).
variable "escalation_rate_threshold" {
  description = "Share of triage requests escalating to a clinician that indicates drift/degraded confidence. Mirrors EscalationRateAnomaly (>0.6)."
  type        = number
  default     = 0.6
}

# Default-only. Used in: guardrail_blocks.threshold.
variable "guardrail_block_threshold" {
  description = "Guardrail blocks in a 5-minute window that indicate attack probing. alert.rules.yml uses >0.2/s; 0.2/s x 300s = 60."
  type        = number
  default     = 60
}

# Set by: local.common_tags in careroute_stack (var.tags + Project/Environment/
# ManagedBy). Used in: the SNS topic and every alarm (dashboards and saved
# queries do not accept tags).
variable "tags" {
  type    = map(string)
  default = {}
}

# [TF] `locals` = named values computed inside this module (not inputs), read as
#      local.<name>. Here they turn inputs into the booleans used by `count`.
#      `var.alarm_email != ""` is itself an expression that yields true/false.
locals {
  alb_alarms = var.alb_arn_suffix != ""
  api_alarms = var.api_id != ""
  email_sub  = var.alarm_email != ""
  app_alarms = var.enable_app_metrics

  # Every app metric carries the `service` dimension (set by the collector's
  # scrape config), so alarms address exactly one deployment's backend.
  app_dims = { service = var.app_metrics_service_label }
}

# --------------------------------------------------------------------------- #
# Alert channel
# --------------------------------------------------------------------------- #
# [TF] `resource "TYPE" "NAME"`: TYPE picks the AWS API (here an SNS standard
#      topic), NAME is the local handle. Other blocks refer to it as
#      aws_sns_topic.alerts.<attribute>; .arn is only known after apply.
# The alert channel: every alarm below publishes to it via
# alarm_actions/ok_actions = [aws_sns_topic.alerts.arn]. Its ARN is output as
# sns_topic_arn -> careroute_stack output `alerts_sns_topic_arn`.
resource "aws_sns_topic" "alerts" {
  #checkov:skip=CKV_AWS_26:SSE with the AWS-managed alias/aws/sns key would BREAK alerting: CloudWatch alarms cannot publish to a topic encrypted with it (its key policy cannot grant cloudwatch.amazonaws.com). Needs a CMK (~$1/month + key policy); topic carries alarm metadata only, no PHI.
  name = "${var.name_prefix}-alerts"
  tags = var.tags
}

# Email subscriber for the topic. SNS emails a confirmation link first; the
# subscription stays "PendingConfirmation" until someone clicks it.
# [TF] `count = cond ? 1 : 0` is the idiom for an OPTIONAL resource: `a ? b : c`
#      is the ternary operator, count 0 creates nothing. With count, the resource
#      becomes a list and is addressed as aws_sns_topic_subscription.email[0].
resource "aws_sns_topic_subscription" "email" {
  count     = local.email_sub ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# --------------------------------------------------------------------------- #
# Alarms
# --------------------------------------------------------------------------- #
# Backend CPU sustained high -> capacity / latency risk on the triage pipeline.
# CloudWatch metric alarm on the backend ECS service's CPU.
#   AWS/ECS CPUUtilization = the service's average CPU % of its reserved units.
#   period 60 + statistic Average = one datapoint per minute (mean of the minute).
#   evaluation_periods 3 = ALARM only after 3 consecutive breaching datapoints.
#   dimensions = which one service this metric series belongs to.
#   alarm_actions fire on entering ALARM, ok_actions on returning to OK.
#   treat_missing_data is unset -> default "missing" (state is left unchanged).
resource "aws_cloudwatch_metric_alarm" "backend_cpu" {
  alarm_name          = "${var.name_prefix}-backend-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Average"
  threshold           = var.cpu_alarm_threshold
  alarm_description   = "Backend/Symptom-Intake CPU above threshold for 3 minutes"
  dimensions = {
    ClusterName = var.cluster_name
    ServiceName = var.backend_service_name
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
  tags          = var.tags
}

# ALB 5xx from the targets (backend errors reaching patients).
# HTTPCode_Target_5XX_Count = 5xx returned BY the targets (the app). 5xx the ALB
# generates itself (no healthy target, timeouts) is HTTPCode_ELB_5XX_Count.
# Sum > 5 per minute, 2 minutes running. treat_missing_data "notBreaching": the
# ALB publishes no datapoint for a minute with zero errors, so missing = healthy.
resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  count               = local.alb_alarms ? 1 : 0
  alarm_name          = "${var.name_prefix}-alb-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = var.alb_arn_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# API Gateway 5xx (edge errors).
# Same shape for the API Gateway HTTP API ("5xx" metric, ApiId dimension).
# Only exists when enable_api_gateway = true.
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  count               = local.api_alarms ? 1 : 0
  alarm_name          = "${var.name_prefix}-apigw-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "5xx"
  namespace           = "AWS/ApiGateway"
  period              = 60
  statistic           = "Sum"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  dimensions          = { ApiId = var.api_id }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# --------------------------------------------------------------------------- #
# Application alarms — the CloudWatch translation of the app repo's
# monitoring/alert.rules.yml. Each one names the rule it mirrors so the two
# files can be diffed when either changes.
# --------------------------------------------------------------------------- #

# Mirrors BackendMetricsDown (up{job="careroute-backend"} == 0).
# careroute_model_info is a Gauge the app publishes on every scrape regardless
# of traffic, so ITS absence — not a drop in request volume — is what means
# "nobody is scraping the triage API". treat_missing_data = "breaching" is the
# whole point of using this series: a quiet night must not look like an outage,
# but a dead scrape must not look like a quiet night either.
# Custom metric in var.app_metrics_namespace, published by the ADOT sidecar.
# LessThanThreshold 1 with statistic Maximum: "no datapoint of value >= 1 in the
# minute". The first of five alarms that all share count = local.app_alarms.
resource "aws_cloudwatch_metric_alarm" "app_metrics_down" {
  count               = local.app_alarms ? 1 : 0
  alarm_name          = "${var.name_prefix}-app-metrics-down"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "careroute_model_info"
  namespace           = var.app_metrics_namespace
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_description   = "No CareRoute /metrics scrape for 3 minutes — the backend or its ADOT collector is down (mirrors BackendMetricsDown)."
  dimensions          = local.app_dims
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# Mirrors HighInferenceLatencyP95 — see the module header: EMF gives a
# StatisticSet, so this alarms on the MEAN, not the p95.
# Mean (Average) latency per minute, 5 consecutive minutes over threshold.
resource "aws_cloudwatch_metric_alarm" "inference_latency" {
  count               = local.app_alarms ? 1 : 0
  alarm_name          = "${var.name_prefix}-inference-latency-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 5
  metric_name         = "careroute_model_predict_seconds"
  namespace           = var.app_metrics_namespace
  period              = 60
  statistic           = "Average"
  threshold           = var.inference_latency_threshold_seconds
  treat_missing_data  = "notBreaching"
  alarm_description   = "Mean severity-model inference latency (incl. SHAP) above threshold for 5 minutes (mean proxy for alert.rules.yml's p95 rule)."
  dimensions          = local.app_dims
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# Mirrors GuardrailBlockSpike — [AI-Security] a burst of blocked inputs is
# prompt-injection/evasion probing or a broken client, not normal traffic.
# Sum of blocks over one 5-minute period (period 300, evaluation_periods 1).
resource "aws_cloudwatch_metric_alarm" "guardrail_blocks" {
  count               = local.app_alarms ? 1 : 0
  alarm_name          = "${var.name_prefix}-guardrail-block-spike"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "careroute_guardrail_blocks_total"
  namespace           = var.app_metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = var.guardrail_block_threshold
  treat_missing_data  = "notBreaching"
  alarm_description   = "Spike in guardrail-blocked triage inputs — possible attack probing."
  dimensions          = local.app_dims
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# Mirrors EscalationRateAnomaly — escalations / triage requests, via metric
# math. clamp_min in the PromQL rule exists to avoid 0/0; here the IF guards it,
# and a window with no traffic evaluates to 0 rather than to a false alarm.
# No metric_name/namespace at the top level: a metric-MATH alarm instead
# evaluates the `metric_query` whose return_data = true ("rate"). The other two
# queries fetch raw series; their `id`s are the variable names in `expression`.
# [TF] `metric_query { }` is a nested block; repeating it three times creates a
#      list of three. `metric { }` inside is a further nested block.
resource "aws_cloudwatch_metric_alarm" "escalation_rate" {
  count               = local.app_alarms ? 1 : 0
  alarm_name          = "${var.name_prefix}-escalation-rate-anomaly"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = var.escalation_rate_threshold
  treat_missing_data  = "notBreaching"
  alarm_description   = "Human-escalation share of triage requests anomalously high — check the severity model for drift."
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags

  metric_query {
    id          = "rate"
    expression  = "IF(requests > 0, escalations / requests, 0)"
    label       = "Escalation share of triage requests"
    return_data = true
  }

  metric_query {
    id = "escalations"
    metric {
      metric_name = "careroute_escalations_total"
      namespace   = var.app_metrics_namespace
      period      = 900
      stat        = "Sum"
      dimensions  = local.app_dims
    }
  }

  metric_query {
    id = "requests"
    metric {
      metric_name = "careroute_triage_requests_total"
      namespace   = var.app_metrics_namespace
      period      = 900
      stat        = "Sum"
      dimensions  = local.app_dims
    }
  }
}

# No PromQL equivalent — this one is specific to being internet-facing.
# [AI-Security] LLM10: sustained 429s mean either an abusive client or a rate
# limit set below legitimate demand. Both need a human to look.
# Threshold 50 is hardcoded here (no variable).
resource "aws_cloudwatch_metric_alarm" "rate_limited" {
  count               = local.app_alarms ? 1 : 0
  alarm_name          = "${var.name_prefix}-rate-limited-burst"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "careroute_rate_limited_total"
  namespace           = var.app_metrics_namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 50
  treat_missing_data  = "notBreaching"
  alarm_description   = "Sustained 429s from the triage rate limiter — abusive client, or the limit is below real demand."
  dimensions          = local.app_dims
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# --------------------------------------------------------------------------- #
# Dashboard — a single pane for triage-service health.
# --------------------------------------------------------------------------- #
# [TF] Several `locals` blocks in one file are fine; they all merge into one
#      `local.` namespace.
locals {
  # Widgets are held as JSON STRINGS rather than as HCL objects. A CloudWatch
  # widget's `metrics` array is genuinely heterogeneous — some entries are
  # ["Namespace","MetricName","Dim","Value"] string arrays, others are
  # {expression=...} objects — and HCL cannot unify those two shapes across a
  # conditional, so `cond ? [widgets...] : []` fails to type-check. Encoding
  # each widget separately sidesteps the unification entirely; the array is
  # reassembled below.

  # Row 0 — infrastructure health (always present).
  # [TF] `[for w in LIST : EXPR]` is a `for` expression: it builds a new list by
  #      evaluating EXPR for every element — here jsonencode(w), which turns an
  #      HCL object into a JSON string. x/y/width/height place the widget on
  #      CloudWatch's 24-column dashboard grid.
  infra_widgets = [for w in [
    {
      type = "metric", x = 0, y = 0, width = 12, height = 6,
      properties = {
        title  = "Backend CPU / Memory",
        region = var.region,
        metrics = [
          ["AWS/ECS", "CPUUtilization", "ClusterName", var.cluster_name, "ServiceName", var.backend_service_name],
          ["AWS/ECS", "MemoryUtilization", "ClusterName", var.cluster_name, "ServiceName", var.backend_service_name]
        ],
        period = 60, stat = "Average"
      }
    },
    {
      type = "metric", x = 12, y = 0, width = 12, height = 6,
      properties = {
        title  = "ALB request count & 5xx",
        region = var.region,
        metrics = local.alb_alarms ? [
          ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", var.alb_arn_suffix],
          ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", var.alb_arn_suffix]
        ] : [],
        period = 60, stat = "Sum"
      }
    }
  ] : jsonencode(w)]

  # Rows 1-2 — the clinical/ML signals, i.e. the CloudWatch rendering of the
  # Grafana "triage observability" dashboard the app repo ships. Only rendered
  # when the ADOT sidecar is actually publishing them.
  # Same pattern wrapped in a ternary: app_alarms false -> empty list, no rows.
  app_widgets = local.app_alarms ? [for w in [
    {
      type = "metric", x = 0, y = 6, width = 12, height = 6,
      properties = {
        title  = "Triage throughput / escalations / guardrail blocks",
        region = var.region,
        metrics = [
          [var.app_metrics_namespace, "careroute_triage_requests_total", "service", var.app_metrics_service_label],
          [var.app_metrics_namespace, "careroute_escalations_total", "service", var.app_metrics_service_label],
          [var.app_metrics_namespace, "careroute_guardrail_blocks_total", "service", var.app_metrics_service_label],
          [var.app_metrics_namespace, "careroute_rate_limited_total", "service", var.app_metrics_service_label]
        ],
        period = 300, stat = "Sum"
      }
    },
    {
      type = "metric", x = 12, y = 6, width = 12, height = 6,
      properties = {
        title  = "Model inference latency (mean) & served confidence",
        region = var.region,
        metrics = [
          [var.app_metrics_namespace, "careroute_model_predict_seconds", "service", var.app_metrics_service_label],
          [var.app_metrics_namespace, "careroute_model_confidence", "service", var.app_metrics_service_label]
        ],
        period = 60, stat = "Average"
      }
    },
    {
      type = "metric", x = 0, y = 12, width = 12, height = 6,
      properties = {
        title  = "Per-agent step duration (mean, by agent)",
        region = var.region,
        # The collector publishes one series per (agent, source) pair; the
        # search expression picks them all up without this file having to list
        # the agent names, which change as the pipeline grows.
        metrics = [
          [{ expression = "SEARCH('{${var.app_metrics_namespace},agent,service} MetricName=\"careroute_agent_duration_seconds\"', 'Average', 60)", id = "agents" }]
        ],
        period = 60
      }
    },
    {
      type = "metric", x = 12, y = 12, width = 12, height = 6,
      properties = {
        title  = "[HITL] Clinician decisions by model agreement",
        region = var.region,
        metrics = [
          [{ expression = "SEARCH('{${var.app_metrics_namespace},agreement,service} MetricName=\"careroute_hitl_decisions_total\"', 'Sum', 300)", id = "hitl" }]
        ],
        period = 300
      }
    }
  ] : jsonencode(w)] : []
}

# The CloudWatch dashboard "<prefix>-triage". dashboard_body must be one JSON
# document. [TF] concat() joins lists, join(",", list) makes one string,
# jsondecode() parses JSON text back into a value, and jsonencode() re-emits it.
# Its name is output as dashboard_name -> careroute_stack output
# `cloudwatch_dashboard`.
resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = "${var.name_prefix}-triage"
  # Both halves are lists of JSON strings, so concat is well-typed; jsondecode
  # turns the reassembled array back into a value jsonencode can emit, which
  # also means a malformed widget fails at plan time rather than in the console.
  dashboard_body = jsonencode({
    widgets = jsondecode("[${join(",", concat(local.infra_widgets, local.app_widgets))}]")
  })
}

# --------------------------------------------------------------------------- #
# Saved Logs Insights queries — the query half of "aggregated logging".
# -----------------------------------------------------------------------------
# The app vault records Lecture 03's central-log-server requirement as an open
# gap, citing ELK. On AWS the store already exists: every container in every task
# writes to ONE CloudWatch log group (modules/ecs_cluster), which is the
# aggregation the slide asks for. What was missing is the half that makes an
# aggregate useful — a way to ask it questions.
#
# app/correlation.py stamps every log line with correlation_id and case_id
# precisely so the lines can be pivoted on afterwards. Nothing in AWS knew that,
# so the key existed and no query used it. These are free (a saved query is
# metadata; only running one costs, and scanning is billed per GB either way).
#
# LLMSecOps p116 (Pillar 3: Audit Logging) lists the fields an audit trail should
# be able to answer on: identity, timestamp, authorisation decisions, latency,
# model version. The queries below are those questions.
# --------------------------------------------------------------------------- #
locals {
  log_groups = [var.log_group_name]

  # [TF] A map literal { key = value }. Each value is a heredoc string:
  #      `<<-EOQ ... EOQ` is multi-line text ("-" strips the common indentation).
  #      The text is Logs Insights query language, not HCL.
  queries = {
    "01-trace-one-case" = <<-EOQ
      fields @timestamp, correlation_id, case_id, @message
      | filter case_id = "REPLACE_WITH_CASE_ID"
      | sort @timestamp asc
      | limit 200
    EOQ

    "02-trace-one-request" = <<-EOQ
      fields @timestamp, correlation_id, @logStream, @message
      | filter correlation_id = "REPLACE_WITH_CORRELATION_ID"
      | sort @timestamp asc
      | limit 200
    EOQ

    # An error with no correlation id is the interesting case, not the boring
    # one: it means the failure happened outside a request the app was tracking.
    "03-errors-by-correlation" = <<-EOQ
      fields @timestamp, correlation_id, case_id, @message
      | filter @message like /(?i)(error|exception|traceback)/
      | stats count() as hits by correlation_id
      | sort hits desc
      | limit 50
    EOQ

    "04-guardrail-blocks" = <<-EOQ
      fields @timestamp, correlation_id, case_id, @message
      | filter @message like /guardrail/ and @message like /(?i)block/
      | sort @timestamp desc
      | limit 100
    EOQ

    # Cross-container: the shipper and the collector log to the same group under
    # different stream prefixes, so a task that is quietly failing to archive its
    # drift evidence shows up here and nowhere else.
    "05-sidecar-failures" = <<-EOQ
      fields @timestamp, @logStream, @message
      | filter @logStream like /(telemetry|otel|drift-monitor)/
      | filter @message like /(?i)(error|denied|failed|fatal)/
      | sort @timestamp desc
      | limit 100
    EOQ
  }
}

# Saved query definitions (CloudWatch > Logs Insights > Saved queries). The "/"
# in `name` shows as a folder per environment in the console.
# [TF] `for_each` over a map creates ONE instance per key; each.key is the map
#      key (query name) and each.value the value (query text). Instances are
#      addressed aws_cloudwatch_query_definition.this["01-trace-one-case"].
#      An empty map ({}) creates none — the for_each form of "optional".
resource "aws_cloudwatch_query_definition" "this" {
  for_each        = var.enable_saved_queries ? local.queries : {}
  name            = "${var.name_prefix}/${each.key}"
  log_group_names = local.log_groups
  query_string    = each.value
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# [TF] `output` = a value this module returns to its caller, read there as
#      module.observability.<name>. Both are re-exported by
#      modules/careroute_stack/outputs.tf (alerts_sns_topic_arn,
#      cloudwatch_dashboard), so `terragrunt output` shows them.
output "sns_topic_arn" { value = aws_sns_topic.alerts.arn }
output "dashboard_name" { value = aws_cloudwatch_dashboard.this.dashboard_name }
