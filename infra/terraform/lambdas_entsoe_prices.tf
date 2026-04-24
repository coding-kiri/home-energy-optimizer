resource "aws_sqs_queue" "entsoe_prices_dlq" {
  name                       = "${local.prefix}-entsoe-prices-dlq"
  message_retention_seconds  = 1209600 # 14 days
}

resource "aws_lambda_function" "entsoe_prices" {
  function_name = "${local.prefix}-entsoe-prices"
  role          = aws_iam_role.entsoe_prices.arn
  runtime       = "python3.13"
  handler       = "handler.handler"
  timeout       = 30
  memory_size   = 256

  filename = "../../lambdas/entsoe_prices/package.zip"

  environment {
    variables = {
      RAW_BUCKET          = aws_s3_bucket.raw.bucket
      ENTSOE_SECRET_NAME  = aws_secretsmanager_secret.entsoe_token.name
    }
  }

  dead_letter_config {
    target_arn = aws_sqs_queue.entsoe_prices_dlq.arn
  }
}

resource "aws_cloudwatch_event_rule" "entsoe_prices_daily" {
  name                = "${local.prefix}-entsoe-prices-daily"
  description         = "Triggers the ENTSO-E day-ahead price ingestion Lambda once per day."
  schedule_expression = "cron(0 14 * * ? *)"
}

resource "aws_cloudwatch_event_target" "entsoe_prices" {
  rule = aws_cloudwatch_event_rule.entsoe_prices_daily.name
  arn  = aws_lambda_function.entsoe_prices.arn
}

resource "aws_lambda_permission" "entsoe_prices_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.entsoe_prices.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.entsoe_prices_daily.arn
}
