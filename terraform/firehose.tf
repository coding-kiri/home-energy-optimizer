resource "aws_cloudwatch_log_group" "firehose" {
  name = "/aws/kinesisfirehose/${local.prefix}-smart-meter"

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_cloudwatch_log_stream" "firehose" {
  name           = "S3Delivery"
  log_group_name = aws_cloudwatch_log_group.firehose.name
}

resource "aws_iam_role" "firehose_source" {
  name = "${local.prefix}-firehose-source"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "firehose.amazonaws.com" }
      }
    ]
  })

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_iam_role_policy" "firehose_source" {
  name = "${local.prefix}-kinesis-read"
  role = aws_iam_role.firehose_source.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "kinesis:DescribeStream",
          "kinesis:DescribeStreamSummary",
          "kinesis:GetRecords",
          "kinesis:GetShardIterator",
          "kinesis:ListShards",
        ]
        Resource = aws_kinesis_stream.smart_meter.arn
      }
    ]
  })
}

resource "aws_iam_role" "firehose_delivery" {
  name = "${local.prefix}-firehose-delivery"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "firehose.amazonaws.com" }
      }
    ]
  })

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_iam_role_policy" "firehose_delivery" {
  name = "${local.prefix}-s3-delivery"
  role = aws_iam_role.firehose_delivery.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetBucketLocation",
          "s3:ListBucket",
          "s3:ListBucketMultipartUploads",
        ]
        Resource = aws_s3_bucket.medallion["raw"].arn
        Condition = {
          StringLike = {
            "s3:prefix" = [
              "raw/smart_meter/*",
              "raw/smart_meter_errors/*",
            ]
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "s3:AbortMultipartUpload",
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = [
          "${aws_s3_bucket.medallion["raw"].arn}/raw/smart_meter/*",
          "${aws_s3_bucket.medallion["raw"].arn}/raw/smart_meter_errors/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = "logs:PutLogEvents"
        Resource = "${aws_cloudwatch_log_stream.firehose.arn}:*"
      },
    ]
  })
}

resource "aws_kinesis_firehose_delivery_stream" "smart_meter" {
  name        = "${local.prefix}-smart-meter"
  destination = "extended_s3"

  kinesis_source_configuration {
    kinesis_stream_arn = aws_kinesis_stream.smart_meter.arn
    role_arn           = aws_iam_role.firehose_source.arn
  }

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose_delivery.arn
    bucket_arn          = aws_s3_bucket.medallion["raw"].arn
    prefix              = "raw/smart_meter/date=!{timestamp:yyyy-MM-dd}/hour=!{timestamp:HH}/"
    error_output_prefix = "raw/smart_meter_errors/"
    buffering_interval  = 300
    buffering_size      = 5
    compression_format  = "UNCOMPRESSED"

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose.name
      log_stream_name = aws_cloudwatch_log_stream.firehose.name
    }
  }

  depends_on = [
    aws_iam_role_policy.firehose_delivery,
    aws_iam_role_policy.firehose_source,
  ]

  tags = {
    Project     = var.project
    Environment = var.env
  }
}
