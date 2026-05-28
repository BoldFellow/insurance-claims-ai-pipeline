# insurance-claims-ai-pipeline -- Lab Guide

## Overview

You will build an event-driven AI pipeline that processes insurance claims end-to-end
without a single piece of custom orchestration code. When a claim manifest is uploaded
to S3, EventBridge triggers a Step Functions Standard workflow that:

1. Reads and validates the manifest
2. Fans out per-artifact to three AWS AI services in parallel (Rekognition for images,
   Textract for documents, a Lambda reader for text)
3. Normalizes the evidence and calls Bedrock Claude to synthesize a recommendation
   with reasoning and a draft adjuster email
4. Applies deterministic guardrails that override low-confidence or DENY signals
   to NEEDS_REVIEW
5. Persists decision artifacts to S3 and DynamoDB
6. Sends an SNS notification to the adjuster when human review is required

By the end of the lab you will understand:
- Step Functions Map state fan-out and direct AWS SDK integrations
- Rekognition DetectLabels and Textract AnalyzeDocument in production IAM context
- Bedrock cross-region inference profiles and structured LLM output
- The "low-trust signal" guardrail pattern and why it differs from a rules-engine DENY
- Manifest-last upload conventions and their failure modes

---

## S0 -- Prerequisites

### AWS Account
- Region: **us-east-1 only**. The Bedrock cross-region inference profile used here
  spans us-east-1, us-east-2, and us-west-2. All stack resources deploy to us-east-1.
- Permissions: AdministratorAccess (or equivalent for CloudFormation, IAM, S3,
  DynamoDB, SFN, Bedrock, SNS, Rekognition, Textract, EventBridge, CloudWatch, KMS).

### Tools
- **AWS CLI v2** -- `aws --version` must show 2.x
- **drawio CLI (optional)** -- version 30.0.2+, for exporting architecture.png
  Install: `brew install drawio` (macOS) or see drawio GitHub releases

### Cost guardrails -- READ BEFORE CONTINUING

Each pipeline run costs approximately **$0.07** (dominated by Textract ~$0.065/page).
Set a CloudWatch billing alarm NOW before you upload anything:

```
aws budgets create-budget \
  --account-id $(aws sts get-caller-identity --query Account --output text) \
  --budget '{"BudgetName":"lab-guard","BudgetLimit":{"Amount":"5","Unit":"USD"},
             "TimeUnit":"MONTHLY","BudgetType":"COST"}' \
  --notifications-with-subscribers '[{
    "Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN",
                    "Threshold":80,"ThresholdType":"PERCENTAGE"},
    "Subscribers":[{"SubscriptionType":"EMAIL","Address":"YOUR_EMAIL"}]}]'
```

**Do NOT loop claim uploads in a shell.** 100 runs = ~$7, mostly Textract.

### Bedrock model access

Haiku 4.5 is used by default. Verify the inference profile ID is still current:

1. Open AWS Console -> **Amazon Bedrock** -> **Cross-region inference** -> Inference profiles
2. Locate the `us.anthropic.claude-haiku-4-5-*` profile. Copy the profile ID.
3. If it differs from the default in `cfn/template.yaml`, pass it via
   `--parameter-overrides BedrockModelId=...` when deploying (see S3).

**IMPORTANT**: Model IDs and profile IDs change without deprecation notice. The ID
in this repo was current at time of writing (2026-05-27) but may have been superseded.

---

## S1 -- Architecture walkthrough

```
Upload artifacts:                    Step Functions Standard Workflow
clients/{cid}/{claim_id}/            +---------------------------------+
  photo-damage.jpg                   | ReadManifest (Lambda)           |
  police-report.pdf    S3 Event      | ValidateArtifactsPresent (Lmbd) |
  statement.txt        Bridge        | ProcessArtifacts (Map state)    |
  manifest.json -----> Rule -------> |   image  -> Rekognition         |
                                     |   doc    -> Textract            |
                                     |   text   -> ReadText Lambda     |
                                     | NormalizeEvidence (Lambda)      |
                                     | SynthesizeVerdict (Lambda)      |
                                     |   -> Bedrock Claude Haiku       |
                                     | ApplyGuardrails (Lambda)        |
                                     | WriteArtifacts (Lambda)         |
                                     | ShouldNotifyAdjuster (Choice)   |
                                     |   NEEDS_REVIEW -> SNS Publish   |
                                     +---------------------------------+
                                              |
              s3://decisions/clients/{cid}/{claim_id}/
                decision.json
                adjuster_email.md
                evidence_bundle.json    DynamoDB: ClaimsDecisions
                                        SNS: AdjusterNotifications
```

