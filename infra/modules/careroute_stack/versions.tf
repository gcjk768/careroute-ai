###############################################################################
# careroute_stack — provider + version constraints
# -----------------------------------------------------------------------------
# This is the single root module each environment (staging / production) points
# its Terragrunt unit at. It declares the required providers; Terragrunt
# generates the actual `provider "aws"` block (region + default tags) at apply
# time so credentials/region stay out of the code.
###############################################################################

# [TF] The `terraform {}` block configures Terraform itself (versions, providers) — no AWS resources.
# [TF] Because careroute_stack is the root module under Terragrunt, `terraform init` reads this.
terraform {
  # [TF] Minimum Terraform CLI version; `init`/`plan` refuse to run on an older binary.
  required_version = ">= 1.9.0"

  # [TF] required_providers = which provider PLUGINS `terraform init` downloads into .terraform/,
  # [TF] from which registry address, and which versions are allowed. The exact versions picked are
  # [TF] recorded in .terraform.lock.hcl. The provider's CONFIGURATION (region, credentials, default
  # [TF] tags) is separate: root.hcl `generate "provider"` writes it into provider.tf at run time.
  required_providers {
    # AWS provider: every aws_* resource and data source in this module and its child modules.
    # [TF] "~> 5.60" is the pessimistic operator: >= 5.60 and < 6.0 (minor upgrades yes, major no).
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    # random provider: random_password.grafana (main.tf) and random_password.db (modules/rds) —
    # generated values kept in state, so they stay stable across applies. "~> 3.6" = >= 3.6, < 4.0.
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
