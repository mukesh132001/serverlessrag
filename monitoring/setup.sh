#!/usr/bin/env bash
# Start the monitoring stack and point the uptime probe at your API.
#   ./setup.sh https://<api-id>.execute-api.<region>.amazonaws.com/prod/
# Local dev (scripts/local_api.py):  ./setup.sh http://host.docker.internal:8000/
set -euo pipefail
cd "$(dirname "$0")"
API_URL="${1:?usage: ./setup.sh <API base URL>}"
API_URL="${API_URL%/}"

cat > prometheus/targets/blackbox.yml <<YAML
- targets:
    - ${API_URL}/health
  labels:
    service: serverlessrag-api
YAML
echo "Uptime probe target: ${API_URL}/health"

docker compose up -d
echo
echo "Grafana:      http://localhost:3000  (admin / \${GRAFANA_ADMIN_PASSWORD:-admin})"
echo "Prometheus:   http://localhost:9090"
echo "Alertmanager: http://localhost:9093"
echo "OTLP:         http://<this host>:4318  -> set OTEL_EXPORTER_OTLP_ENDPOINT on the Lambdas"
