###############################################################################
# Module: ecr
# -----------------------------------------------------------------------------
# One Elastic Container Registry repository per application image. The GitLab
# CI/CD "build model artifacts" + "container scan (Trivy)" stages push the
# backend and frontend images here; ECS Fargate then pulls them on deploy.
#
# The proposal's pipeline names "Container registry (GitLab / Amazon ECR)" as
# the registry stage feeding the eval-harness gate -> deploy.
###############################################################################

# --------------------------------------------------------------------------- #
# Inputs. Only caller: `module "ecr"` in modules/careroute_stack/main.tf.
# [TF] Each `variable` is an input read as var.<name>; the module block sets it
# with `<name> = <value>`, otherwise the `default` applies.
# --------------------------------------------------------------------------- #

# Set by: local.name_prefix ("careroute-<env>"). Used in: repository name + Name tag.
variable "name_prefix" { type = string }

# Set by: ["backend", "frontend"] (explicit in module "ecr"). Used in: for_each on
# aws_ecr_repository.this — one repository per entry.
variable "repositories" {
  description = "Logical image names to create repos for (e.g. [\"backend\",\"frontend\"])."
  type        = list(string)
  default     = ["backend", "frontend"]
}

# Set by: var.ecr_image_tag_mutability <- live/demo/terragrunt.hcl: "MUTABLE" when
# IMAGE_TAG is unset/"latest", else "IMMUTABLE"; staging uses the stack default
# IMMUTABLE. Used in: aws_ecr_repository.this.
variable "image_tag_mutability" {
  description = <<-EOT
    IMMUTABLE (the default, and the right answer) forbids overwriting a tag that
    already exists, which is what makes an image reference a RELEASE IDENTITY
    rather than a moving pointer. Three things follow from it and from nothing
    else: the task definition changes when the code changes, so `apply` actually
    starts a deployment; the running revision is traceable to one build; and the
    previous tag still resolves, so there is something to roll back TO.

    MUTABLE only exists for the `:latest` workflow in README.md ("build & push
    first"), where the second push to the same tag must be allowed to clobber
    the first. Set it per environment while that is still how images get here;
    it is a trade-off to record, not a default to inherit.
  EOT
  type        = string
  default     = "IMMUTABLE"

  # [TF] validation { condition, error_message } rejects bad input at PLAN time,
  # before any API call. contains(list, value) = "value is in list".
  validation {
    condition     = contains(["IMMUTABLE", "MUTABLE"], var.image_tag_mutability)
    error_message = "image_tag_mutability must be IMMUTABLE or MUTABLE."
  }
}

# Basic scanning: ECR checks each pushed image for known CVEs (results in the
# console's image "Vulnerabilities" tab). Set by: nobody — default true.
variable "scan_on_push" {
  description = "Run ECR's built-in vulnerability scan whenever an image is pushed."
  type        = bool
  default     = true
}

# Set by: nobody — default 5. Used in: lifecycle policy rule 1 countNumber.
variable "max_untagged_images" {
  description = "Lifecycle policy: how many untagged images to keep before expiry."
  type        = number
  default     = 5
}

# Set by: nobody — default 10. Used in: lifecycle policy rule 2 countNumber.
variable "max_tagged_images" {
  description = <<-EOT
    How many TAGGED images to keep per repository before the oldest expire.

    This matters because of IMMUTABLE tags, not in spite of them: once every
    build pushes a distinct `$CI_COMMIT_SHA`, nothing ever overwrites anything,
    so tagged images accumulate forever and the untagged-only rule below never
    touches them. A backend image carrying scikit-learn, numpy, SHAP and a baked
    severity model is not small, and ECR bills per GB-month.

    10 keeps enough history to roll back several releases deep, which is the
    reason to keep any of them.
  EOT
  type        = number
  default     = 10
}

# Set by: nobody — default true. Used in: aws_ecr_repository.this.force_delete.
variable "force_delete" {
  description = "Allow `terraform destroy` to delete repos that still contain images (matches the proposal's create-and-destroy model)."
  type        = bool
  default     = true
}

# Set by: local.common_tags.
variable "tags" {
  type    = map(string)
  default = {}
}

# ECR private repository, e.g. "careroute-demo/backend"
# (URL <account>.dkr.ecr.<region>.amazonaws.com/careroute-demo/backend).
# [TF] for_each = toset(list) creates ONE instance per element, addressed as
# aws_ecr_repository.this["backend"]; each.value is the current element.
# Unlike count, removing one element does not renumber the others.
resource "aws_ecr_repository" "this" {
  for_each = toset(var.repositories)
  name     = "${var.name_prefix}/${each.value}"
  # MUTABLE: a push can move an existing tag (e.g. :latest) to a new image.
  # IMMUTABLE: pushing an existing tag fails.
  image_tag_mutability = var.image_tag_mutability
  # Lets `terraform destroy` delete a repo that still holds images.
  force_delete = var.force_delete

  image_scanning_configuration {
    scan_on_push = var.scan_on_push
  }

  # Server-side encryption with ECR-managed keys (SSE-S3 style); "KMS" would use KMS keys.
  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-${each.value}" })
}

# Keep the registry tidy / cheap: expire old untagged (dangling) layers.
# ECR lifecycle policy = automatic image expiry rules evaluated by ECR.
# [TF] for_each over a MAP of resources (aws_ecr_repository.this): one policy
# per repository; each.key = "backend"/"frontend", each.value = that repository.
resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name

  # [TF] jsonencode() turns this HCL object into the JSON document the ECR API
  # expects — HCL `=` becomes JSON `:`, lists/maps map straight across.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images beyond the retention count"
        selection = {
          # untagged = image layers no tag points at any more (e.g. the old :latest).
          tagStatus = "untagged"
          # imageCountMoreThan N = keep the newest N matching images, expire the rest.
          countType   = "imageCountMoreThan"
          countNumber = var.max_untagged_images
        }
        action = { type = "expire" }
      },
      # Tagged images need their own rule: with IMMUTABLE tags every build
      # pushes a new SHA and nothing is ever overwritten, so rule 1 — which only
      # ever sees dangling layers — would let the repository grow without bound.
      # `tagStatus = any` at the LOWEST priority is the catch-all: ECR evaluates
      # rules in priority order, so rule 1 still claims the untagged images first
      # and this one governs what is left.
      {
        rulePriority = 2
        description  = "Expire the oldest images beyond the tagged retention count"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.max_tagged_images
        }
        action = { type = "expire" }
      },
    ]
  })
}

# --------------------------------------------------------------------------- #
# Outputs — repository_urls is a map { backend = "<acct>.dkr.ecr...", ... }
# consumed by the stack to build the full `<url>:<tag>` image reference.
# --------------------------------------------------------------------------- #
# [TF] { for k, r in MAP : k => EXPR } builds a new map from the for_each'd
# resources: {"backend" = "<url>", "frontend" = "<url>"}.
# Consumers: module.ecr.repository_urls["backend"] / ["frontend"] in careroute_stack
# (image for module backend, frontend, sub_agents, drift_monitor) and the
# ecr_backend_repo / ecr_frontend_repo outputs in careroute_stack/outputs.tf.
output "repository_urls" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

# Not read by any caller today (handy for scoping a CI push policy).
output "repository_arns" {
  value = { for k, r in aws_ecr_repository.this : k => r.arn }
}
