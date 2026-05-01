provider "databricks" {
  profile = var.databricks_profile
}

data "aws_caller_identity" "current" {}

# Placeholder trust policy — allows the current AWS account root to assume the
# role. After applying, go to AWS Console → IAM → this role → Trust relationships
# and replace with the values from the "databricks_credential" outputs below.
resource "aws_iam_role" "databricks_s3" {
  name = "${local.prefix}-databricks-s3"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
    }]
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

  # Skips Databricks' S3 access check at creation time.
  # The trust policy must be updated manually in the AWS Console after apply
  # using the values from the "databricks_credential" outputs.
  skip_validation = true
}
