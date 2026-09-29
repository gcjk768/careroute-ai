###############################################################################
# Module: api_gateway
# -----------------------------------------------------------------------------
# The public front door from the cloud diagram: an HTTP API (API Gateway v2)
# doing "routing - auth - rate-limit - logging". It reaches the *internal* ALB
# over a private VPC link, so the ALB and every ECS task stay off the internet.
#
#   Client ─▶ API Gateway (throttle + access logs) ─▶ VPC link ─▶ internal ALB
#
# A JWT authorizer is wired in optionally (disabled by default) to show where
# the proposal's "auth" belongs without forcing an identity provider on the
# prototype.
###############################################################################

# [TF] Module inputs, set by module "api_gateway" in modules/careroute_stack/main.tf. That
#      module block has count = var.enable_api_gateway ? 1 : 0, so in demo and
#      staging (false) none of this file is created, and module.api_gateway is an
#      empty list - which is why the stack reads it as module.api_gateway[0].
# Set by: careroute_stack/main.tf (module "api_gateway") local.name_prefix. Used in: resource names.
variable "name_prefix" { type = string }

# Set by: careroute_stack/main.tf (module "api_gateway") module.alb.http_listener_arn. Used in: integration_uri (an
# HTTP API private integration targets an ALB LISTENER ARN, not a DNS name).
variable "alb_listener_arn" {
  description = "ARN of the internal ALB HTTP listener to proxy all traffic to."
  type        = string
}

# Set by: careroute_stack/main.tf (module "api_gateway") module.networking.private_subnet_ids.
# Used in: aws_apigatewayv2_vpc_link.this.
variable "vpc_link_subnet_ids" {
  description = "Private subnets for the VPC link ENIs."
  type        = list(string)
}

# Set by: careroute_stack/main.tf (module "api_gateway") [module.security.vpc_link_sg_id].
# Used in: aws_apigatewayv2_vpc_link.this.
variable "vpc_link_security_group_ids" {
  type = list(string)
}

# Set by: careroute_stack/main.tf (module "api_gateway") var.api_throttle_rate_limit. Used in: the stage's route settings.
variable "throttle_rate_limit" {
  description = "Steady-state requests/sec (the proposal's rate-limit control)."
  type        = number
  default     = 50
}

# Set by: careroute_stack/main.tf (module "api_gateway") var.api_throttle_burst_limit. Used in: the stage's route settings.
variable "throttle_burst_limit" {
  description = "Max burst requests."
  type        = number
  default     = 100
}

# Set by: careroute_stack/main.tf (module "api_gateway") var.log_retention_days. Used in: aws_cloudwatch_log_group.access.
variable "log_retention_days" {
  type    = number
  default = 30
}

# Optional JWT auth (e.g. Cognito / Auth0). Leave issuer empty to disable.
# Set by: nobody - careroute_stack passes neither jwt_issuer nor jwt_audiences,
# so JWT auth is always off unless they are added to module "api_gateway".
# Used in: local.jwt_enabled and aws_apigatewayv2_authorizer.jwt.
variable "jwt_issuer" {
  type    = string
  default = ""
}
variable "jwt_audiences" {
  type    = list(string)
  default = []
}

# Set by: careroute_stack/main.tf (module "api_gateway") local.common_tags.
variable "tags" {
  type    = map(string)
  default = {}
}

locals {
  jwt_enabled = var.jwt_issuer != ""
}

# --------------------------------------------------------------------------- #
# HTTP API + VPC link to the internal ALB
# --------------------------------------------------------------------------- #
# The HTTP API (API Gateway v2: cheaper and simpler than a v1 REST API). Its id
# is used by the integration, authorizer, route and stage below; output api_id
# -> careroute_stack/main.tf module "observability".
resource "aws_apigatewayv2_api" "this" {
  name          = "${var.name_prefix}-http-api"
  protocol_type = "HTTP"

  # API Gateway answers CORS preflight (OPTIONS) itself with these settings.
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["content-type", "authorization"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-http-api" })
}

# VPC link (v2): API-Gateway-managed ENIs in the private subnets, carrying the
# vpc_link SG, so the managed service can reach the ALB inside the VPC.
resource "aws_apigatewayv2_vpc_link" "this" {
  name               = "${var.name_prefix}-vpclink"
  subnet_ids         = var.vpc_link_subnet_ids
  security_group_ids = var.vpc_link_security_group_ids
  tags               = var.tags
}

# HTTP_PROXY straight to the ALB listener over the private link.
# Integration = where a route sends requests. HTTP_PROXY passes each request
# through unchanged; connection_type VPC_LINK + connection_id = "via that link".
resource "aws_apigatewayv2_integration" "alb" {
  api_id             = aws_apigatewayv2_api.this.id
  integration_type   = "HTTP_PROXY"
  integration_method = "ANY"
  integration_uri    = var.alb_listener_arn
  connection_type    = "VPC_LINK"
  connection_id      = aws_apigatewayv2_vpc_link.this.id
}

# Optional JWT authorizer for the "auth" control.
# [TF] count = cond ? 1 : 0 makes this optional; referenced below as .jwt[0].
resource "aws_apigatewayv2_authorizer" "jwt" {
  count            = local.jwt_enabled ? 1 : 0
  api_id           = aws_apigatewayv2_api.this.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "${var.name_prefix}-jwt"

  jwt_configuration {
    issuer   = var.jwt_issuer
    audience = var.jwt_audiences
  }
}

# Catch-all route -> ALB. Attaches the JWT authorizer when enabled.
# Route key "$default" = catch-all for any method and path (no other routes exist).
resource "aws_apigatewayv2_route" "default" {
  # [TF] authorizer_id = null means "argument not set"; the provider treats it as omitted.
  api_id             = aws_apigatewayv2_api.this.id
  route_key          = "$default"
  target             = "integrations/${aws_apigatewayv2_integration.alb.id}"
  authorization_type = local.jwt_enabled ? "JWT" : "NONE"
  authorizer_id      = local.jwt_enabled ? aws_apigatewayv2_authorizer.jwt[0].id : null
}

# --------------------------------------------------------------------------- #
# Access logging + throttling on the auto-deployed stage
# --------------------------------------------------------------------------- #
# CloudWatch log group for the stage's access logs (one JSON line per request).
resource "aws_cloudwatch_log_group" "access" {
  name              = "/apigw/${var.name_prefix}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# Stage = a deployed, addressable version of the API. The "$default" stage has
# no path prefix in its URL, and auto_deploy publishes every change. Its
# invoke_url is output api_endpoint.
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.throttle_rate_limit
    throttling_burst_limit = var.throttle_burst_limit
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    # [TF] jsonencode() turns an HCL map into a JSON string. "$context.*" are
    #      API Gateway's per-request variables, not Terraform interpolation
    #      (Terraform only interpolates ${...}).
    format = jsonencode({
      requestId      = "$context.requestId"
      ip             = "$context.identity.sourceIp"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      integrationErr = "$context.integrationErrorMessage"
    })
  }

  tags = var.tags
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# -> careroute_stack/main.tf module "observability" (API Gateway alarms).
output "api_id" { value = aws_apigatewayv2_api.this.id }
# -> careroute_stack/main.tf (locals) local.app_base_url, :137 the CORS list, and outputs app_url /
#    api_endpoint in careroute_stack/outputs.tf.
output "api_endpoint" { value = aws_apigatewayv2_stage.default.invoke_url }
