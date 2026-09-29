# tflint configuration — CareRoute AI infra.
# `tflint --init` installs the AWS ruleset below; `tflint --recursive` then lints
# every module. Provider-aware checks (invalid instance types, deprecated
# arguments, missing required attributes) that `terraform validate` cannot catch.
# A lint tool only: it creates nothing in AWS and Terraform/Terragrunt never
# read this file. CI runs it in the `tflint` job (.gitlab-ci.yml).
plugin "aws" {
  enabled = true
  # Pinned so lint results cannot change underneath you on a new ruleset release.
  version = "0.31.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
