resource "aws_secretsmanager_secret" "lakebase" {
  name        = "${local.prefix}-lakebase"
  description = "Lakebase connection string for the ${var.env} environment"

  lifecycle {
    ignore_changes = all
  }

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_secretsmanager_secret" "iot_certificate" {
  name        = "${local.prefix}-iot-producer-certificate"
  description = "IoT producer certificate and private key for the ${var.env} environment"

  tags = {
    Project     = var.project
    Environment = var.env
  }
}

resource "aws_secretsmanager_secret_version" "iot_certificate" {
  secret_id = aws_secretsmanager_secret.iot_certificate.id
  secret_string = jsonencode({
    certificate_pem = aws_iot_certificate.meter_concentrator.certificate_pem
    private_key     = aws_iot_certificate.meter_concentrator.private_key
  })
}
