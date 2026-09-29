###############################################################################
# Module: artifacts
# -----------------------------------------------------------------------------
# One private, versioned, encrypted S3 bucket that closes the two storage gaps
# the application repo records in docs/vault/Infra-Dependent Work 2026-09-02.md:
#
#   1. DVC remote (§4) — backend/.dvc/config is EMPTY, so `dvc push` has nowhere
#      to go and `dvc pull` cannot restore backend/data/triage_dataset.npz on a
#      fresh clone. The dataset is reproducible from code but not *versioned*,
#      which is the difference between MLOps Pillar 2 claimed and delivered.
#      After apply:  dvc remote add -d origin s3://<bucket>/dvc
#
#   2. Runtime ML telemetry — the backend writes its inference log, drift
#      reports and fairness/monitoring output to the task filesystem
#      (CAREROUTE_INFERENCE_LOG / _MONITOR_DIR / _REPORT_DIR). Fargate task
#      storage is EPHEMERAL: every deploy, Spot reclaim or crash deletes the
#      drift evidence. The prefix below is where a shipper puts it; see the
#      README for why we did not mount EFS for a demo.
#
# Bucket versioning is deliberate — DVC's contract is content-addressed objects,
# and versioning means an accidental `dvc gc`/delete is recoverable.
###############################################################################

# ---- Inputs ---------------------------------------------------------------- #
# [TF] Each `variable` is an input, set by `module "artifacts" { ... }` in
#      modules/careroute_stack/main.tf and read here as var.<name>. That block
#      has `count = var.enable_artifacts_bucket ? 1 : 0` (live/demo false,
#      live/staging true), so the stack reads outputs as module.artifacts[0].<name>.
#
# name_prefix: local.name_prefix ("careroute-<env>"). Used in: bucket name,
#   Name tag, IAM policy name.
variable "name_prefix" { type = string }

# Default-only (the stack does not pass it) -> always true.
# Used in: aws_s3_bucket.this.force_destroy.
variable "force_destroy" {
  description = "Let `terragrunt destroy` delete a non-empty bucket. TRUE matches this project's create-and-destroy demo model; set FALSE the moment the bucket holds the only copy of anything."
  type        = bool
  default     = true
}

# Default-only -> 30. Used in: the lifecycle rule's noncurrent_days.
variable "noncurrent_version_expiration_days" {
  description = "Days to keep superseded object versions. Bounded so versioning cannot quietly accumulate cost."
  type        = number
  default     = 30
}

# Set by: `var.enable_alb_access_logs ? "alb-access-logs" : ""` (live/staging sets
# enable_alb_access_logs = true). The stack then passes this bucket's name to
# modules/alb as access_logs_bucket, so the ALB writes under this prefix.
# Used in: local.alb_logs_enabled + the AllowELBLogDelivery statement's resource.
variable "alb_access_logs_prefix" {
  description = "When non-empty, the bucket policy also grants the regional ELB log-delivery service PutObject under this prefix. Empty (default) leaves the policy at TLS-only."
  type        = string
  default     = ""
}

# Set by: local.common_tags. Used in: bucket and IAM policy tags.
variable "tags" {
  type    = map(string)
  default = {}
}

# A bucket name must be globally unique; the account id keeps it so without
# needing a random suffix that changes on every re-create.
# [TF] A `data` block READS something that already exists instead of creating
#      it; referenced as data.<TYPE>.<NAME>.<attr>. aws_caller_identity = the
#      account/ARN of the credentials Terraform runs with (an STS call); its
#      account_id is used in the bucket name and the ELB log-delivery statement.
data "aws_caller_identity" "current" {}

# ELB access logging is delivered by an AWS-owned account whose ID differs per
# region. `logdelivery.elasticloadbalancing.amazonaws.com` is the modern service
# principal and works in every region that has it; the older per-region account
# IDs are only needed in the original regions. Using the service principal keeps
# this from being a lookup table that goes stale.
# [TF] `locals` = values computed inside the module, read as local.<name>.
locals {
  alb_logs_enabled = var.alb_access_logs_prefix != ""
}

# The S3 bucket itself, e.g. careroute-staging-artifacts-123456789012.
# force_destroy = true lets `destroy` delete it even when it still holds objects
# (and all their versions); without it S3 refuses to delete a non-empty bucket.
# [TF] merge(a, b) combines maps, keys in b win — adds a Name tag to var.tags.
# Since AWS provider v4 the bucket's settings are SEPARATE resources below, each
# pointing at it via `bucket = aws_s3_bucket.this.id`.
resource "aws_s3_bucket" "this" {
  #checkov:skip=CKV2_AWS_62:No consumer for object events (DVC + telemetry writes are pulled, not pushed). A notification with no target is theatre; add aws_s3_bucket_notification when something must react to writes.
  bucket        = "${var.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.force_destroy
  tags          = merge(var.tags, { Name = "${var.name_prefix}-artifacts" })
}