Key architectural decisions:
- **Standard workflow** (not Express): 90-day execution history, per-state billing.
  At ~25 state transitions per run, cost is ~$0.0006/run -- not a concern at lab scale.
- **Map state with MaxConcurrency=10**: all artifacts processed in parallel. The
  pipeline does not wait for one image before starting the next document.
- **Direct SDK integrations** for Rekognition, Textract, DynamoDB, SNS: no Lambda
  wrapper needed for these calls. The ASL `arn:aws:states:::aws-sdk:*` pattern
  calls the service directly, with the state machine role as the IAM principal.
- **Manifest-last convention**: EventBridge fires only on `manifest.json` writes.
  This is a *convention*, not an atomicity guarantee. S3 PUTs can be reordered.
  The `ValidateArtifactsPresent` state (HeadObject on every declared artifact) is
  the actual guard against partial-evidence runs.

---

## S2 -- Bedrock model availability

**The Bedrock Model access page has been retired by AWS.** Anthropic foundation models
(including Haiku 4.5) are now automatically enabled in all commercial AWS regions on
first invocation. You do not need to manually grant model access.

If you navigate to Amazon Bedrock in the console you may see a simplified access page
or no model access section at all -- this is expected. The models are available.

To confirm the model is available in your account:

```bash
aws bedrock list-foundation-models \
  --query 'modelSummaries[?contains(modelId,`claude-haiku-4-5`)].{id:modelId,status:modelLifecycleStatus}' \
  --output table --region us-east-1
```

This should return at least one row with `ACTIVE` status. If the command returns nothing
or the model shows `LEGACY`, check the Bedrock console for any account-level restrictions.

Why cross-region inference profiles need multi-region IAM:
The `us.anthropic.claude-haiku-4-5-*` profile is a *cross-region inference profile*.
When you invoke it, Bedrock may route the request to us-east-1, us-east-2, or us-west-2
based on capacity. The IAM policy in the stack grants `bedrock:InvokeModel` on the
profile ARN *and* on the underlying foundation-model ARNs in all three regions.
Missing any one throws `AccessDeniedException` at runtime -- the error points to the
target region, not the region where the stack is deployed.

---

## S3 -- Deploy the stack

All Lambda code is embedded inline in `cfn/template.yaml` via `Code.ZipFile`.
There is no build step and no separate Lambda packaging -- just upload the template
and create the stack.

**Step 1**: Verify the foundation model is available in your account:

```bash
aws bedrock get-foundation-model \
  --model-identifier anthropic.claude-haiku-4-5-20251001-v1:0 \
  --region us-east-1 \
  --query 'modelDetails.modelLifecycle' \
  --output table
```

Note: use the foundation model ID (`anthropic.claude-haiku-4-5-...`, no `us.` prefix)
not the inference profile ID. The cross-region profile ID (`us.anthropic...`) is used
at runtime in the `BedrockModelId` parameter, but `get-foundation-model` requires the
underlying foundation model ID.

If this returns `ResourceNotFoundException`, the model ID may have changed. Look up the
current Haiku foundation model ID:
```bash
aws bedrock list-foundation-models \
  --query 'modelSummaries[?contains(modelId,`claude-haiku`)].modelId' \
  --output table --region us-east-1
```

**Step 2**: Create a staging bucket for the template (required -- the template
exceeds the 51 KB inline limit for `--template-body`):

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws s3 mb s3://cfn-templates-${ACCOUNT} --region us-east-1
```

**Step 3**: Deploy the stack:

```bash
# NOTE: aws cloudformation validate-template --template-body has a 51 KB limit.
# This template is ~60 KB. Skip the local validate step -- aws cloudformation deploy
# validates the template automatically after uploading it to S3.

