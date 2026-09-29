###############################################################################
# Module: alb
# -----------------------------------------------------------------------------
# An *internal* Application Load Balancer that sits behind API Gateway and in
# front of the ECS services. It owns the HTTP routing shown in the cloud diagram:
#
#     API Gateway ─(VPC link)─▶ ALB :80
#                                 ├── /api/*  ─▶ backend target group  (:8000 FastAPI)
#                                 └── /*      ─▶ frontend target group (:3000 Next.js server)
#
# Target type is "ip" because Fargate tasks are registered by ENI IP, not by
# EC2 instance. Health checks let the ALB drain unhealthy agent tasks.
#
# The /api/* rule is LOAD-BEARING, not a nicety: the browser calls /api
# same-origin, and the frontend image's own Next.js `rewrites()` fallback is
# baked at BUILD time (frontend/Dockerfile ARG BACKEND_URL, default
# http://backend:8000 — a compose hostname that does not resolve in the VPC).
# If this rule is removed or out-prioritised, every /api call reaches the Next
# server, which proxies it to a nonexistent host.
#
# POST /api/triage/stream is Server-Sent Events held open for a whole triage
# run, so `idle_timeout` below is a correctness setting, not a tuning knob.
###############################################################################

# [TF] Module inputs (var.<name>), set by module "alb" in modules/careroute_stack/main.tf;
#      unset ones use `default`.
# Set by: careroute_stack/main.tf (module "alb") local.name_prefix. Used in: ALB + target-group names
# (both capped at 32 characters by AWS).
variable "name_prefix" { type = string }
# Set by: careroute_stack/main.tf (module "alb") module.networking.vpc_id. Used in: the target groups.
variable "vpc_id" { type = string }

# Set by: careroute_stack/main.tf (module "alb") - PUBLIC subnets when alb_internal = false (demo, staging),
# private ones otherwise (the description above assumes internal).
# Used in: aws_lb.this subnets (an ALB needs subnets in at least two AZs).
variable "subnet_ids" {
  description = "Subnets the ALB nodes live in (private, since it is internal)."
  type        = list(string)
}

# Set by: careroute_stack/main.tf (module "alb") [module.security.alb_sg_id]. Used in: aws_lb.this.
variable "security_group_ids" {
  type = list(string)
}

# Set by: careroute_stack/main.tf (module "alb") var.alb_internal (false in demo/staging -> internet-facing,
# public DNS name). Used in: aws_lb.this.
variable "internal" {
  description = "true = internal ALB (reached only via API Gateway); false = internet-facing."
  type        = bool
  default     = true
}

# Set by: default only (matches careroute_stack/main.tf (module "frontend") container_port). Used in:
# aws_lb_target_group.frontend port.
variable "frontend_port" {
  description = "Port the frontend container listens on (Next.js standalone server)."
  type        = number
  default     = 3000
}

# Set by: default only (matches careroute_stack/main.tf (module "backend") container_port). Used in:
# aws_lb_target_group.backend port.
variable "backend_port" {
  type    = number
  default = 8000
}

# Set by: default only. Used in: aws_lb_listener_rule.backend_api condition.
variable "backend_path_pattern" {
  description = "Path prefix routed to the backend/Symptom-Intake service."
  type        = string
  default     = "/api/*"
}

# Set by: default only. Used in: aws_lb_target_group.frontend health_check.
variable "frontend_health_check_path" {
  type    = string
  default = "/"
}

# Set by: default only. Used in: aws_lb_target_group.backend health_check.
variable "backend_health_check_path" {
  description = "FastAPI health endpoint used by the ALB target group."
  type        = string
  default     = "/api/health"
}

# Set by: careroute_stack/main.tf (module "alb") var.enable_prometheus_stack. Used in: count on
# aws_lb_target_group.grafana and aws_lb_listener_rule.grafana.
variable "enable_grafana_route" {
  description = "Add a target group + listener rule so Grafana is reachable at /grafana on the same ALB. Only useful with the monitoring stack deployed."
  type        = bool
  default     = false
}

