resource "aws_secretsmanager_secret" "entsoe_token" {
  name        = "entsoe/token"
  description = "ENTSO-E Transparency Platform API token. Set the value manually after apply."
}
