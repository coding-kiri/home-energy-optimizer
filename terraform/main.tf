terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  prefix = "${var.project}-${var.env}"

  buckets = {
    raw    = "${local.prefix}-raw"    # landing zone for ingested source files
    bronze = "${local.prefix}-bronze" # medallion layer 1 — raw, unvalidated data
    silver = "${local.prefix}-silver" # medallion layer 2 — cleaned, validated data
    gold   = "${local.prefix}-gold"   # medallion layer 3 — aggregated, business-ready data
  }
}

resource "aws_s3_bucket" "medallion" {
  for_each = local.buckets

  bucket = each.value

  tags = {
    Project     = var.project
    Environment = var.env
    Layer       = each.key
  }
}

resource "aws_s3_bucket_versioning" "medallion" {
  for_each = aws_s3_bucket.medallion

  bucket = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "medallion" {
  for_each = aws_s3_bucket.medallion

  bucket = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "medallion" {
  for_each = aws_s3_bucket.medallion

  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
