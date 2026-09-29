###############################################################################
# Module: security
# -----------------------------------------------------------------------------
# Central place for every Security Group (the stateful, least-privilege firewall
# rules). Keeping them here makes the trust relationships easy to audit:
#
#   Internet ─▶ API Gateway (managed, no SG) ─▶ vpc_link_sg ─▶ alb_sg ─▶ ecs_sg
#                                                                          │
#                                            ecs_sg ─▶ rds_sg    (5432)    │
#                                            ecs_sg ─▶ ecs_sg    (intra)   ◀┘
#
# Everything the patient traffic touches lives in private subnets; only the two
# managed entry points (API Gateway + its VPC link ENIs) face the ALB.
###############################################################################

# [TF] One-line variables: a type and no default = required input (var.<name>).
#      Set by module "security" in modules/careroute_stack/main.tf.
# Set by: careroute_stack/main.tf (module "security") local.name_prefix. Used in: every SG name and Name tag.
variable "name_prefix" { type = string }
# Set by: careroute_stack/main.tf (module "security") module.networking.vpc_id (a networking OUTPUT flowing in here).
# Used in: vpc_id of all five SGs.
variable "vpc_id" { type = string }
# Set by: careroute_stack/main.tf (module "security") module.networking.vpc_cidr. Used in: aws_security_group.alb.
variable "vpc_cidr" { type = string }

# Set by: nobody - careroute_stack does not pass it, so the default applies. Must
# equal module "backend" container_port (careroute_stack/main.tf, 8000).
# Used in: aws_security_group.ecs ingress.
variable "backend_port" {
  description = "Container port for the FastAPI Symptom-Intake service."
  type        = number
  default     = 8000
}

# Set by: default only; must equal module "frontend" container_port (careroute_stack/main.tf).
# Grafana also listens on 3000, which is why the /grafana ALB route reaches the
# monitoring task through this same rule. Used in: aws_security_group.ecs.
variable "frontend_port" {
  description = "Container port for the Next.js frontend server (ALB -> task ingress)."
  type        = number
  default     = 3000
}


# PostgreSQL port. Set by: default only. Used in: aws_security_group.rds.
variable "db_port" {
  type    = number
  default = 5432
}

# Set by: careroute_stack/main.tf (module "security") = !var.alb_internal (demo/staging: true).
# Used in: the dynamic "ingress" block on aws_security_group.alb.
variable "alb_internet_facing" {
  description = "When true (cheapest demo: no API Gateway), the ALB is the public entry, so allow HTTP from the internet directly."
  type        = bool
  default     = false
}

# Set by: careroute_stack/main.tf (module "security") = var.acm_certificate_arn != "" (a true/false expression).
# Used in: the same dynamic "ingress" block (adds port 443).
variable "alb_tls_enabled" {
  description = <<-EOT
    Whether the ALB has an HTTPS listener. Controls the :443 ingress rule.

    It is a separate flag rather than always-on because a security group rule
    for a port nothing listens on is worse than useless: it is a permanently
    open door in every audit report and on every Checkov run, which a reviewer
    then has to chase down to discover it leads nowhere. Open the port when
    something is behind it.
  EOT
  type        = bool
  default     = false
}

# Set by: careroute_stack/main.tf (module "security") local.common_tags. Used in: merged into each SG's tags.
variable "tags" {
  type    = map(string)
  default = {}
}

