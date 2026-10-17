#!/usr/bin/env bash
# One-command deploy: builds the Lambda layer, deploys the CDK stack, prints the endpoints.
# Extra CDK options are passed through, e.g. ./deploy.sh -c allowedOrigin=http://my-ui-host
set -euo pipefail
cd "$(dirname "$0")"

command -v aws >/dev/null || { echo "AWS CLI not found. Install it first."; exit 1; }
command -v npm >/dev/null || { echo "Node.js/npm not found. Install Node 18+ first."; exit 1; }
aws sts get-caller-identity >/dev/null || { echo "AWS credentials not configured. Run 'aws configure'."; exit 1; }

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="${CDK_DEFAULT_REGION:-us-east-1}"
echo "Account: $ACCOUNT_ID  Region: $REGION"

./scripts/build_layer.sh

cd infrastructure
npm install
npx cdk bootstrap "aws://$ACCOUNT_ID/$REGION"
npx cdk deploy ServerlessRagStack --require-approval never "$@"
cd ..

output() {
  aws cloudformation describe-stacks --stack-name ServerlessRagStack --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
API_URL=$(output ApiEndpoint)
UI_URL=$(output ChatUIUrl)

echo
echo "API:     ${API_URL}"
echo "Chat UI: ${UI_URL}  (allow a few minutes for the instance to finish setup)"
echo
echo "Try it:"
echo "  curl -X POST '${API_URL}ingest' -H 'Content-Type: application/json' -d '{\"query\": \"retrieval augmented generation\", \"max_results\": 10}'"
echo "  curl -X POST '${API_URL}chat'   -H 'Content-Type: application/json' -d '{\"query\": \"How do RAG systems reduce hallucination?\"}'"
echo
echo "Clean up when done (Aurora and the NAT gateway cost money while running):"
echo "  cd infrastructure && npx cdk destroy ServerlessRagStack"
