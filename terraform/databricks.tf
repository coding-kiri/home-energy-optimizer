provider "databricks" {
  profile = var.databricks_profile
}

data "aws_caller_identity" "current" {}

locals {
  # True once the bootstrap outputs have been pasted into terraform.tfvars.
  bootstrap_complete = var.uc_master_role_arn != "" && var.uc_external_id != ""

  self_role_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.prefix}-databricks-s3"

  # Step 1 (bootstrap): only the self-assume statement so the role can be
  # created and the storage credential can be registered with Databricks.
  # Step 2 (bootstrap complete): add the UC master role statement so Unity
  # Catalog can actually assume the role and access S3.
  trust_statements = local.bootstrap_complete ? [
    {
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = var.uc_master_role_arn }
      Condition = { StringEquals = { "sts:ExternalId" = var.uc_external_id } }
    },
    {
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = local.self_role_arn }
      Condition = { StringEquals = { "sts:ExternalId" = var.uc_external_id } }
    }
  ] : [
    {
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = local.self_role_arn }
      Condition = {}
    }
  ]
}

resource "aws_iam_role" "databricks_s3" {
  name = "${local.prefix}-databricks-s3"

  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.trust_statements
  })

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_iam_role_policy" "databricks_s3_access" {
  name = "${local.prefix}-s3-access"
  role = aws_iam_role.databricks_s3.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = flatten([
          for b in aws_s3_bucket.medallion : [b.arn, "${b.arn}/*"]
        ])
      }
    ]
  })
}

resource "databricks_storage_credential" "medallion" {
  name    = "${local.prefix}-s3"
  comment = "Managed by Terraform — S3 access for medallion buckets"

  aws_iam_role {
    role_arn = aws_iam_role.databricks_s3.arn
  }
}

resource "databricks_external_location" "medallion" {
  for_each = aws_s3_bucket.medallion

  name            = "${var.env}-${each.key}"
  url             = "s3://${each.value.bucket}"
  credential_name = databricks_storage_credential.medallion.name
  comment         = "Managed by Terraform — ${each.key} layer"

  # Skips validation during step 1 of bootstrap (UC master role ARN not yet known).
  # Automatically set to false once uc_master_role_arn and uc_external_id are provided.
  skip_validation = !local.bootstrap_complete
}
