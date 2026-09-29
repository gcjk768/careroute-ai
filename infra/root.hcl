###############################################################################
# Terragrunt ROOT configuration
# -----------------------------------------------------------------------------
# Included by every environment (live/<env>/terragrunt.hcl). It exists to keep
# the two things that must NOT be duplicated per environment in one place:
#
#   1. remote_state  — where Terraform state + locks live (S3 + DynamoDB),
#                      keyed per environment so staging and prod never collide.
#   2. generate      — the `provider "aws"` block, so region + default tags are
#                      injected at apply time instead of hard-coded in modules.
#
# Per-environment values (region, environment name) come from live/<env>/env.hcl.
###############################################################################

# [TG] `locals` = named values private to THIS file (read as local.<name>).
#      They are not sent to Terraform by themselves; only `inputs` (bottom) are.
locals {
  # Each environment folder has an env.hcl next to its terragrunt.hcl.
  # [TG] get_terragrunt_dir() = the folder of the CHILD terragrunt.hcl being run
  #      (e.g. live/demo), not this file's folder: an included file is evaluated
  #      in the child's context, so each environment loads its OWN env.hcl.
  # [TG] read_terragrunt_config() parses another HCL file and returns its blocks
  #      as an object, hence the `.locals.<name>` paths on the next two lines.
  env_vars    = read_terragrunt_config("${get_terragrunt_dir()}/env.hcl")
  # "demo" / "staging". Flows to: the state key below, the Environment default
  # tag in provider.tf, and var.environment in careroute_stack (via `inputs`).
  environment = local.env_vars.locals.environment
  # "ap-southeast-1". Flows to: state bucket name + region, the provider's
  # region in provider.tf, and var.region in careroute_stack (via `inputs`).
  aws_region  = local.env_vars.locals.aws_region
}

# --------------------------------------------------------------------------- #
# Remote state: one S3 bucket, one key per environment, DynamoDB for locking.
# generate = true auto-provisions the bucket + table on first run.
# --------------------------------------------------------------------------- #
# WHY STATE EXISTS: Terraform records every resource it created (IDs and
# attributes) in a state file, and each plan diffs your code against it. Kept
# in S3 so your laptop and CI share one copy; the DynamoDB table holds a lock
# so two applies can never run at once and corrupt it.
# [TG] remote_state is Terragrunt's wrapper that writes Terraform's own
#      `terraform { backend "s3" { ... } }` block for you.
remote_state {
  backend = "s3"

  # [TG] Writes backend.tf (the backend "s3" block built from `config` below)
  #      into Terragrunt's working copy of the module before terraform runs.
  #      overwrite_terragrunt = replace the file only if Terragrunt wrote it.
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }

  config = {
    # [TG] get_aws_account_id() asks STS (GetCallerIdentity) with your current
    #      credentials, so the bucket name is unique per account + region.
    bucket         = "careroute-tfstate-${get_aws_account_id()}-${local.aws_region}"
    # One state object per environment: demo/careroute/terraform.tfstate,
    # staging/careroute/terraform.tfstate. Same bucket, different key.
    key            = "${local.environment}/careroute/terraform.tfstate"
    region         = local.aws_region
    # Server-side encryption of the state object. It matters: state holds
    # secret values (e.g. the API keys written to Secrets Manager) in plain text.
    encrypt        = true
    # DynamoDB lock table (partition key LockID); one table serves every env.
    dynamodb_table = "careroute-tf-locks"
  }
}

# --------------------------------------------------------------------------- #
# Provider block generated into every module. default_tags flow onto every
# taggable resource, so cost allocation / ownership is consistent for free.
# --------------------------------------------------------------------------- #
# [TG] A `generate` block writes an arbitrary file into the working dir before
#      terraform runs. This one produces provider.tf, which is why no module
#      under modules/ declares `provider "aws"` itself (versions.tf only pins it).
generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  # The heredoc below (<<-EOF ... EOF) is the literal text of provider.tf;
  # Terragrunt fills in the ${local.*} parts before writing it. default_tags
  # makes the AWS provider stamp these tags on every taggable resource.
  contents  = <<-EOF
    provider "aws" {
      region = "${local.aws_region}"
      default_tags {
        tags = {
          Project     = "CareRoute-AI"
          Environment = "${local.environment}"
          ManagedBy   = "Terragrunt"
        }
      }
    }
  EOF
}

# Values every environment passes to the stack module (overridable per env).
# [TG] `inputs` are handed to Terraform as TF_VAR_<name> environment variables,
#      so each key must match a `variable` in the module being run
#      (modules/careroute_stack/variables.tf). The environment's own `inputs`
#      block (live/<env>/terragrunt.hcl) is merged on top; the child wins a clash.
inputs = {
  # -> variable "environment" (careroute_stack/variables.tf) -> local.name_prefix
  #    "careroute-<env>" (every resource name) and local.common_tags.
  environment = local.environment
  # -> variable "region" (careroute_stack/variables.tf) -> awslogs-region of the sidecars,
  #    and the region input of the ECS service / monitoring / scheduled modules.
  region      = local.aws_region
}
