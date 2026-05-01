#!/usr/bin/env bash
# bootstrap.sh — First-time deployment of the home-energy-optimizer Terraform stack.
#
# Unity Catalog storage credentials on AWS require a two-step apply because of a
# circular dependency: the IAM role trust policy needs values (UC master role ARN
# and external ID) that are only known after the Databricks storage credential is
# created, which in turn requires the IAM role to already exist.
#
# Usage:
#   cd terraform
#   cp terraform.tfvars.example terraform.tfvars   # fill in env, aws_region, etc.
#   bash bootstrap.sh

set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f terraform.tfvars ]; then
  echo "ERROR: terraform.tfvars not found."
  echo "Copy terraform.tfvars.example to terraform.tfvars and fill in your values first."
  exit 1
fi

echo "=== Step 1: initial apply (skip_validation=true, self-assume trust only) ==="
terraform init -upgrade
terraform apply

echo ""
echo "=== Reading bootstrap outputs ==="
UC_MASTER_ROLE_ARN=$(terraform output -raw -json databricks_credential | python3 -c "import sys,json; print(json.load(sys.stdin)['unity_catalog_iam_arn'])")
UC_EXTERNAL_ID=$(terraform output -raw -json databricks_credential | python3 -c "import sys,json; print(json.load(sys.stdin)['external_id'])")

echo "  uc_master_role_arn = ${UC_MASTER_ROLE_ARN}"
echo "  uc_external_id     = ${UC_EXTERNAL_ID}"

echo ""
echo "=== Appending bootstrap values to terraform.tfvars ==="
# Remove any existing (commented or uncommented) bootstrap lines first
sed -i '/^#\? *uc_master_role_arn/d' terraform.tfvars
sed -i '/^#\? *uc_external_id/d' terraform.tfvars

cat >> terraform.tfvars <<EOF

# Bootstrap step 2 — written by bootstrap.sh
uc_master_role_arn = "${UC_MASTER_ROLE_ARN}"
uc_external_id     = "${UC_EXTERNAL_ID}"
EOF

echo ""
echo "=== Step 2: final apply (skip_validation=false, full trust policy) ==="
terraform apply

echo ""
echo "Bootstrap complete. The Unity Catalog storage credential is fully configured."
