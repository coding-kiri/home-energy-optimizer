#!/usr/bin/env bash
# bootstrap.sh — First-time deployment of the home-energy-optimizer Terraform stack.
#
# Unity Catalog storage credentials on AWS require a two-step apply because of a
# circular dependency: the IAM role trust policy needs values (UC master role ARN
# and external ID) that are only known after the Databricks storage credential is
# created, which in turn requires the IAM role to already exist.
#
# Usage (per environment — separate tfvars and backend config per env):
#   cd terraform
#   cp terraform.tfvars.dev.example terraform.tfvars.dev
#   cp backend-dev.hcl.example backend-dev.hcl   # fill in bucket, table, region
#   bash bootstrap.sh terraform.tfvars.dev backend-dev.hcl
#
# Prod:
#   bash bootstrap.sh terraform.tfvars.prod backend-prod.hcl
#
# If you use a non-default tfvars filename, avoid keeping a conflicting
# terraform.tfvars in the same directory (Terraform merges all loaded var files).

set -euo pipefail

cd "$(dirname "$0")"

usage() {
  echo "Usage: $0 <tfvars-file> <backend-config.hcl>"
  echo "Example: $0 terraform.tfvars.dev backend-dev.hcl"
  exit 1
}

[ $# -ge 2 ] || usage

TFVARS=$1
BACKEND_CFG=$2

if [ ! -f "$TFVARS" ]; then
  echo "ERROR: Var file not found: $TFVARS"
  echo "Copy terraform.tfvars.dev.example or terraform.tfvars.prod.example and fill in values."
  exit 1
fi

if [ ! -f "$BACKEND_CFG" ]; then
  echo "ERROR: Backend config not found: $BACKEND_CFG"
  exit 1
fi

INIT_ARGS=(-upgrade -backend-config="$BACKEND_CFG")

if [ "$TFVARS" = "terraform.tfvars" ]; then
  VAR_FILE_ARGS=()
else
  VAR_FILE_ARGS=(-var-file="$TFVARS")
fi

echo "=== terraform init (${INIT_ARGS[*]}) ==="
terraform init "${INIT_ARGS[@]}"

echo ""
echo "=== Step 1: initial apply (skip_validation=true, self-assume trust only) ==="
terraform apply "${VAR_FILE_ARGS[@]}"

echo ""
echo "=== Reading bootstrap outputs ==="
UC_MASTER_ROLE_ARN=$(terraform output -raw uc_master_role_arn 2>/dev/null)
UC_EXTERNAL_ID=$(terraform output -raw uc_external_id 2>/dev/null)

echo "  uc_master_role_arn = ${UC_MASTER_ROLE_ARN}"
echo "  uc_external_id     = ${UC_EXTERNAL_ID}"

echo ""
echo "=== Appending bootstrap values to ${TFVARS} ==="
sed -i '/^#\? *uc_master_role_arn/d' "$TFVARS"
sed -i '/^#\? *uc_external_id/d' "$TFVARS"

cat >> "$TFVARS" <<EOF

# Bootstrap step 2 — written by bootstrap.sh
uc_master_role_arn = "${UC_MASTER_ROLE_ARN}"
uc_external_id     = "${UC_EXTERNAL_ID}"
EOF

echo ""
echo "=== Step 2: final apply (skip_validation=false, full trust policy) ==="
terraform apply "${VAR_FILE_ARGS[@]}"

echo ""
echo "Bootstrap complete for ${TFVARS}. The Unity Catalog storage credential is fully configured."