# Deploy (uploads template to S3 automatically, validates, then creates the stack)
aws cloudformation deploy \
  --template-file cfn/template.yaml \
  --stack-name insurance-claims-ai-pipeline \
  --s3-bucket cfn-templates-${ACCOUNT} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameter-overrides \
      BedrockModelId=us.anthropic.claude-haiku-4-5-20251001-v1:0 \
      AdjusterEmail=you@example.com
```

`aws cloudformation deploy` is idempotent -- re-run it to apply any changes to
`cfn/template.yaml` (Lambda code edits, parameter changes, etc.).

Stack creation takes approximately 3-4 minutes. When complete, print the outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table
```

Save `IntakeBucketName` and `DecisionsBucketName` -- you will use them in S5.

---

## S4 -- Cost guardrails before seeding

Before uploading any claim artifacts, confirm the billing alarm from S0 is active.
Then check your current month's spend:

```bash
aws ce get-cost-and-usage \
  --time-period Start=$(date +%Y-%m-01),End=$(date +%Y-%m-%d) \
  --granularity MONTHLY \
  --metrics "UnblendedCost" \
  --output table
```

Per-run cost breakdown (3 artifacts: 1 image, 1 single-page PDF, 1 text):

| Service | Cost |
|---------|------|
| Rekognition DetectLabels | ~$0.001 |
| Textract AnalyzeDocument (FORMS + TABLES) | ~$0.065 |
| Bedrock Haiku 4.5 | ~$0.001 |
| SFN state transitions (~25) | ~$0.001 |
| Lambda + S3 + DynamoDB + SNS | rounding error |
| **Total** | **~$0.07/run** |

---

## S5 -- Seed a sample claim

Sample artifacts are in `samples/`. Upload them in two phases: artifacts first,
manifest last. The EventBridge rule fires on the manifest write -- uploading it
last ensures all artifacts are present before the state machine starts.

```bash
INTAKE_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --query 'Stacks[0].Outputs[?OutputKey==`IntakeBucketName`].OutputValue' \
  --output text)

PREFIX=clients/acme-corp/CLM-001

# Step 1: upload artifacts
aws s3 cp samples/photo-damage.jpg   s3://${INTAKE_BUCKET}/${PREFIX}/photo-damage.jpg
aws s3 cp samples/police-report.pdf  s3://${INTAKE_BUCKET}/${PREFIX}/police-report.pdf
aws s3 cp samples/statement.txt      s3://${INTAKE_BUCKET}/${PREFIX}/statement.txt

# Step 2: upload manifest LAST to trigger the pipeline
aws s3 cp samples/manifest.json      s3://${INTAKE_BUCKET}/${PREFIX}/manifest.json
```

The pipeline starts within about 5 seconds of the manifest upload.

**Note on the sample photo**: `samples/photo-damage.jpg` is a minimal valid JPEG
placeholder. Rekognition will return only generic labels (it contains no meaningful
visual content). For a realistic demo, replace it with a CC0-licensed vehicle damage
photo (e.g., from Wikimedia Commons) and document the source in `samples/README.md`.

---

## S6 -- Watch the execution

Open the Step Functions console:
- Navigate to **Step Functions** -> **State machines**
- Click `insurance-claims-ai-pipeline-ClaimsStateMachine`
- Click the most recent execution to open the visual workflow view

What to observe:
- **ProcessArtifacts** Map state: watch all three branches run in parallel
- **DetectLabels** and **AnalyzeDocument** call AWS services directly (no Lambda icon
  -- these are direct SDK integrations)
- **SynthesizeVerdict**: expect 5-15 seconds for Haiku 4.5
- **ShouldNotifyAdjuster**: Choice state -- follows the NEEDS_REVIEW or APPROVE branch

From the CLI:

```bash
STATE_MACHINE_ARN=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --query 'Stacks[0].Outputs[?OutputKey==`StateMachineArn`].OutputValue' \
  --output text)

aws stepfunctions list-executions \
  --state-machine-arn "${STATE_MACHINE_ARN}" \
  --output table
```

---

## S7 -- Inspect outputs and trigger the guardrail

### Inspect happy path (CLM-001)

