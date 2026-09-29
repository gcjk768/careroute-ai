# Per-environment constants for STAGING.
# Read by root.hcl (remote-state key + provider region).
#
# The state key is "staging/careroute/", a different object from demo's, so the
# two environments can never touch each other's resources even by accident.
# [TG] Not a Terraform file: plain HCL `locals` that root.hcl loads with
#      read_terragrunt_config("<this folder>/env.hcl") and then reads as
#      local.env_vars.locals.environment / local.env_vars.locals.aws_region.
locals {
  # -> root.hcl local.environment -> state key "<env>/careroute/...", the
  #    Environment default tag, and var.environment in careroute_stack.
  environment = "staging"
  # -> root.hcl local.aws_region -> state bucket name/region, the provider's
  #    region (provider.tf), and var.region in careroute_stack.
  aws_region  = "ap-southeast-1" # Singapore
}
