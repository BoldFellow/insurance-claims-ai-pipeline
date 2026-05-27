#!/usr/bin/env bash
# deploy.sh
# Builds Lambda zip packages, uploads them to an artifact S3 bucket, then
# creates (or updates) the insurance-claims-ai-pipeline CloudFormation stack.
#
# Usage:
#   ./scripts/deploy.sh \
#     --stack-name    insurance-claims-ai-pipeline \
#     --artifact-bucket my-deploy-artifacts-123456789012 \
#     [--adjuster-email you@example.com] \
#     [--model-id us.anthropic.claude-haiku-4-5-20251001-v1:0]
#
# Prerequisites:
#   - AWS CLI v2 configured with us-east-1 credentials
#   - Python 3 and pip available
#   - Bedrock model access already granted (script checks this first)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
STACK_NAME="insurance-claims-ai-pipeline"
ARTIFACT_BUCKET=""
ADJUSTER_EMAIL=""
MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/build"
CFN_TEMPLATE="${REPO_ROOT}/cfn/template.yaml"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name)       STACK_NAME="$2";       shift 2 ;;
    --artifact-bucket)  ARTIFACT_BUCKET="$2";  shift 2 ;;
    --adjuster-email)   ADJUSTER_EMAIL="$2";   shift 2 ;;
    --model-id)         MODEL_ID="$2";         shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [[ -z "${ARTIFACT_BUCKET}" ]]; then
  echo "ERROR: --artifact-bucket is required."
  echo "  Create a bucket first: aws s3 mb s3://my-deploy-artifacts-\$(aws sts get-caller-identity --query Account --output text)"
  exit 1
fi

echo "================================================"
echo " insurance-claims-ai-pipeline deploy"
echo "  Stack          : ${STACK_NAME}"
echo "  Artifact bucket: ${ARTIFACT_BUCKET}"
echo "  Region         : ${REGION}"
echo "  Model ID       : ${MODEL_ID}"
echo "================================================"

# ---------------------------------------------------------------------------
# Step 1: Preflight -- Bedrock model access
# ---------------------------------------------------------------------------
echo ""
echo "[1/5] Checking Bedrock model access..."
AWS_DEFAULT_REGION="${REGION}" \
  "${SCRIPT_DIR}/check-bedrock-access.sh" "${MODEL_ID}"

# ---------------------------------------------------------------------------
# Step 2: Validate CloudFormation template
# ---------------------------------------------------------------------------
echo ""
echo "[2/5] Validating CloudFormation template..."
aws cloudformation validate-template \
  --template-body "file://${CFN_TEMPLATE}" \
  --region "${REGION}" \
  --output text --query 'Description'

echo "  Template valid."

# ---------------------------------------------------------------------------
# Step 3: Build Lambda zip packages
# ---------------------------------------------------------------------------
echo ""
echo "[3/5] Building Lambda zip packages..."

LAMBDAS=(
  read_manifest
  validate_artifacts_present
  read_text
  normalize_evidence
  synthesize_verdict
  apply_guardrails
  write_artifacts
)

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

for fn in "${LAMBDAS[@]}"; do
  SRC="${REPO_ROOT}/app/lambdas/${fn}"
  PKG="${BUILD_DIR}/${fn}"
  ZIP="${BUILD_DIR}/${fn}.zip"

  echo "  Building ${fn}..."
  rm -rf "${PKG}"
  mkdir -p "${PKG}"

  # Install dependencies if requirements.txt exists and is non-empty
  if [[ -f "${SRC}/requirements.txt" ]] && \
     grep -qv '^[[:space:]]*$' "${SRC}/requirements.txt"; then
    pip install \
      --quiet \
      --require-virtualenv=false \
      -r "${SRC}/requirements.txt" \
      -t "${PKG}/"
  fi

  cp "${SRC}/handler.py" "${PKG}/"
  (cd "${PKG}" && zip -r "${ZIP}" . -x "*.pyc" -x "*/__pycache__/*" -x "*/.DS_Store")
  echo "    -> ${ZIP}"
done

# ---------------------------------------------------------------------------
# Step 4: Upload Lambda zips to artifact bucket
# ---------------------------------------------------------------------------
echo ""
echo "[4/5] Uploading Lambda zips to s3://${ARTIFACT_BUCKET}/${STACK_NAME}/lambdas/..."

for fn in "${LAMBDAS[@]}"; do
  ZIP="${BUILD_DIR}/${fn}.zip"
  S3_KEY="${STACK_NAME}/lambdas/${fn}.zip"
  aws s3 cp "${ZIP}" "s3://${ARTIFACT_BUCKET}/${S3_KEY}" \
    --region "${REGION}"
  echo "  Uploaded ${S3_KEY}"
done

# ---------------------------------------------------------------------------
# Step 5: Create or update stack
# ---------------------------------------------------------------------------
echo ""
echo "[5/5] Deploying CloudFormation stack..."

PARAMS="ParameterKey=ArtifactBucket,ParameterValue=${ARTIFACT_BUCKET}"
PARAMS="${PARAMS} ParameterKey=BedrockModelId,ParameterValue=${MODEL_ID}"

if [[ -n "${ADJUSTER_EMAIL}" ]]; then
  PARAMS="${PARAMS} ParameterKey=AdjusterEmail,ParameterValue=${ADJUSTER_EMAIL}"
fi

# Check if stack already exists
STACK_STATUS=$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].StackStatus' \
  --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [[ "${STACK_STATUS}" == "DOES_NOT_EXIST" ]]; then
  echo "  Creating new stack: ${STACK_NAME}..."
  aws cloudformation create-stack \
    --stack-name "${STACK_NAME}" \
    --template-body "file://${CFN_TEMPLATE}" \
    --parameters ${PARAMS} \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "${REGION}"

  echo "  Waiting for CREATE_COMPLETE (this takes ~3-4 minutes)..."
  aws cloudformation wait stack-create-complete \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}"
else
  echo "  Stack exists (${STACK_STATUS}). Updating..."
  aws cloudformation update-stack \
    --stack-name "${STACK_NAME}" \
    --template-body "file://${CFN_TEMPLATE}" \
    --parameters ${PARAMS} \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "${REGION}" || {
      MSG=$?
      # update-stack exits 255 if there are no changes; that is fine
      if aws cloudformation describe-stacks \
           --stack-name "${STACK_NAME}" \
           --region "${REGION}" \
           --query 'Stacks[0].StackStatus' \
           --output text 2>/dev/null | grep -q "COMPLETE"; then
        echo "  No changes to deploy -- stack already up to date."
        MSG=0
      fi
      exit ${MSG}
    }

  echo "  Waiting for UPDATE_COMPLETE..."
  aws cloudformation wait stack-update-complete \
    --stack-name "${STACK_NAME}" \
    --region "${REGION}"
fi

# ---------------------------------------------------------------------------
# Print outputs
# ---------------------------------------------------------------------------
echo ""
echo "================================================"
echo " Stack deployed successfully."
echo "================================================"
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

echo ""
echo "Next step: seed a sample claim"
echo "  ./scripts/seed-sample-claim.sh acme-corp CLM-001"