**Expected result**: CLM-001 will produce `final_status=NEEDS_REVIEW`, not `APPROVE`.
The sample `police-report.pdf` contains text identifying it as a synthetic/demo document.
The LLM correctly flags this as a suspicious marker, resulting in a confidence score
below the 0.85 threshold. This is expected behavior -- the guardrail fires because the
signal confidence is low, not because the claim is fraudulent.

The teaching value of CLM-001 is observing the full pipeline run, the evidence bundle,
and the draft adjuster email. The CLM-002 red-flag contrast (confidence ~0.25 vs ~0.65,
9 flags vs 5 flags) is where the guardrail behavior is most instructive.

```bash
DECISIONS_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --query 'Stacks[0].Outputs[?OutputKey==`DecisionsBucketName`].OutputValue' \
  --output text)

# Final decision
aws s3 cp s3://${DECISIONS_BUCKET}/clients/acme-corp/CLM-001/decision.json -

# Draft adjuster email
aws s3 cp s3://${DECISIONS_BUCKET}/clients/acme-corp/CLM-001/adjuster_email.md -

# Full audit bundle (includes original LLM output before guardrails)
aws s3 cp s3://${DECISIONS_BUCKET}/clients/acme-corp/CLM-001/evidence_bundle.json -

# DynamoDB record
TABLE=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --query 'Stacks[0].Outputs[?OutputKey==`TableName`].OutputValue' \
  --output text)

aws dynamodb get-item \
  --table-name "${TABLE}" \
  --key '{"client_id":{"S":"acme-corp"},"claim_id":{"S":"CLM-001"}}'
```

### Trigger the guardrail (CLM-002)

The red-flag sample set contains a claimant statement with multiple suspicious signals:
- No police report
- Vague incident location and time
- Damage pattern inconsistent with stated cause
- Coverage limit increased 14 days before the incident

```bash
INTAKE_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --query 'Stacks[0].Outputs[?OutputKey==`IntakeBucketName`].OutputValue' \
  --output text)

PREFIX=clients/acme-corp/CLM-002

aws s3 cp samples/red-flag/photo-damage.jpg   s3://${INTAKE_BUCKET}/${PREFIX}/photo-damage.jpg
aws s3 cp samples/red-flag/police-report.pdf  s3://${INTAKE_BUCKET}/${PREFIX}/police-report.pdf
aws s3 cp samples/red-flag/statement.txt      s3://${INTAKE_BUCKET}/${PREFIX}/statement.txt
aws s3 cp samples/red-flag/manifest.json      s3://${INTAKE_BUCKET}/${PREFIX}/manifest.json
```

After the execution completes, compare CLM-001 and CLM-002 in `evidence_bundle.json`:
- `llm_verdict.recommendation`: what the LLM originally recommended
- `final_status`: the post-guardrail effective status
- `override_reason`: explains which guardrail fired

**Teaching note -- why this guardrail exists:**

Real claim adjudication systems *do* auto-deny claims -- but at the deterministic
rules-engine layer: policy lapsed, claim outside coverage window, claimant on fraud
watchlist. These are binary, verifiable facts.

The guardrail in this pipeline targets something different: *probabilistic signals*
derived by an LLM. An LLM DENY is a weighted vote from a pattern-matching model,
not a definitive finding. The confidence score quantifies the model's own uncertainty.
Low confidence + DENY together indicate: "the evidence points toward denial but the
signal is weak." That is precisely when human judgment adds the most value.

This is a "low-trust signal -> human-in-the-loop" pattern, not an assertion that
LLMs cannot make negative determinations.

---

## S8 -- Modify the prompt

The Bedrock system prompt and output schema are embedded directly in `cfn/template.yaml`
in the `SynthesizeVerdictFunction` resource, under `Code.ZipFile`. Search for
`SYSTEM_PROMPT` to locate it.

