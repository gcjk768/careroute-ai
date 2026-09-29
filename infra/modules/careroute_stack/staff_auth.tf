###############################################################################
# careroute_stack — staff API key for the clinical escalation endpoints
# -----------------------------------------------------------------------------
# The app closed README §7's blocker on its side: the backend now requires
# X-Staff-Key == CAREROUTE_STAFF_API_KEY on /api/escalations* (401 otherwise),
# and the frontend's server-side Route Handler (app/api/escalations/[[...path]])
# attaches the key so it never reaches the browser. With the key unset the
# backend stays OPEN — which is what this stack deployed until now, because it
# never set the key.
#
# Two things have to be true on AWS for the key to protect anything:
#
#   1. Both tasks get the SAME key (below: generated, held in Secrets Manager,
#      injected into backend and frontend).
#   2. /api/escalations* reaches the FRONTEND, not the backend. The ALB's
#      /api/* rule sends everything under /api straight to the backend, which
#      skips the Route Handler: the browser's escalation calls then arrive with
#      no key and get 401. modules/alb puts a higher-priority rule in front
#      (frontend_proxied_paths), and the handler forwards to the backend over
#      Cloud Map — never back through the ALB, which would route it to the
#      frontend again, in a loop.
###############################################################################

locals {
  # /api/staff/* is the staff login (Route Handler /api/staff/session). Without
  # it the /api/* rule sent login to the backend (404) and nobody could sign in.
  staff_proxy_paths = var.enable_staff_api_key ? ["/api/escalations", "/api/escalations/*", "/api/staff/*"] : []

  staff_secrets = var.enable_staff_api_key ? {
    CAREROUTE_STAFF_API_KEY = aws_secretsmanager_secret.staff_api_key[0].arn
  } : {}

  # The key alone made the frontend proxy a confused deputy: it attached the
  # key for ANY caller, so an anonymous browser could read and decide
  # escalations. The password gates a signed session cookie the proxy now
  # requires (app: frontend/lib/staffSession.js). Frontend only.
  staff_frontend_secrets = var.enable_staff_api_key ? merge(local.staff_secrets, {
    CAREROUTE_STAFF_PASSWORD = aws_secretsmanager_secret.staff_password[0].arn
  }) : {}

  # The Route Handler reads BACKEND_URL at runtime. backend.<project>.local is
  # the backend's Cloud Map name (module.backend registers as "backend" when
  # local.backend_service_discovery, which the staff key turns on). Port 8000
  # is task-to-task: the ECS security group already allows `self`.
  staff_frontend_env = var.enable_staff_api_key ? {
    BACKEND_URL = "http://backend.${var.project}.local:8000"
  } : {}
}

resource "random_password" "staff_api_key" {
  count   = var.enable_staff_api_key ? 1 : 0
  length  = 40
  special = false # sent as an HTTP header value; keep it to [A-Za-z0-9]
}

# Named under "<name_prefix>/", the prefix the execution role may read
# (modules/ecs_cluster secrets_read), so no IAM change is needed.
resource "aws_secretsmanager_secret" "staff_api_key" {
  count                   = var.enable_staff_api_key ? 1 : 0
  name                    = "${local.name_prefix}/staff-api-key"
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "staff_api_key" {
  count         = var.enable_staff_api_key ? 1 : 0
  secret_id     = aws_secretsmanager_secret.staff_api_key[0].id
  secret_string = random_password.staff_api_key[0].result
}

resource "random_password" "staff_password" {
  count   = var.enable_staff_api_key ? 1 : 0
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "staff_password" {
  count                   = var.enable_staff_api_key ? 1 : 0
  name                    = "${local.name_prefix}/staff-password"
  recovery_window_in_days = var.secret_recovery_window_days
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "staff_password" {
  count         = var.enable_staff_api_key ? 1 : 0
  secret_id     = aws_secretsmanager_secret.staff_password[0].id
  secret_string = var.staff_password != "" ? var.staff_password : random_password.staff_password[0].result
}
