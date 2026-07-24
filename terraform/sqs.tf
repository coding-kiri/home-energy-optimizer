resource "aws_sqs_queue" "smart_meter_consumer_dlq" {
  name                      = "${local.prefix}-smart-meter-consumer-dlq"
  message_retention_seconds = 1209600

  tags = {
    Project     = var.project
    Environment = var.env
  }
}
