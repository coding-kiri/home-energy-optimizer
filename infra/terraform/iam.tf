data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "entsoe_prices" {
  name               = "${local.prefix}-entsoe-prices"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "entsoe_prices" {
  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:*:*:*"]
  }

  statement {
    sid     = "S3Write"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw.arn}/raw/entsoe_prices/*"]
  }

  statement {
    sid     = "ReadToken"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.entsoe_token.arn]
  }

  statement {
    sid     = "SendToDlq"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.entsoe_prices_dlq.arn]
  }
}

resource "aws_iam_role_policy" "entsoe_prices" {
  name   = "${local.prefix}-entsoe-prices"
  role   = aws_iam_role.entsoe_prices.id
  policy = data.aws_iam_policy_document.entsoe_prices.json
}