Try:
1. Adding a new field to the output schema (e.g., `"suggested_investigation_steps"`)
2. Changing the system prompt framing ("you are a senior adjuster" vs "you are a
   fraud analyst")
3. Requesting a different tone for the draft adjuster email

After editing `cfn/template.yaml`, redeploy:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

aws cloudformation deploy \
  --template-file cfn/template.yaml \
  --stack-name insurance-claims-ai-pipeline \
  --s3-bucket cfn-templates-${ACCOUNT} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

Then re-seed to observe the changed output.

---

## S9 -- Swap models

The `BedrockModelId` parameter controls which model is used. To swap to Sonnet:

1. Find the current Sonnet cross-region inference profile ID in the Bedrock console
   (Bedrock -> Cross-region inference -> Inference profiles)
2. Sonnet is auto-enabled in your account (same as Haiku -- no manual access grant
   needed). Verify the model is available if you want to confirm first:
   ```bash
   aws bedrock list-foundation-models \
     --query 'modelSummaries[?contains(modelId,`claude-sonnet`)].modelId' \
     --output table --region us-east-1
   ```
3. Redeploy with the new model ID:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

aws cloudformation deploy \
  --template-file cfn/template.yaml \
  --stack-name insurance-claims-ai-pipeline \
  --s3-bucket cfn-templates-${ACCOUNT} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameter-overrides BedrockModelId=us.anthropic.claude-sonnet-4-6-XXXXXXXX-v1:0
```

**Cost warning**: Sonnet costs approximately 15-30x more than Haiku per API call.
With the same 3-artifact sample, expect ~$0.09-0.10/run vs ~$0.07 for Haiku --
a 30% total increase but 15-30x on the Bedrock line item alone.

---

## S10 -- Observability

### CloudWatch Logs

Each Lambda has a dedicated log group with 14-day retention:
```
/aws/lambda/insurance-claims-ai-pipeline-ReadManifest
/aws/lambda/insurance-claims-ai-pipeline-SynthesizeVerdict
... (one per Lambda)
```

State machine execution logs:
```
/aws/states/insurance-claims-ai-pipeline-ClaimsStateMachine
```

View SynthesizeVerdict logs for the last 10 minutes:
```bash
aws logs tail /aws/lambda/insurance-claims-ai-pipeline-SynthesizeVerdict \
  --since 10m --follow
```

### Step Functions execution history

The state machine is a Standard workflow -- full per-state event history is retained
for 90 days.

```bash
EXEC_ARN=$(aws stepfunctions list-executions \
  --state-machine-arn "${STATE_MACHINE_ARN}" \
  --query 'executions[0].executionArn' \
  --output text)

aws stepfunctions get-execution-history \
  --execution-arn "${EXEC_ARN}" \
  --output json | python3 -c "
import sys, json
for e in json.load(sys.stdin)['events']:
    print(e.get('timestamp',''), e.get('type',''))
"
```

### X-Ray (optional)

The state machine role includes X-Ray permissions. To enable tracing:
1. Open Step Functions console -> state machine -> Edit
2. Enable X-Ray tracing
3. Re-run a claim and view the service map in X-Ray console

---

## S11 -- Security walkthrough

### Why Rekognition and Textract need Resource: "*"

Open `cfn/template.yaml` and find `StateMachineRole`:

```yaml
# Rekognition -- no resource-level ARN for DetectLabels; action is the constraint
- Effect: Allow
  Action: rekognition:DetectLabels
  Resource: "*"

# Textract -- no resource-level permissions at all; Resource: * is required
- Effect: Allow
  Action: textract:AnalyzeDocument
  Resource: "*"
```

**Rekognition**: The `DetectLabels` API has no resource-level ARN. There is no
ARN concept for "a Rekognition model" -- the service is fully managed. The IAM
constraint is the *action*, not the resource. The wildcard is required by the service,
not a design choice.

**Textract**: Stronger case -- Textract has *no resource-level permissions at all*.
It is not possible to write a Textract IAM statement with a non-wildcard Resource.
The API documentation explicitly states this. Again, the action (`textract:AnalyzeDocument`)
is the full constraint.

**Bedrock**: Different story. Bedrock *does* support resource-level permissions --
on inference profile ARNs and foundation-model ARNs. The stack uses both:
```yaml
Resource:
  - arn:aws:bedrock:us-east-1:{acct}:inference-profile/us.anthropic.claude-haiku-4-5-...
  - arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-...
  - arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-haiku-4-5-...
  - arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-...
```

The cross-region profile can fan out to us-east-2 or us-west-2. Without IAM on the
foundation-model ARNs in those regions, the invocation throws `AccessDeniedException`
at runtime pointing to the *remote* region -- a notoriously confusing error.

### Other security posture

- **KMS SSE** on both S3 buckets, DynamoDB, SNS, and all CloudWatch log groups
- **TLS-only bucket policies** (`aws:SecureTransport` Deny on HTTP)
- **One IAM role per Lambda** -- WriteArtifacts cannot read the intake bucket;
  ReadManifest cannot write to decisions
- **DeletionPolicy: Delete** on the DynamoDB table -- table is deleted with the stack
  for clean teardown in this lab. In production, you would use `Retain` to protect
  audit records from accidental stack deletion.

---

## S12 -- Teardown

CloudFormation cannot delete S3 buckets that contain objects (or versioned objects).
Empty both buckets first, then delete the stack.

**Step 1**: Empty the intake bucket (versioned -- must delete all versions and
delete markers explicitly):

```bash
INTAKE_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --query 'Stacks[0].Outputs[?OutputKey==`IntakeBucketName`].OutputValue' \
  --output text)

