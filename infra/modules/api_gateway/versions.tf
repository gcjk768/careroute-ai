# Version constraints for the api_gateway module. Same pins as the root module
# (careroute_stack/versions.tf), which is what actually selects the versions;
# declared here too so the module lints and validates by itself (tflint --recursive).
terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}
