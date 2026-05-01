variable "env" {
  description = "Deployment environment (dev, prod)"
  type        = string
  default     = "dev"
}

variable "project" {
  description = "Project name used as a prefix for all resource names"
  type        = string
  default     = "home-energy-optimizer"
}

variable "aws_region" {
  description = "AWS region to deploy resources into"
  type        = string
  default     = "eu-west-2"
}

variable "databricks_profile" {
  description = "Databricks CLI profile from ~/.databrickscfg (same profile used by the Databricks extension and DAB)."
  type        = string
  default     = "home-energy-optimizer-dev"
}

# Bootstrap variables — populated during step 2 of the initial deployment.
# See bootstrap.sh for the full two-step process.
variable "uc_master_role_arn" {
  description = "Unity Catalog IAM role ARN from the databricks_credential Terraform output (step 2 of bootstrap)."
  type        = string
  default     = ""
}

variable "uc_external_id" {
  description = "External ID from the databricks_credential Terraform output (step 2 of bootstrap)."
  type        = string
  default     = ""
}