# Grafana's container port (monitoring_stack). Set by: default only.
# Used in: aws_lb_target_group.grafana.
variable "grafana_port" {
  type    = number
  default = 3000
}

# Set by: default only. Used in: aws_lb_listener_rule.grafana + its health check path.
variable "grafana_path_pattern" {
  description = "Path prefix routed to Grafana. Grafana is told to serve from this sub-path (GF_SERVER_SERVE_FROM_SUB_PATH), so its own health endpoint lives under it too."
  type        = string
  default     = "/grafana*"
}

# Set by: careroute_stack/main.tf (module "alb") var.alb_idle_timeout. Used in: aws_lb.this.
variable "idle_timeout" {
  description = <<-EOT
    Seconds the ALB keeps an idle connection open. MUST exceed the longest gap
    between bytes on POST /api/triage/stream — a Server-Sent Events response the
    browser holds open for the whole triage run. The app emits an SSE comment
    frame every CAREROUTE_SSE_HEARTBEAT_SECONDS (10 s) while the pipeline is
    silent, and the measured worst-case silent stretch (Care Routing's OneMap
    shortlist, the red-flag LLM path) is 24-30 s. 120 s leaves headroom if the
    heartbeat is ever turned off; the AWS default of 60 s would cut a stalled
    run mid-stream and the UI would hang on "Triaging...".
  EOT
  type        = number
  default     = 120
}

# Set by: default only. Used in: all three target groups.
variable "deregistration_delay" {
  description = "Seconds to drain in-flight requests before killing a target. Long-lived SSE streams need more than the 300 s default is generous for, but a Spot task replacement should not hold a deploy for 5 minutes."
  type        = number
  default     = 60
}

# Set by: default only. Used in: aws_lb_target_group.backend.
variable "backend_load_balancing_algorithm" {
  description = <<-EOT
    How the ALB picks a backend task. "least_outstanding_requests" sends each
    new request to the task with the fewest requests in flight, which is what a
    service of long-held SSE streams needs once it has more than one task:
    round robin counts requests, not how long they stay open, so it keeps
    handing new triage runs to a task already busy with 30-second ones.
    With one task it changes nothing, so it is the default rather than a toggle
    to remember on the day the backend scales out.
  EOT
  type        = string
  default     = "least_outstanding_requests"

  validation {
    condition     = contains(["round_robin", "least_outstanding_requests"], var.backend_load_balancing_algorithm)
    error_message = "backend_load_balancing_algorithm must be round_robin or least_outstanding_requests."
  }
}

# Set by: careroute_stack/main.tf (module "alb") var.acm_certificate_arn (empty in demo/staging).
# Used in: local.tls_enabled and aws_lb_listener.https.
variable "certificate_arn" {
  description = <<-EOT
    ACM certificate ARN. Set it and the ALB gains an HTTPS :443 listener
    carrying all the routing, while :80 becomes a permanent redirect to it.
    Leave empty (the default) and the ALB stays HTTP-only, which is what the
    throwaway demo runs.

    This is the largest security gap in the stack while it is empty. LLMSecOps
    p117 lists "encrypt in transit (TLS)" in its wrap-up checklist, and Checkov
    flags the absence three ways (CKV_AWS_2, CKV2_AWS_20, CKV_AWS_103). The
    fourth it used to be counted with, CKV_AWS_378, is about the ALB-to-task hop
    and is NOT fixed by a certificate, so it is skipped inline on the target
    groups instead. It is empty only because a certificate needs a domain name to
    be issued against, and the demo has none — not because HTTP was judged good
    enough for a healthcare app.

    Getting one: register/own a domain, then in ACM request a public certificate
    in THIS region (an ALB cannot use a us-east-1 certificate the way CloudFront
    can), validate by DNS, and pass the ARN here. Point the domain's record at
    `alb_dns_name`.
  EOT
  type        = string
  default     = ""
}

# Allowed TLS versions/ciphers. Set by: careroute_stack/main.tf (module "alb") var.alb_ssl_policy.
# Used in: aws_lb_listener.https.
variable "ssl_policy" {
  description = "ELB security policy for the HTTPS listener. The TLS13 policy is the current AWS recommendation and excludes everything below TLS 1.2."
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-2021-06"
}