# S3 Block Public Access for this bucket: reject public ACLs, reject public
# bucket policies, ignore any existing public ACLs, and restrict access to
# policies that are public. Together: nothing in here can become public.
resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning: overwrites/deletes keep the prior object as a "noncurrent version".
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Default encryption = SSE-S3 (AES256, S3-managed keys, no KMS cost). SSE-KMS
# would be sse_algorithm = "aws:kms" plus a key ARN.
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Lifecycle rules. `filter {}` (empty) = applies to every object in the bucket.
#   noncurrent_version_expiration: permanently delete a superseded version N days
#     after it stopped being current (keeps versioning from growing cost forever).
#   abort_incomplete_multipart_upload: clean up parts of uploads that never
#     completed after 7 days (otherwise they are billed but invisible).
# No storage-class transitions (e.g. to Glacier) are configured.
resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# The bucket holds a synthetic clinical dataset and drift reports. TLS-only is
# the minimum bar for anything on the PHI side of the line, even when today's
# data is synthetic — the policy must already be right when real data arrives.
# [TF] data "aws_iam_policy_document" builds IAM policy JSON from HCL blocks —
#      nothing is created in AWS; .json (used by the bucket policy below) is the
#      rendered string. Each `statement` block is one policy statement.
# Statement 1: Deny every S3 action, for everyone, when the request is not over
# HTTPS (aws:SecureTransport = false) — an explicit Deny beats any Allow.
data "aws_iam_policy_document" "bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.this.arn,
      "${aws_s3_bucket.this.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # ALB access log delivery. Without this statement the ALB accepts the config
  # and then writes NOTHING — there is no error anywhere, the prefix just stays
  # empty, which is the worst way for an audit log to fail.
  # [TF] `dynamic "statement"` generates `statement` blocks from for_each; with
  #      `cond ? [1] : []` it is emitted once or not at all. `content` is the body.
  # Allows the ELB log-delivery service to write objects under
  # <prefix>/AWSLogs/<account id>/, and only on behalf of THIS account.
  dynamic "statement" {
    for_each = local.alb_logs_enabled ? [1] : []
    content {
      sid       = "AllowELBLogDelivery"
      effect    = "Allow"
      actions   = ["s3:PutObject"]
      resources = ["${aws_s3_bucket.this.arn}/${var.alb_access_logs_prefix}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]
      principals {
        type        = "Service"
        identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
      }
      condition {
        test     = "StringEquals"
        variable = "aws:SourceAccount"
        values   = [data.aws_caller_identity.current.account_id]
      }
    }
  }
}

# Attaches the policy document above to the bucket (the bucket's resource policy).
# [TF] depends_on forces ordering when no attribute reference implies it: the
#      public access block is applied first, since S3 can reject a bucket-policy
#      change that races with a Block Public Access change on the same bucket.
resource "aws_s3_bucket_policy" "this" {
  bucket     = aws_s3_bucket.this.id
  policy     = data.aws_iam_policy_document.bucket.json
  depends_on = [aws_s3_bucket_public_access_block.this]
}

# --------------------------------------------------------------------------- #
# Least-privilege read/write for whoever needs it (the ECS task role, a CI user).
# Scoped to THIS bucket only; ListBucket is bucket-level, the rest object-level.
# --------------------------------------------------------------------------- #
# Identity-based policy JSON (for a role/user, not the bucket): list the bucket
# (bucket ARN) and get/put/delete objects (bucket ARN + "/*").
data "aws_iam_policy_document" "read_write" {
  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.this.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.this.arn}/*"]
  }
}

# A customer-managed IAM policy built from that JSON. Its ARN is output as
# read_write_policy_arn, which careroute_stack attaches to the ECS TASK role in
# aws_iam_role_policy_attachment.task_artifacts (so the telemetry shipper, drift
# monitor and MLflow containers can use the bucket).
resource "aws_iam_policy" "read_write" {
  name        = "${var.name_prefix}-artifacts-rw"
  description = "Read/write the CareRoute artifacts bucket (DVC remote + ML telemetry)."
  policy      = data.aws_iam_policy_document.read_write.json
  tags        = var.tags
}

# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
# [TF] Outputs, read in careroute_stack as module.artifacts[0].<name>:
#   bucket_name           -> local.artifacts_bucket_name (telemetry sidecar,
#                            drift monitor, MLflow), module.alb access_logs_bucket,
#                            and outputs.tf `artifacts_bucket`.
#   bucket_arn            -> not consumed by the stack.
#   read_write_policy_arn -> aws_iam_role_policy_attachment.task_artifacts.
output "bucket_name" { value = aws_s3_bucket.this.id }
output "bucket_arn" { value = aws_s3_bucket.this.arn }
output "read_write_policy_arn" { value = aws_iam_policy.read_write.arn }

# -> careroute_stack outputs.tf `dvc_remote_url`.
output "dvc_remote_url" {
  description = "Paste into the app repo: cd backend && dvc remote add -d origin <this>"
  value       = "s3://${aws_s3_bucket.this.id}/dvc"
}

# Not consumed: careroute_stack builds the same s3://.../ml-telemetry string itself.
output "ml_telemetry_prefix" {
  description = "Where the backend's inference log / drift reports should be shipped."
  value       = "s3://${aws_s3_bucket.this.id}/ml-telemetry"
}