# Delete all object versions and delete markers
aws s3api list-object-versions --bucket "${INTAKE_BUCKET}" \
  --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' \
  --output json | \
  python3 -c "
import sys, json, subprocess
data = json.load(sys.stdin)
objs = data.get('Objects') or []
if objs:
    subprocess.run(['aws','s3api','delete-objects',
        '--bucket','${INTAKE_BUCKET}','--delete',
        json.dumps({'Objects': objs, 'Quiet': True})], check=True)
print(f'Deleted {len(objs)} versions')
"

aws s3api list-object-versions --bucket "${INTAKE_BUCKET}" \
  --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' \
  --output json | \
  python3 -c "
import sys, json, subprocess
data = json.load(sys.stdin)
objs = data.get('Objects') or []
if objs:
    subprocess.run(['aws','s3api','delete-objects',
        '--bucket','${INTAKE_BUCKET}','--delete',
        json.dumps({'Objects': objs, 'Quiet': True})], check=True)
print(f'Deleted {len(objs)} delete markers')
"
```

**Step 2**: Empty the decisions bucket (also versioned -- `aws s3 rm --recursive` is NOT
sufficient because it only adds delete markers; the underlying versions remain and block
stack deletion):

```bash
DECISIONS_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --query 'Stacks[0].Outputs[?OutputKey==`DecisionsBucketName`].OutputValue' \
  --output text)

aws s3api list-object-versions --bucket "${DECISIONS_BUCKET}" \
  --query '[Versions[].{Key:Key,VersionId:VersionId},DeleteMarkers[].{Key:Key,VersionId:VersionId}]' \
  --output json | python3 -c "
import json, sys, subprocess, tempfile, os
data = json.load(sys.stdin)
objects = (data[0] or []) + (data[1] or [])
if not objects:
    print('Decisions bucket already empty')
else:
    batch = {'Objects': [{'Key': o['Key'], 'VersionId': o['VersionId']} for o in objects], 'Quiet': True}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(batch, f)
        fname = f.name
    import os as _os
    subprocess.run(['aws', 's3api', 'delete-objects',
        '--bucket', '${DECISIONS_BUCKET}', '--region', 'us-east-1',
        '--delete', f'file://{fname}'], check=True)
    _os.unlink(fname)
    print(f'Deleted {len(objects)} versions/markers from decisions bucket')
"
```

**Step 3**: Delete the stack:

```bash
aws cloudformation delete-stack \
  --stack-name insurance-claims-ai-pipeline \
  --region us-east-1

aws cloudformation wait stack-delete-complete \
  --stack-name insurance-claims-ai-pipeline \
  --region us-east-1

echo "Stack deleted."
```

Items NOT deleted by the stack:
- **KMS key** -- enters PENDING_DELETION (30-day waiting period by default)

Verify cleanup:
```bash
aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline 2>&1

aws logs describe-log-groups \
  --log-group-name-prefix /aws/lambda/insurance-claims-ai-pipeline \
  --query 'logGroups[].logGroupName' \
  --output table
