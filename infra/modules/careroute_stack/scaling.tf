###############################################################################
# careroute_stack — scaling: what can grow, what cannot yet, and why
# -----------------------------------------------------------------------------
# Every tier, and what limits it:
#
#   ALB            scales itself (AWS-managed). Nothing to do.
#   frontend       STATELESS. Autoscales on CPU and, optionally, ALB requests
#                  per task (frontend_enable_autoscaling). Scales freely.
#   backend        STATEFUL TODAY — cases, escalations, session memory, the
#                  audit trail and rate-limit windows live in process memory
#                  (store.py, audit.py, ratelimit.py). Held at one task by
#                  terraform_data.guards. Everything it needs to scale out is
#                  built here and switched off: autoscaling policies,
#                  least-outstanding-requests routing, a shared state table,
#                  and a Prometheus scrape that finds every task.
#   state          modules/state_store: DynamoDB on-demand, $0 idle
#                  (enable_shared_state_store). The seam the app must fill.
#   LLM            the hosted API. Scales on the provider's side; the limit is
#                  its rate limit and the per-task cost guards, not this stack.
#   monitoring     one Prometheus per stack. Scrapes backend tasks by DNS, so
#                  it follows the backend from one task to N without a change.
#
# The path from here to a horizontally scaled backend:
#   1. app: implement store/audit/ratelimit against CAREROUTE_STATE_TABLE
#   2. infra: enable_shared_state_store = true  (table + IAM + endpoint + env)
#   3. infra: allow_multi_task_backend = true, enable_autoscaling = true,
#             backend_max_count = N, backend_requests_per_target = <load test>
# Step 3 is refused at plan time without step 2 (see the guards in main.tf).
###############################################################################

locals {
  # ALBRequestCountPerTarget is keyed on "<alb arn suffix>/<target group arn
  # suffix>" — i.e. "app/<alb>/<id>/targetgroup/<tg>/<id>".
  backend_alb_resource_label  = "${module.alb.alb_arn_suffix}/${module.alb.backend_target_group_arn_suffix}"
  frontend_alb_resource_label = "${module.alb.alb_arn_suffix}/${module.alb.frontend_target_group_arn_suffix}"

  # What the backend is told about the shared store. Merged into its
  # environment in main.tf. Nothing in the app reads these yet (see
  # modules/state_store); they are the contract the app side implements.
  state_env = var.enable_shared_state_store ? {
    CAREROUTE_STATE_BACKEND = "dynamodb"
    CAREROUTE_STATE_TABLE   = module.state_store[0].table_name
    CAREROUTE_STATE_GSI     = "gsi1"
  } : {}
}

# DynamoDB state table + its item-level read/write policy. Outputs table_name
# (-> local.state_env) and read_write_policy_arn (-> the attachment below).
module "state_store" {
  source = "../state_store"
  count  = var.enable_shared_state_store ? 1 : 0

  name_prefix            = local.name_prefix
  point_in_time_recovery = var.state_store_point_in_time_recovery
  deletion_protection    = var.state_store_deletion_protection

  tags = local.common_tags
}

# On the TASK role (what the app code runs as), the same way the artifacts
# bucket policy is attached in main.tf.
resource "aws_iam_role_policy_attachment" "task_state_store" {
  count      = var.enable_shared_state_store ? 1 : 0
  role       = module.ecs_cluster.task_role_name
  policy_arn = module.state_store[0].read_write_policy_arn
}
