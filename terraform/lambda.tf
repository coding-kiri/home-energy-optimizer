locals {
  # The platform-specific zip is gitignored; run build.sh before planning or applying.
  stream_to_lakebase_package = "${path.module}/../lambdas/stream_to_lakebase/build/package.zip"
}

resource "aws_cloudwatch_log_group" "stream_to_lakebase" {
  name              = "/aws/lambda/${local.prefix}-stream-to-lakebase"
  retention_in_days = 14

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_iam_role" "stream_to_lakebase" {
  name = "${local.prefix}-stream-to-lakebase"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "lambda.amazonaws.com" }
      }
    ]
  })

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_iam_role_policy" "stream_to_lakebase" {
  name = "${local.prefix}-stream-to-lakebase"
  role = aws_iam_role.stream_to_lakebase.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.lakebase.arn
      },
      {
        Effect = "Allow"
        Action = [
          "kinesis:DescribeStream",
          "kinesis:GetRecords",
          "kinesis:GetShardIterator",
          "kinesis:ListShards",
        ]
        Resource = aws_kinesis_stream.smart_meter.arn
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.smart_meter_consumer_dlq.arn
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.stream_to_lakebase.arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "stream_to_lakebase" {
  function_name = "${local.prefix}-stream-to-lakebase"
  description   = "Insert smart-meter readings from Kinesis into Lakebase"
  role          = aws_iam_role.stream_to_lakebase.arn
  handler       = "handler.handler"
  runtime       = "python3.12"
  architectures = ["x86_64"]
  memory_size   = 512
  timeout       = 60

  filename         = local.stream_to_lakebase_package
  source_code_hash = filebase64sha256(local.stream_to_lakebase_package)

  environment {
    variables = {
      SECRET_ARN = aws_secretsmanager_secret.lakebase.arn
      PG_TABLE   = "meter_readings"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.stream_to_lakebase,
    aws_iam_role_policy.stream_to_lakebase,
  ]

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_lambda_event_source_mapping" "smart_meter" {
  event_source_arn                   = aws_kinesis_stream.smart_meter.arn
  function_name                      = aws_lambda_function.stream_to_lakebase.arn
  batch_size                         = 500
  maximum_batching_window_in_seconds = 60
  starting_position                  = "LATEST"
  maximum_retry_attempts             = 3
  bisect_batch_on_function_error     = true

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.smart_meter_consumer_dlq.arn
    }
  }
}