```

---

## Appendix A -- Cost estimate

Per run (3 artifacts: 1 JPEG, 1 single-page PDF, 1 text), Haiku 4.5:

| Service | Detail | Cost |
|---------|--------|------|
| Rekognition DetectLabels | 1 image | ~$0.001 |
| Textract AnalyzeDocument | FORMS + TABLES, 1 page | ~$0.065 |
| Bedrock Haiku 4.5 | ~2K input + ~500 output tokens | ~$0.001 |
| SFN Standard | ~25 state transitions @ $0.000025 each | ~$0.001 |
| Lambda | 7 invocations, 256-512 MB, < 5s each | < $0.001 |
| DynamoDB on-demand | 1 PutItem | < $0.001 |
| S3 | 4 GetObject + 3 PutObject | < $0.001 |
| SNS | 0 or 1 Publish | < $0.001 |
| **Total** | | **~$0.07/run** |

Per run, Sonnet swap (S9):

| Change | Cost |
|--------|------|
| All non-Bedrock costs same | ~$0.068 |
| Bedrock Sonnet | ~$0.015-0.030 |
| **Total** | **~$0.09-0.10/run** |

The Sonnet line item is 15-30x higher than Haiku. Total run cost is only 30% higher
because Textract dominates. Important teaching point: optimizing the LLM call has
diminishing returns when Textract is the cost driver.

**Cost trap**: Standard SFN charges per state transition -- ~25 per run. 100 claims
in quick succession = 2,500 transitions (~$0.06) + 100 Textract pages ($6.50) = ~$6.56.
Always set the billing alarm before bulk testing.

---

## Appendix B -- Troubleshooting

### Bedrock AccessDeniedException

Symptoms: SynthesizeVerdict fails with `AccessDeniedException`, error message mentions
a region other than us-east-1 (e.g., `us-east-2` or `us-west-2`).

Cause: The cross-region inference profile routed the request to a region where either
model access is not granted or the IAM policy does not cover the foundation-model ARN.

Fix:
1. Models are auto-enabled; this error almost always indicates a missing IAM permission,
   not a model access issue. The error message will name the specific region that rejected
   the call (e.g., `us-east-2`).
2. Verify the `SynthesizeVerdictRole` policy in `cfn/template.yaml` covers
   foundation-model ARNs in us-east-1, us-east-2, AND us-west-2.
3. Check model availability for the specific region mentioned in the error:
   ```bash
   aws bedrock list-foundation-models \
     --query 'modelSummaries[?modelId==`anthropic.claude-haiku-4-5-20251001-v1:0`].{id:modelId,status:modelLifecycleStatus}' \
     --output table --region us-east-2
   ```

### Textract UnsupportedDocumentException

Cause: The uploaded PDF has more than one page. Textract `AnalyzeDocument` (sync)
is single-page only.

Fix: Use a single-page PDF. The included `samples/police-report.pdf` is single-page.
For multi-page support, see Appendix D extension exercise.

### EventBridge rule not firing

Check these in order:
1. **S3 EventBridge enabled**: intake bucket Properties -> Event notifications ->
   Amazon EventBridge -> On
2. **Rule pattern**: Open EventBridge -> Rules -> `*-IntakeManifestRule` -> Event
   pattern. The bucket name must match exactly.
3. **Object key suffix**: The rule fires on keys ending in `manifest.json`. Verify
   the uploaded key ends in `manifest.json` (case sensitive).
4. **SFN execution quota**: Standard SFN has a regional limit on open executions.
   Failed executions count against this limit until they are closed.

### Pipeline fails at ValidateArtifactsPresent

Cause: Manifest was uploaded before all artifact files finished uploading. The
EventBridge rule fired immediately on manifest upload; by the time the state machine
ran HeadObject on each artifact, some were not yet present.

This is the manifest-last convention's race condition in action (see S1 teaching note).

Fix: Ensure all artifact uploads (`aws s3 cp` for photo, PDF, and text) complete
before uploading `manifest.json`. Follow the two-step upload order in S5 -- artifacts
first, manifest last.

### DynamoDB record not appearing

Check: the table name includes the stack name prefix.
```bash
aws dynamodb scan \
  --table-name insurance-claims-ai-pipeline-ClaimsDecisions \
  --output table
