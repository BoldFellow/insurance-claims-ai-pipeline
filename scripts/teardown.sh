#!/usr/bin/env bash
# teardown.sh
# Empties both S3 buckets (including all versions), deletes the CloudFormation
# stack, and verifies cleanup. The DynamoDB table has DeletionPolicy: Retain --
# delete it manually if desired.
#
# Usage: ./scripts/teardown.sh [--stack-name insurance-claims-ai-pipeline]

set -euo pipefail

STACK_NAME="insurance-claims-ai-pipeline"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

echo "================================================"
echo " insurance-claims-ai-pipeline teardown"
echo "  Stack : ${STACK_NAME}"
echo "  Region: ${REGION}"
echo "================================================"
echo ""
echo "This will:"
echo "  - Delete ALL objects and versions in both S3 buckets"
echo "  - Delete the CloudFormation stack (IAM roles, Lambdas, SFN, SNS, KMS)"
echo "  - The DynamoDB table will be RETAINED (DeletionPolicy: Retain)"
echo "    Delete it manually if needed:"
echo "    aws dynamodb delete-table --table-name ${STACK_NAME}-ClaimsDecisions"
echo ""
read -r -p "Type 'yes' to continue: " CONFIRM
if [[ "${CONFIRM}" != "yes" ]]; then
  echo "Aborted."
  exit 0
fi

# ---------------------------------------------------------------------------
# Helper: empty a versioned S3 bucket
# ---------------------------------------------------------------------------
empty_bucket() {
  local BUCKET="$1"

  echo ""
  echo "  Emptying s3://${BUCKET}..."

  # Check if bucket exists
  if ! aws s3api head-bucket --bucket "${BUCKET}" --region "${REGION}" 2>/dev/null; then
    echo "  Bucket ${BUCKET} does not exist or is not accessible. Skipping."
    return 0
  fi

  # Delete all object versions in batches of 1000
  while true; do
    VERSIONS=$(aws s3api list-object-versions \
      --bucket "${BUCKET}" \
      --region "${REGION}" \
      --max-items 1000 \
      --output json \
      --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' 2>/dev/null || echo '{"Objects":[]}')

    COUNT=$(echo "${VERSIONS}" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('Objects') or []))")
    if [[ "${COUNT}" == "0" ]]; then break; fi

    echo "    Deleting ${COUNT} versions..."
    echo "${VERSIONS}" | aws s3api delete-objects \
      --bucket "${BUCKET}" \
      --delete "$(cat)" \
      --region "${REGION}" \
      --output text > /dev/null
  done

  # Delete all delete markers
  while true; do
    MARKERS=$(aws s3api list-object-versions \
      --bucket "${BUCKET}" \
      --region "${REGION}" \
      --max-items 1000 \
      --output json \
      --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' 2>/dev/null || echo '{"Objects":[]}')

    COUNT=$(echo "${MARKERS}" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('Objects') or []))")
    if [[ "${COUNT}" == "0" ]]; then break; fi

    echo "    Deleting ${COUNT} delete markers..."
    echo "${MARKERS}" | aws s3api delete-objects \
      --bucket "${BUCKET}" \
      --delete "$(cat)" \
      --region "${REGION}" \
      --output text > /dev/null
  done

  echo "  Bucket ${BUCKET} emptied."
}

# ---------------------------------------------------------------------------
# Step 1: Get bucket names from stack outputs
# ---------------------------------------------------------------------------
echo ""
echo "[1/3] Retrieving bucket names from stack outputs..."

INTAKE_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`IntakeBucketName`].OutputValue' \
  --output text 2>/dev/null || echo "")

DECISIONS_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`DecisionsBucketName`].OutputValue' \
  --output text 2>/dev/null || echo "")

if [[ -z "${INTAKE_BUCKET}" ]]; then
  INTAKE_BUCKET="${STACK_NAME}-intake-$(aws sts get-caller-identity --query Account --output text)"
  echo "  Could not get bucket from outputs. Using derived name: ${INTAKE_BUCKET}"
fi
if [[ -z "${DECISIONS_BUCKET}" ]]; then
  DECISIONS_BUCKET="${STACK_NAME}-decisions-$(aws sts get-caller-identity --query Account --output text)"
  echo "  Could not get bucket from outputs. Using derived name: ${DECISIONS_BUCKET}"
fi

# ---------------------------------------------------------------------------
# Step 2: Empty S3 buckets
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] Emptying S3 buckets (versioning requires explicit version deletion)..."
empty_bucket "${INTAKE_BUCKET}"
empty_bucket "${DECISIONS_BUCKET}"

# ---------------------------------------------------------------------------
# Step 3: Delete stack
# ---------------------------------------------------------------------------
echo ""
echo "[3/3] Deleting CloudFormation stack: ${STACK_NAME}..."
aws cloudformation delete-stack \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}"

echo "  Waiting for DELETE_COMPLETE (2-3 minutes)..."
aws cloudformation wait stack-delete-complete \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}"

echo ""
echo "================================================"
echo " Teardown complete."
echo "================================================"
echo ""
echo "Items NOT deleted (by design):"
echo "  - DynamoDB table: ${STACK_NAME}-ClaimsDecisions (DeletionPolicy: Retain)"
echo "  - KMS key: in PENDING_DELETION (30-day default waiting period)"
echo ""
echo "To delete the DynamoDB table:"
echo "  aws dynamodb delete-table --table-name ${STACK_NAME}-ClaimsDecisions"
echo ""
echo "Verify no orphan resources remain:"
echo "  aws cloudformation describe-stacks --stack-name ${STACK_NAME} 2>&1"
echo "  aws logs describe-log-groups --log-group-name-prefix /aws/lambda/${STACK_NAME}"
echo "  aws logs describe-log-groups --log-group-name-prefix /aws/states/${STACK_NAME}"
