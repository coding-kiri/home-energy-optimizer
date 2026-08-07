resource "aws_kinesis_stream" "smart_meter" {
  name             = "${local.prefix}-smart-meter"
  shard_count      = 1
  retention_period = 24

  stream_mode_details {
    stream_mode = "PROVISIONED"
  }

  encryption_type = "KMS"
  kms_key_id      = "alias/aws/kinesis"

  tags = {
    Project     = var.project
    Environment = var.env
  }
}