```

Failed runs are written with `client_id=PIPELINE_ERROR` and `claim_id=<execution-name>`.

---

## Appendix C -- Cleanup verification checklist

After completing S12 teardown, verify:

- [ ] `aws cloudformation describe-stacks --stack-name insurance-claims-ai-pipeline`
      returns `Stack with id ... does not exist`
- [ ] `aws s3 ls | grep insurance-claims-ai-pipeline` returns nothing
- [ ] `aws logs describe-log-groups --log-group-name-prefix /aws/lambda/insurance-claims-ai-pipeline`
      returns empty
- [ ] `aws logs describe-log-groups --log-group-name-prefix /aws/states/insurance-claims-ai-pipeline`
      returns empty
- [ ] `aws kms list-aliases | grep insurance-claims-ai-pipeline` -- key should be in
      PENDING_DELETION (expected, will auto-delete after 30 days)

Manual cleanup if needed:
```bash
# Orphan log groups (if any)
aws logs delete-log-group \
  --log-group-name /aws/lambda/insurance-claims-ai-pipeline-SynthesizeVerdict
```

---

## Appendix D -- Extension exercises

### D1 -- Multi-page PDFs via async Textract

The sync `AnalyzeDocument` API used in this lab is single-page only. To support
multi-page claim documents:

1. Replace the `AnalyzeDocument` direct SDK integration with a Lambda that calls
   `textract.start_document_analysis()` (async)
2. Use a Step Functions `.waitForTaskToken` pattern: the Lambda starts the Textract
   job and returns the task token to Textract as a callback; Textract calls back when
   analysis is complete, resuming the SFN execution
3. Poll or use `GetDocumentAnalysis` to retrieve the result pages

Key SFN change: instead of `End: true` after analysis, the state needs a heartbeat
timeout (`HeartbeatSeconds`) and the Lambda passes `TaskToken` as a Textract tag.

### D2 -- Bedrock Guardrails service integration

This lab implements guardrails as application logic in a Lambda (confidence threshold
+ DENY downgrade). Amazon Bedrock also offers a native Guardrails service that can
block harmful content, apply topic filters, and redact PII *at the API layer*.

To add Bedrock Guardrails:
1. Create a guardrail in the Bedrock console with content filters appropriate for
   insurance claim processing
2. Pass `guardrailIdentifier` and `guardrailVersion` to the `invoke_model` call
   in the `SynthesizeVerdictFunction` ZipFile block in `cfn/template.yaml`
3. Handle `GUARDRAIL_INTERVENED` in the response and set `final_status = NEEDS_REVIEW`

The application-level guardrail in this lab remains valuable for business logic
(confidence threshold, DENY escalation) even when the Bedrock service guardrail
handles content filtering.

### D3 -- Human-in-the-loop adjuster approval

Currently the pipeline notifies the adjuster but does not wait for a response.
To implement true human-in-the-loop approval:

1. Add a `WaitForAdjusterApproval` state after `NotifyAdjuster` using
   `.waitForTaskToken`
2. The SNS message includes the task token and a callback URL
3. The adjuster clicks APPROVE or REJECT in a simple web UI that calls
   `StepFunctions.sendTaskSuccess()` or `StepFunctions.sendTaskFailure()`
4. Set `HeartbeatSeconds: 86400` (24-hour timeout) so unreviewed claims auto-fail

### D4 -- Rules engine + LLM hybrid

The current architecture relies entirely on the LLM for the initial recommendation.
A more robust production design uses a layered approach:

1. **Deterministic rules engine** (Lambda or Step Functions Parallel state) runs first:
   - Policy active? Coverage in scope? Claim filed within time limit?
   - Fraud watchlist check (external API call)
   - These produce binary APPROVE/DENY facts, not probabilistic scores
2. **LLM synthesis** runs only for claims that pass the rules engine
   - The LLM receives the structured rules output as additional evidence
   - Its role narrows to: "given these facts, assess evidentiary quality and
     draft the adjuster communication"
3. **Guardrails** apply to the LLM layer only, not to rules-engine decisions

This hybrid respects the difference between deterministic facts (rules engine) and
probabilistic evidence assessment (LLM), and avoids asking the LLM to make decisions
that should be made by code.
