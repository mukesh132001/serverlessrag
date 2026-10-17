#!/usr/bin/env bash
# Build the Lambda dependency layer (Linux x86_64, Python 3.11) into lambda_layer/python.
# The built layer is gitignored; run this before `cdk deploy`.
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf lambda_layer/python
mkdir -p lambda_layer/python
pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --target lambda_layer/python \
  -r requirements-layer.txt

echo "Layer built in lambda_layer/python"