# Set by: careroute_stack/main.tf (module "alb") - module.artifacts[0].bucket_name when BOTH
# enable_alb_access_logs and enable_artifacts_bucket are true (staging), else "".
# Used in: the dynamic "access_logs" block on aws_lb.this.
variable "access_logs_bucket" {
  description = <<-EOT
    S3 bucket for ALB access logs. Empty (default) disables them.

    Storage is the only charge — ALB access logging itself is free — so this is
    the cheapest third of LLMSecOps Pillar 3 (audit logging): who called what,
    when, from where, with what response. The stack points it at the artifacts
    bucket when that is enabled.

    NOTE: the bucket policy must allow the regional ELB log-delivery principal
    to PutObject, or the ALB silently writes nothing. modules/artifacts adds
    that statement when it is told it is a log destination.
  EOT
  type        = string
  default     = ""
}

# Set by: default only. Must match the prefix the artifacts bucket policy allows
# (careroute_stack/main.tf passes "alb-access-logs" to module "artifacts").
variable "access_logs_prefix" {
  type    = string
  default = "alb-access-logs"
}

# Set by: careroute_stack/main.tf (module "alb") local.staff_proxy_paths.
# Used in: aws_lb_listener_rule.frontend_proxied (count + path values).
variable "frontend_proxied_paths" {
  description = <<-EOT
    /api paths that must reach the FRONTEND, not the backend, despite matching
    the /api/* rule: the escalation endpoints, whose Next.js Route Handler
    attaches the staff API key server-side. Sent straight to the backend they
    arrive keyless and get 401. Up to 5 patterns (one ALB condition). Empty =
    no rule.
  EOT
  type        = list(string)
  default     = []
}

# Set by: careroute_stack/main.tf (module "alb") local.common_tags.
variable "tags" {
  type    = map(string)
  default = {}
}

locals {
  # One place decides whether TLS exists, because four resources depend on it.
  tls_enabled = var.certificate_arn != ""

  # Listener that carries the path rules: HTTPS when there is one, else HTTP.
  # Attaching a rule to the redirect listener would route nothing, because a
  # redirect default action fires before any forward rule can match.
  # [TF] aws_lb_listener.https is counted (0 or 1 copies); [0] is its only copy,
  #      and this branch of the ternary is only chosen when it exists.
  routing_listener_arn = local.tls_enabled ? aws_lb_listener.https[0].arn : aws_lb_listener.http.arn
}

# The Application Load Balancer (console: EC2 > Load Balancers). Its outputs
# alb_dns_name -> careroute_stack/main.tf (locals) local.alb_origin + app_url, alb_arn_suffix ->
# careroute_stack/main.tf module "observability" (CloudWatch alarms).
resource "aws_lb" "this" {
  name               = "${var.name_prefix}-alb"
  internal           = var.internal
  load_balancer_type = "application"
  subnets            = var.subnet_ids
  security_groups    = var.security_group_ids
  idle_timeout       = var.idle_timeout

  # Strip malformed headers instead of forwarding them to the tasks.
  #
  # This is load-bearing for THIS app, not generic hardening. The backend is
  # configured to believe X-Forwarded-For from the ALB's subnets
  # (CAREROUTE_TRUSTED_PROXIES — see careroute_stack), because otherwise every
  # client shares one rate-limit bucket. That trust is only safe if what arrives
  # at the task is a header the ALB actually produced: with this false, a caller
  # can send a malformed or duplicated XFF and have it forwarded through the
  # trusted path, which is precisely the bypass the trusted-proxy check exists
  # to close. Checkov CKV_AWS_131.
  drop_invalid_header_fields = true

  # [TF] dynamic block with for_each = [] or [1]: emits access_logs 0 or 1 times,
  #      the idiom for an optional nested block. [1] is just a one-item list.
  dynamic "access_logs" {
    for_each = var.access_logs_bucket == "" ? [] : [1]
    content {
      bucket  = var.access_logs_bucket
      prefix  = var.access_logs_prefix
      enabled = true
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-alb" })
}

# --- Frontend target group (default action) -------------------------------- #
# Target group = the pool of targets the ALB forwards to. target_type "ip":
# Fargate (awsvpc) tasks are registered by their ENI IP, not instance ID. The
# ECS service does the registering: output frontend_target_group_arn ->
# careroute_stack/main.tf module "frontend".
resource "aws_lb_target_group" "frontend" {
  #checkov:skip=CKV_AWS_378:ALB-to-task hop stays inside the VPC; TLS terminates at the ALB (see var.certificate_arn). Re-encrypting to the task would need a cert in every container. Not fixed by a certificate either, so it is not part of the listener TLS gap.
  name                 = "${var.name_prefix}-fe-tg"
  port                 = var.frontend_port
  protocol             = "HTTP"
  vpc_id               = var.vpc_id
  target_type          = "ip"
  deregistration_delay = var.deregistration_delay

  # The ALB polls `path` every 30 s: 2 passes -> healthy, 3 fails -> unhealthy.
  # matcher = which HTTP status codes count as a pass.
  health_check {
    path                = var.frontend_health_check_path
    matcher             = "200-399"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-fe-tg" })
}

# --- Backend target group (path-routed) ------------------------------------ #
# Same for FastAPI. Output backend_target_group_arn -> careroute_stack/main.tf module "backend".
resource "aws_lb_target_group" "backend" {
  #checkov:skip=CKV_AWS_378:ALB-to-task hop stays inside the VPC; TLS terminates at the ALB (see var.certificate_arn). Re-encrypting to the task would need a cert in every container. Not fixed by a certificate either, so it is not part of the listener TLS gap.
  name                 = "${var.name_prefix}-be-tg"
  port                 = var.backend_port
  protocol             = "HTTP"
  vpc_id               = var.vpc_id
  target_type          = "ip"
  deregistration_delay = var.deregistration_delay

  # See var.backend_load_balancing_algorithm: with long SSE streams, round robin
  # piles new runs onto a task that is already holding many open ones.
  load_balancing_algorithm_type = var.backend_load_balancing_algorithm

  health_check {
    path                = var.backend_health_check_path
    matcher             = "200-399"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-be-tg" })
}

# --- Listeners -------------------------------------------------------------- #
# :80 forwards when there is no certificate, and permanently redirects to :443
# when there is. The redirect is 301 rather than 302 because the scheme upgrade
# is not a temporary condition, and because a browser that caches it stops
# sending the first request in clear text at all.
# A listener = the port + protocol the ALB accepts connections on. The two
# dynamic default_action blocks below have opposite conditions, so exactly one
# is generated.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = local.tls_enabled ? [] : [1]
    content {
      # Everything that is not /api/* renders the SPA.
      type             = "forward"
      target_group_arn = aws_lb_target_group.frontend.arn
    }
  }

  dynamic "default_action" {
    for_each = local.tls_enabled ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

# Only exists with a certificate (count 0 or 1). Carries the path rules then.
resource "aws_lb_listener" "https" {
  count             = local.tls_enabled ? 1 : 0
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.ssl_policy
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}

# Listener rule: paths matching /api/* go to the backend target group. Rules are
# evaluated lowest priority number first; anything unmatched falls through to
# the listener's default_action (the frontend).
resource "aws_lb_listener_rule" "backend_api" {
  listener_arn = local.routing_listener_arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend.arn
  }

  condition {
    path_pattern {
      values = [var.backend_path_pattern]
    }
  }
}

# Priority 5: evaluated BEFORE /api/* (10), so these paths go to the frontend's
# server-side proxy instead of straight to the backend. See frontend_proxied_paths.
resource "aws_lb_listener_rule" "frontend_proxied" {
  count        = length(var.frontend_proxied_paths) > 0 ? 1 : 0
  listener_arn = local.routing_listener_arn
  priority     = 5

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }

  condition {
    path_pattern {
      values = var.frontend_proxied_paths
    }
  }
}

# --- Grafana target group + rule (optional) -------------------------------- #
# Grafana shares the ALB rather than getting its own, so the demo still has a
# single entry point and a single hourly ALB charge.
# Output grafana_target_group_arn -> careroute_stack/main.tf module "monitoring", whose ECS
# service registers the Grafana task in it.
resource "aws_lb_target_group" "grafana" {
  #checkov:skip=CKV_AWS_378:ALB-to-task hop stays inside the VPC; TLS terminates at the ALB (see var.certificate_arn). Re-encrypting to the task would need a cert in every container. Not fixed by a certificate either, so it is not part of the listener TLS gap.
  count                = var.enable_grafana_route ? 1 : 0
  name                 = "${var.name_prefix}-gf-tg"
  port                 = var.grafana_port
  protocol             = "HTTP"
  vpc_id               = var.vpc_id
  target_type          = "ip"
  deregistration_delay = var.deregistration_delay

  health_check {
    # Grafana is configured to serve from the sub-path, so its health endpoint
    # is /grafana/api/health, not /api/health. The ALB health check bypasses the
    # listener rule and hits the target directly, so it must use the full path
    # the container actually answers on.
    # trimsuffix("/grafana*", "*") = "/grafana", so this is /grafana/api/health.
    path                = "${trimsuffix(var.grafana_path_pattern, "*")}/api/health"
    matcher             = "200-399"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-gf-tg" })
}

# priority 20: checked after /api/* (10). The paths don't overlap, so the order
# only makes evaluation deterministic.
resource "aws_lb_listener_rule" "grafana" {
  count        = var.enable_grafana_route ? 1 : 0
  listener_arn = local.routing_listener_arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.grafana[0].arn
  }

  condition {
    path_pattern {
      values = [var.grafana_path_pattern]
    }
  }
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# [TF] Outputs are read in careroute_stack as module.alb.<name>.
# alb_arn: not referenced by careroute_stack today.
output "alb_arn" { value = aws_lb.this.arn }

# -> careroute_stack/main.tf module "monitoring". [TF] null = "no value" when the route is off.
output "grafana_target_group_arn" {
  value = var.enable_grafana_route ? aws_lb_target_group.grafana[0].arn : null
}
# -> careroute_stack/main.tf module "observability" (ALB CloudWatch metrics/alarms).
output "alb_arn_suffix" { value = aws_lb.this.arn_suffix } # for CloudWatch metrics
# -> careroute_stack/main.tf (locals) local.alb_origin (CORS, app URL) and outputs app_url / alb_dns_name.
output "alb_dns_name" { value = aws_lb.this.dns_name }
# -> careroute_stack/main.tf module "api_gateway" alb_listener_arn (VPC-link integration target).
output "http_listener_arn" { value = aws_lb_listener.http.arn }
# Not referenced by careroute_stack today.
output "https_listener_arn" { value = local.tls_enabled ? aws_lb_listener.https[0].arn : null }

# The scheme the app is actually reachable on. The stack builds CORS origins and
# the Grafana root URL from this, so a certificate appearing must not leave the
# app advertising http:// to a browser that was just redirected to https://.
# -> careroute_stack/main.tf (locals) local.alb_origin.
output "url_scheme" { value = local.tls_enabled ? "https" : "http" }
# -> careroute_stack outputs.tf output "tls_enabled".
output "tls_enabled" { value = local.tls_enabled }
# -> careroute_stack/main.tf module "frontend" target_group_arn.
output "frontend_target_group_arn" { value = aws_lb_target_group.frontend.arn }
# -> careroute_stack/main.tf module "backend" target_group_arn.
output "backend_target_group_arn" { value = aws_lb_target_group.backend.arn }
# -> careroute_stack/deploy_safety.tf: the TargetGroup dimension of the
# deployment-rollback alarms (CloudWatch wants the suffix, not the full ARN).
output "backend_target_group_arn_suffix" { value = aws_lb_target_group.backend.arn_suffix }
output "frontend_target_group_arn_suffix" { value = aws_lb_target_group.frontend.arn_suffix }
