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
  default     = "wsl-dev"
}
