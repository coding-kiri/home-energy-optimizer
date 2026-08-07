resource "aws_iot_thing" "meter_concentrator" {
  name = "${local.prefix}-meter-concentrator-001"

  attributes = {
    Environment = var.env
  }
}

resource "aws_iot_certificate" "meter_concentrator" {
  active = true
}

resource "aws_iot_thing_principal_attachment" "meter_concentrator" {
  principal = aws_iot_certificate.meter_concentrator.arn
  thing     = aws_iot_thing.meter_concentrator.name
}

resource "aws_iot_policy" "meter_concentrator" {
  name = "${local.prefix}-meter-concentrator"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "iot:Connect"
        Resource = "arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:client/${aws_iot_thing.meter_concentrator.name}"
      },
      {
        Effect   = "Allow"
        Action   = "iot:Publish"
        Resource = "arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:topic/$aws/rules/${aws_iot_topic_rule.smart_meter_to_kds.name}"
      },
    ]
  })
}

resource "aws_iot_policy_attachment" "meter_concentrator" {
  policy = aws_iot_policy.meter_concentrator.name
  target = aws_iot_certificate.meter_concentrator.arn
}

resource "aws_cloudwatch_log_group" "iot_rule_errors" {
  name = "/aws/iot/${local.prefix}-smart-meter-errors"

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_iam_role" "iot_rule" {
  name = "${local.prefix}-iot-rule"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "iot.amazonaws.com" }
      }
    ]
  })

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_iam_role_policy" "iot_rule" {
  name = "${local.prefix}-iot-rule"
  role = aws_iam_role.iot_rule.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "kinesis:PutRecord"
        Resource = aws_kinesis_stream.smart_meter.arn
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.iot_rule_errors.arn}:*"
      },
    ]
  })
}

resource "aws_iot_topic_rule" "smart_meter_to_kds" {
  name        = "${replace(local.prefix, "-", "_")}_smart_meter_to_kds"
  description = "Route smart-meter readings from Basic Ingest to Kinesis"
  enabled     = true
  sql         = "SELECT * FROM 'smartmeter/readings'"
  sql_version = "2016-03-23"

  kinesis {
    role_arn      = aws_iam_role.iot_rule.arn
    stream_name   = aws_kinesis_stream.smart_meter.name
    partition_key = "$${household_id}"
  }

  error_action {
    cloudwatch_logs {
      log_group_name = aws_cloudwatch_log_group.iot_rule_errors.name
      role_arn       = aws_iam_role.iot_rule.arn
    }
  }
}
