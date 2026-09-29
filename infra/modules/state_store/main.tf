###############################################################################
# Module: state_store  (optional — the shared backing store the backend lacks)
# -----------------------------------------------------------------------------
# One DynamoDB table for the state backend/app/store.py, audit.py and
# ratelimit.py keep in process memory today: cases, escalations, session
# history, the audit trail and rate-limit windows. That in-process state is the
# ONLY thing holding the backend at one task (terraform_data.guards). With it in
# a shared table, any task can serve any request, and the backend can scale out
# like the frontend already can.
#
# READ THIS BEFORE TURNING IT ON. Like RDS, this provisions a store the app does
# not use yet: the backend has no DynamoDB client and reads no CAREROUTE_STATE_*
# variable. The variables are injected so the app can pick them up the day
# store.py grows a DynamoDB backend. Until then it is an empty table — and the
# single-task guard stays, because the table existing does not move the state.
#
# WHY DYNAMODB, not ElastiCache or RDS:
#   * $0 when idle. On-demand capacity bills per request; an empty table costs
#     nothing, so it fits a stack that is created for an afternoon. ElastiCache
#     Serverless has a minimum storage charge; RDS is an always-on instance.
#   * It scales without sizing: no node type, no connection pool, no failover
#     to plan. Connection count grows with task count on RDS; not here.
#   * No VPC placement. Tasks reach it over the free gateway endpoint (see
#     modules/networking enable_dynamodb_gateway_endpoint) or the public
#     endpoint, with IAM — not a password — as the credential.
#   * Durable. The audit trail is a record of clinical decisions; a cache that
#     can evict is the wrong home for it, and ElastiCache is a cache.
#
# SINGLE-TABLE LAYOUT. Every entity shares the table and is told apart by its
# key prefix, which is the usual DynamoDB shape and keeps IAM to one ARN:
#
#   pk                 sk                     what
#   CASE#<id>          META                   a case record
#   ESC#<id>           META                   an escalation (gsi1: status queue)
#   SESSION#<sid>      CASE#<ts>#<id>         episodic memory (expires_at = TTL)
#   AUDIT#<case_id>    <seq, zero-padded>     append-only audit entries
#   RATE#<client>      <window start>         rate-limit window (expires_at)
#
# gsi1 serves the one list query the app runs: /api/escalations, "every open
# escalation, oldest first" (gsi1pk = ESC#STATUS#<status>, gsi1sk = created_at).
# The layout is a proposal for the app side, not something this module enforces
# — only the key NAMES are fixed here, because they are the table's schema.
###############################################################################

# ---- Inputs ---------------------------------------------------------------- #
# Set by: careroute_stack local.name_prefix, e.g. "careroute-demo".
variable "name_prefix" { type = string }

# Set by: var.state_store_point_in_time_recovery (default true).
# Used in: aws_dynamodb_table.this point_in_time_recovery.
variable "point_in_time_recovery" {
  description = "Continuous backups (35-day restore window). Billed per GB stored; an empty table costs nothing."
  type        = bool
  default     = true
}

# Set by: var.state_store_deletion_protection (default false: create-and-destroy).
variable "deletion_protection" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ---- Table ------------------------------------------------------------------ #
# PAY_PER_REQUEST = on-demand: no read/write capacity to size or autoscale.
# Server-side encryption is always on in DynamoDB; the block below only chooses
# the AWS-owned key (free) over a customer-managed one — see CKV_AWS_119 in
# .checkov.yaml for why.
resource "aws_dynamodb_table" "this" {
  name         = "${var.name_prefix}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  # Only attributes used in a key (table or index) are declared; every other
  # attribute is schemaless.
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  attribute {
    name = "gsi1pk"
    type = "S"
  }
  attribute {
    name = "gsi1sk"
    type = "S"
  }

  global_secondary_index {
    name            = "gsi1"
    hash_key        = "gsi1pk"
    range_key       = "gsi1sk"
    projection_type = "ALL"
  }

  # Items with an `expires_at` epoch-seconds attribute in the past are deleted by
  # DynamoDB itself, for free: session memory and rate-limit windows age out
  # without a sweeper. Audit items simply never carry the attribute.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery
  }

  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = var.deletion_protection

  tags = merge(var.tags, { Name = "${var.name_prefix}-state" })
}

# ---- IAM -------------------------------------------------------------------- #
# What the backend task role may do: read and write items, nothing on the table
# itself (no DeleteTable, no UpdateTable, no Scan — every access pattern above
# is a key lookup or a Query).
#
# The second statement makes the audit trail append-only from the app's side:
# no DeleteItem and no UpdateItem on any AUDIT# partition. PutItem stays allowed
# because appending IS a put; overwriting an existing entry is the app's job to
# refuse (attribute_not_exists(sk) on the put). An explicit Deny wins over any
# Allow, so this holds even if a broader policy is attached to the role later.
data "aws_iam_policy_document" "read_write" {
  statement {
    sid = "StateItems"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:Query",
      "dynamodb:ConditionCheckItem",
      "dynamodb:DescribeTable",
    ]
    resources = [
      aws_dynamodb_table.this.arn,
      "${aws_dynamodb_table.this.arn}/index/*",
    ]
  }

  statement {
    sid       = "AuditTrailIsAppendOnly"
    effect    = "Deny"
    actions   = ["dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem"]
    resources = [aws_dynamodb_table.this.arn]
    condition {
      test     = "ForAnyValue:StringLike"
      variable = "dynamodb:LeadingKeys"
      values   = ["AUDIT#*"]
    }
  }
}

# Managed (not inline) so the caller attaches it with a policy_attachment, the
# same way modules/artifacts hands over its read_write_policy_arn.
resource "aws_iam_policy" "read_write" {
  name        = "${var.name_prefix}-state-rw"
  description = "Item-level read/write on the CareRoute state table; audit entries are append-only."
  policy      = data.aws_iam_policy_document.read_write.json
  tags        = var.tags
}

# ---- Outputs ---------------------------------------------------------------- #
# table_name -> CAREROUTE_STATE_TABLE in the backend container (careroute_stack scaling.tf).
output "table_name" { value = aws_dynamodb_table.this.name }
output "table_arn" { value = aws_dynamodb_table.this.arn }
# read_write_policy_arn -> aws_iam_role_policy_attachment.task_state_store.
output "read_write_policy_arn" { value = aws_iam_policy.read_write.arn }
