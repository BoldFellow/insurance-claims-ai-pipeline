#!/usr/bin/env bash
# seed-sample-claim.sh
# Uploads sample claim artifacts to the intake bucket and triggers the pipeline
# by uploading manifest.json last.
#
# Usage:
#   ./scripts/seed-sample-claim.sh <client-id> <claim-id> [--red-flag] [--stack-name <name>]
#
# Example:
#   ./scripts/seed-sample-claim.sh acme-corp CLM-001
#   ./scripts/seed-sample-claim.sh acme-corp CLM-002 --red-flag
#
# COST WARNING: Each run incurs ~$0.07 (Textract dominates). Do NOT loop this
# script. Set a billing alarm before running (see guide.md S4).

set -euo pipefail

CLIENT_ID="${1:-}"
CLAIM_ID="${2:-}"
RED_FLAG=false
STACK_NAME="insurance-claims-ai-pipeline"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

if [[ -z "${CLIENT_ID}" || -z "${CLAIM_ID}" ]]; then
  echo "Usage: ./scripts/seed-sample-claim.sh <client-id> <claim-id> [--red-flag] [--stack-name <name>]"
  exit 1
fi

shift 2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --red-flag)   RED_FLAG=true;      shift ;;
    --stack-name) STACK_NAME="$2";    shift 2 ;;
    *) echo "Unknown argument: $1";   exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAMPLES_DIR="${SCRIPT_DIR}/samples"

if [[ "${RED_FLAG}" == "true" ]]; then
  SAMPLES_DIR="${SAMPLES_DIR}/red-flag"
  echo "Using red-flag sample set (demonstrates guardrail downgrade to NEEDS_REVIEW)"
fi

# Rate-limit guard: if this client/claim combo was seeded in the last 60 seconds, require CONFIRM=yes
SEED_STAMP_FILE="/tmp/.seed-${STACK_NAME}-${CLIENT_ID}-${CLAIM_ID}"
if [[ -f "${SEED_STAMP_FILE}" ]]; then
  AGE=$(( $(date +%s) - $(date -r "${SEED_STAMP_FILE}" +%s 2>/dev/null || echo 0) ))
  if [[ ${AGE} -lt 60 && "${CONFIRM:-}" != "yes" ]]; then
    echo ""
    echo "WARNING: This client/claim combination was seeded less than 60 seconds ago."
    echo "  Seeding repeatedly creates redundant Bedrock + Textract charges (~\$0.07 each)."
    echo "  Re-run with CONFIRM=yes to override: CONFIRM=yes ./scripts/seed-sample-claim.sh ..."
    echo ""
    exit 1
  fi
fi
touch "${SEED_STAMP_FILE}"

# Get intake bucket from stack outputs
INTAKE_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`IntakeBucketName`].OutputValue' \
  --output text)

if [[ -z "${INTAKE_BUCKET}" ]]; then
  echo "ERROR: Could not retrieve IntakeBucketName from stack ${STACK_NAME}."
  echo "  Is the stack deployed? Run ./scripts/deploy.sh first."
  exit 1
fi

PREFIX="clients/${CLIENT_ID}/${CLAIM_ID}"
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo ""
echo "================================================"
echo " Seeding sample claim"
echo "  Bucket   : ${INTAKE_BUCKET}"
echo "  Prefix   : ${PREFIX}"
echo "  Red flag : ${RED_FLAG}"
echo "================================================"

# ---------------------------------------------------------------------------
# Upload artifacts first, manifest last (manifest-last convention)
# The EventBridge rule fires only on manifest.json, so we must upload all
# artifacts before dropping the manifest.
# ---------------------------------------------------------------------------

# Photo
PHOTO="${SAMPLES_DIR}/photo-damage.jpg"
if [[ -f "${PHOTO}" ]]; then
  echo "  Uploading photo-damage.jpg..."
  aws s3 cp "${PHOTO}" \
    "s3://${INTAKE_BUCKET}/${PREFIX}/photo-damage.jpg" \
    --region "${REGION}"
fi

# Police report
REPORT="${SAMPLES_DIR}/police-report.pdf"
if [[ -f "${REPORT}" ]]; then
  echo "  Uploading police-report.pdf..."
  aws s3 cp "${REPORT}" \
    "s3://${INTAKE_BUCKET}/${PREFIX}/police-report.pdf" \
    --region "${REGION}"
fi

# Statement
STATEMENT="${SAMPLES_DIR}/statement.txt"
if [[ -f "${STATEMENT}" ]]; then
  echo "  Uploading statement.txt..."
  aws s3 cp "${STATEMENT}" \
    "s3://${INTAKE_BUCKET}/${PREFIX}/statement.txt" \
    --region "${REGION}"
fi

# Manifest -- last, triggers the pipeline
# Build manifest inline so submitted_at is current
MANIFEST=$(python3 - <<PYEOF
import json, sys
manifest = {
    "schema_version": "1",
    "client_id": "${CLIENT_ID}",
    "claim_id": "${CLAIM_ID}",
    "submitted_at": "${NOW}",
    "artifacts": [
        {"type": "image",    "key": "${PREFIX}/photo-damage.jpg"},
        {"type": "document", "key": "${PREFIX}/police-report.pdf"},
        {"type": "text",     "key": "${PREFIX}/statement.txt"}
    ]
}
print(json.dumps(manifest, indent=2))
PYEOF
)

MANIFEST_TMPFILE=$(mktemp /tmp/manifest-XXXXXX.json)
trap 'rm -f "${MANIFEST_TMPFILE}"' EXIT
echo "${MANIFEST}" > "${MANIFEST_TMPFILE}"

echo "  Uploading manifest.json (triggers pipeline)..."
aws s3 cp "${MANIFEST_TMPFILE}" \
  "s3://${INTAKE_BUCKET}/${PREFIX}/manifest.json" \
  --region "${REGION}"

echo ""
echo "================================================"
echo " Claim seeded. Pipeline should start within ~5s."
echo "================================================"
echo ""
echo "Watch the execution:"
STATE_MACHINE_ARN=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`StateMachineArn`].OutputValue' \
  --output text)

echo "  State machine: ${STATE_MACHINE_ARN}"
echo ""
echo "  Console: https://console.aws.amazon.com/states/home?region=${REGION}#/statemachines/view/$(python3 -c "import urllib.parse; print(urllib.parse.quote('${STATE_MACHINE_ARN}', safe=''))")"
echo ""
echo "List recent executions:"
echo "  aws stepfunctions list-executions --state-machine-arn ${STATE_MACHINE_ARN} --region ${REGION} --output table"
echo ""
echo "After completion, inspect results:"
DECISIONS_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`DecisionsBucketName`].OutputValue' \
  --output text)
TABLE=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`TableName`].OutputValue' \
  --output text)

echo "  S3 decision  : aws s3 cp s3://${DECISIONS_BUCKET}/${PREFIX}/decision.json -"
echo "  Adjuster note: aws s3 cp s3://${DECISIONS_BUCKET}/${PREFIX}/adjuster_email.md -"
echo "  DynamoDB     : aws dynamodb get-item --table-name ${TABLE} \\"
echo "                   --key '{\"client_id\":{\"S\":\"${CLIENT_ID}\"},\"claim_id\":{\"S\":\"${CLAIM_ID}\"}}' \\"
echo "                   --region ${REGION}"
