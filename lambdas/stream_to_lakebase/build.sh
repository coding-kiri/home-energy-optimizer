#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"
PACKAGE_DIR="${BUILD_DIR}/package"
ZIP_PATH="${BUILD_DIR}/package.zip"

rm -rf "${BUILD_DIR}"
mkdir -p "${PACKAGE_DIR}"

uv pip install \
  --target "${PACKAGE_DIR}" \
  --python-platform x86_64-manylinux2014 \
  --python-version 3.12 \
  --only-binary :all: \
  "psycopg2-binary==2.9.12"

rm -f "${PACKAGE_DIR}/.lock"
cp "${SCRIPT_DIR}/handler.py" "${PACKAGE_DIR}/handler.py"

(
  cd "${PACKAGE_DIR}"
  zip -q -r "${ZIP_PATH}" .
)

echo "Built ${ZIP_PATH}"
