#!/usr/bin/env bash
# check-bedrock-access.sh
# Verifies that Bedrock model access is granted for the target inference profile.
# Called by deploy.sh before stack creation. Also callable standalone.
#
# Usage: ./check-bedrock-access.sh [model-id]
# Default model: us.anthropic.claude-haiku-4-5-20251001-v1:0
#
# Exit 0 = access confirmed
# Exit 1 = access denied or error

set -euo pipefail

MODEL_ID="${1:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

echo "[check-bedrock-access] Region : ${REGION}"
echo "[check-bedrock-access] Model  : ${MODEL_ID}"

PAYLOAD='{"messages":[{"role":"user","content":"ping"}],"max_tokens":5,"anthropic_version":"bedrock-2023-05-31"}'

TMPFILE=$(mktemp /tmp/bedrock-check-XXXXXX.json)
trap 'rm -f "${TMPFILE}"' EXIT

HTTP_CODE=$(aws bedrock-runtime invoke-model \
  --region "${REGION}" \
  --model-id "${MODEL_ID}" \
  --body "${PAYLOAD}" \
  --content-type "application/json" \
  --accept "application/json" \
  "${TMPFILE}" \
  --output text \
  --query 'ResponseMetadata.HTTPStatusCode' 2>&1) || true

if grep -q '"type":"message"' "${TMPFILE}" 2>/dev/null || \
   echo "${HTTP_CODE}" | grep -q '^200'; then
  echo "[check-bedrock-access] OK -- model access confirmed."
  exit 0
fi

echo ""
echo "ERROR: Bedrock model access check failed."
echo "  Model : ${MODEL_ID}"
echo "  Region: ${REGION}"
echo ""
echo "To fix:"
echo "  1. Open the AWS Console -> Amazon Bedrock -> Model access"
echo "  2. Click 'Modify model access' and request access for Anthropic Claude Haiku 4.5"
echo "  3. Wait for access to be granted (usually instant for Haiku)"
echo "  4. Re-run this script or re-run deploy.sh"
echo ""
echo "If the model ID shown above looks outdated, verify the current ID in:"
echo "  AWS Console -> Bedrock -> Cross-region inference -> Inference profiles"
echo ""
exit 1
