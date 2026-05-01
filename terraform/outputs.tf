output "bucket_names" {
  description = "Names of all medallion S3 buckets keyed by layer (raw, bronze, silver, gold)"
  value       = { for k, v in aws_s3_bucket.medallion : k => v.bucket }
}

output "bucket_arns" {
  description = "ARNs of all medallion S3 buckets keyed by layer (raw, bronze, silver, gold)"
  value       = { for k, v in aws_s3_bucket.medallion : k => v.arn }
}

output "databricks_credential" {
  description = "Values needed to update the IAM role trust policy in the AWS Console"
  value = {
    unity_catalog_iam_arn = databricks_storage_credential.medallion.aws_iam_role[0].unity_catalog_iam_arn
    external_id           = databricks_storage_credential.medallion.aws_iam_role[0].external_id
  }
}
