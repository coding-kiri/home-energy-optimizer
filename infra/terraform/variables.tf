variable "env" {
  description = "Deployment environment (e.g. dev, prod). Used as a suffix in all resource names."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be one of: dev, prod."
  }
}