# --------------------------------------------------------------------------- #
# VPC Link SG — attached to the API Gateway VPC-link ENIs. It only needs to
# reach the ALB, so it has open egress and no ingress.
# --------------------------------------------------------------------------- #
# Created even when API Gateway is off (no count); an unused SG costs nothing.
# Output vpc_link_sg_id -> careroute_stack/main.tf module "api_gateway".
# [TF] ingress / egress are inline rule blocks: Terraform owns the SG's whole
#      rule set, so a rule added by hand in the console is removed next apply.
# [TF] `description` is IMMUTABLE in EC2 — there is no ModifySecurityGroupDescription
#      API, so the provider honours any edit to this string by DESTROYING and
#      recreating the group. This one must stay byte-for-byte equal to the
#      deployed group's description ("... ENIs to internal ALB", spelled out, not
#      an arrow) or every apply plans a replacement that AWS then refuses with
#      DependencyViolation, because the API Gateway VPC link's ENIs still hold it.
#      Prose here is not free. Change it only together with the
#      create_before_destroy + name_prefix pair, never on its own.
resource "aws_security_group" "vpc_link" {
  #checkov:skip=CKV2_AWS_5:Attached in modules/careroute_stack via module.security outputs; Checkov does not resolve cross-module attachment. Also created unconditionally so the API Gateway toggle needs no SG churn; an unattached SG is free and grants nothing.
  name        = "${var.name_prefix}-vpclink-sg"
  description = "API Gateway VPC link ENIs to internal ALB"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name_prefix}-vpclink-sg" })

  egress {
    description = "All outbound (to the ALB)"
    from_port   = 0
    to_port     = 0
    # protocol "-1" = all protocols; from/to port 0 then means all ports.
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --------------------------------------------------------------------------- #
# ALB SG — the internal load balancer. Only the VPC link (API Gateway) and
# other things inside the VPC may talk to it.
# --------------------------------------------------------------------------- #
# Output alb_sg_id -> careroute_stack/main.tf module "alb" security_group_ids. Also the SOURCE
# of the ecs SG's ingress rules below.
resource "aws_security_group" "alb" {
  #checkov:skip=CKV2_AWS_5:Attached in modules/careroute_stack via module.security outputs; Checkov does not resolve cross-module attachment. Consumer: module "alb" security_group_ids.
  name        = "${var.name_prefix}-alb-sg"
  description = "Internal ALB; ingress from API Gateway VPC link + in-VPC clients"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name_prefix}-alb-sg" })

  ingress {
    # security_groups = source is "any ENI carrying that SG" (SG referencing),
    # not an IP range. The next rule uses cidr_blocks = an IP range instead.
    description     = "HTTP from API Gateway VPC link"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.vpc_link.id]
  }

  ingress {
    description = "HTTP from inside the VPC (health checks / internal callers)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # Demo mode: ALB is the public entry point (no API Gateway in front). 443 is
  # opened only when a certificate exists to terminate on it — see
  # var.alb_tls_enabled. With TLS on, :80 still needs to be open: it serves the
  # 301 redirect that gets the browser to :443 in the first place.
  # [TF] A `dynamic "ingress"` block emits one ingress block per element of
  #      for_each; inside `content`, ingress.value is the current element (80 or
  #      443). An empty list [] emits none: how a rule is made optional.
  dynamic "ingress" {
    for_each = var.alb_internet_facing ? (var.alb_tls_enabled ? [80, 443] : [80]) : []
    content {
      description = "HTTP/HTTPS from the internet (public demo ALB)"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  egress {
    description = "All outbound: the ALB must reach the task ENIs on any app port"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --------------------------------------------------------------------------- #
# ECS service SG — every Fargate task (frontend, backend, sub-agents).
# Accepts traffic from the ALB on the app ports and from itself, so sub-agents
# can call each other over the private Cloud Map DNS names when that tier is on.
# The ADOT metrics collector needs no rule at all: it is a sidecar inside the
# backend task, reaching the app over the shared network namespace.
# --------------------------------------------------------------------------- #
# Output ecs_sg_id -> every Fargate workload in careroute_stack/main.tf: monitoring (:697),
# backend (:742), mlflow (:823), drift_monitor (:952), frontend (:990),
# sub_agents (:1026).
resource "aws_security_group" "ecs" {
  #checkov:skip=CKV2_AWS_5:Attached in modules/careroute_stack via module.security outputs; Checkov does not resolve cross-module attachment. Consumers: every Fargate service/task in careroute_stack.
  name        = "${var.name_prefix}-ecs-sg"
  description = "ECS Fargate tasks: ingress from ALB + intra-service"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name_prefix}-ecs-sg" })

  ingress {
    description     = "Frontend port from ALB"
    from_port       = var.frontend_port
    to_port         = var.frontend_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "Backend port from ALB"
    from_port       = var.backend_port
    to_port         = var.backend_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    # self = true: the source is this same SG, i.e. task-to-task traffic (this
    # is also how Prometheus scrapes the backend on :8000).
    description = "Intra-service traffic (agent-to-agent over Cloud Map DNS)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  egress {
    description = "All outbound: ECR image pulls, the hosted LLM API, OneMap, CloudWatch. Unrestricted because these are public endpoints with no stable CIDR; tightening it needs VPC endpoints, which cost more than the demo saves."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --------------------------------------------------------------------------- #
# RDS SG — only the ECS tasks may reach Postgres.
# --------------------------------------------------------------------------- #
# Output rds_sg_id -> careroute_stack/main.tf module "rds" (only created when enable_rds).
resource "aws_security_group" "rds" {
  #checkov:skip=CKV2_AWS_5:Attached in modules/careroute_stack via module.security outputs; Checkov does not resolve cross-module attachment. Consumer: module "rds" (off by default); unattached it is free and grants nothing.
  name        = "${var.name_prefix}-rds-sg"
  description = "RDS Postgres; ingress 5432 from ECS tasks only"
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name_prefix}-rds-sg" })

  ingress {
    description     = "Postgres from ECS tasks"
    from_port       = var.db_port
    to_port         = var.db_port
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  egress {
    description = "All outbound: RDS initiates nothing here, so this is permissive only to keep the demo simple"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# [TF] Outputs are read by the caller as module.security.<name>; the consumer
#      of each one is noted on its SG above.
output "vpc_link_sg_id" { value = aws_security_group.vpc_link.id }
output "alb_sg_id" { value = aws_security_group.alb.id }
output "ecs_sg_id" { value = aws_security_group.ecs.id }
output "rds_sg_id" { value = aws_security_group.rds.id }
