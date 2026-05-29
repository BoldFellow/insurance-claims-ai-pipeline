# insurance-claims-ai-pipeline -- Lab Guide

## Overview

You will build an event-driven AI pipeline that processes insurance claims end-to-end.
When a claim manifest is uploaded to S3, EventBridge triggers a Step Functions Standard
workflow that:

1. Reads and validates the claim manifest
2. Confirms all declared artifacts exist in S3 (Map state with HeadObject -- no Lambda)
3. Fans out per-artifact in parallel: Rekognition for photos, Textract for documents,
   a Lambda reader for written statements (direct SDK integrations in the Map state)
4. Assembles the evidence into a single dict (Pass state -- no Lambda)
5. Calls Bedrock Claude to synthesize a dual-output recommendation: a claimant-safe
   letter body AND an internal adjuster summary
6. Runs Amazon Comprehend on the written statement (sentiment + key phrases) and
   enriches the evidence bundle before calling Bedrock
7. Synthesizes a dual-output recommendation via Bedrock Claude: a claimant-safe
   letter body AND an internal adjuster summary
8. Applies deterministic guardrails via a Choice state: confidence < 0.85
   -> NEEDS_REVIEW (no Lambda needed)
9. Persists four decision artifacts to S3 and a record to DynamoDB
10. Sends a direct SES email to the claimant for APPROVE/DENY; routes to the adjuster
    SNS topic for NEEDS_REVIEW

By the end of the lab you will understand:
- Step Functions Map state fan-out and direct AWS SDK integrations
- The difference between what belongs in ASL (routing, branching, retries) and what
  needs a Lambda (S3 parsing, Bedrock invocation, multi-file writes)
- Rekognition DetectLabels and Textract AnalyzeDocument in production IAM context
- Bedrock cross-region inference profiles and structured LLM output
- Amazon Comprehend sentiment and key-phrase analysis as evidence enrichment before Bedrock
- The "low-confidence -> human review" guardrail pattern and why it differs from a rules-engine DENY
- Dual output design: claimant-safe vs adjuster-internal content separation

Everything is built manually through the AWS Console first. A CloudFormation shortcut
that deploys the complete stack in one command is in Appendix A.

---

## S0 -- Prerequisites

### AWS Account
- Region: any region where Rekognition, Textract, and Bedrock are all available.
  **us-east-1** (N. Virginia) and **eu-central-1** (Frankfurt) both work for this lab.
  This guide uses `<REGION>` as a placeholder -- substitute your chosen region
  consistently throughout.
- Permissions: AdministratorAccess (or equivalent for CloudFormation, IAM, S3,
  DynamoDB, Step Functions, Bedrock, SNS, SES, Rekognition, Textract, EventBridge,
  CloudWatch).
- Sign in to the console and confirm you are in your chosen region before starting.
  Check the region selector in the top-right corner.

### Cost guardrails -- READ BEFORE CONTINUING

Each pipeline run costs approximately **$0.07** (dominated by Textract ~$0.065/page).
Before uploading any claim artifacts, set a billing alarm:

1. Open **AWS Budgets** in the console
2. Click **Create budget** -> **Use a template** -> **Zero spend budget**
3. Name it `lab-guard`, set the alert email to your address, click **Create budget**

**Do NOT loop claim uploads.** 100 runs = ~$7, mostly Textract.

---

## S1 -- Architecture walkthrough

```
Upload artifacts:                    Step Functions Standard Workflow
clients/{cid}/{claim_id}/            +---------------------------------------------+
  photo-damage.jpg                   | 1. ReadManifest (Lambda)                    |
  police-report.pdf    S3 Event      |    Reads manifest.json, validates schema     |
  statement.txt        Bridge        |                                              |
  manifest.json -----> Rule -------> | 2. ValidateArtifacts (Map state)            |
                                     |    HeadObject each artifact (direct SDK)     |
                                     |    -> fails execution if any are missing     |
                                     |                                              |
                                     | 3. ProcessArtifacts (Map state)             |
                                     |    Branches per artifact type:              |
                                     |      image  -> Rekognition DetectLabels     |
                                     |      doc    -> Textract AnalyzeDocument     |
                                     |      text   -> ReadText (Lambda)            |
                                     |    Each iteration emits {artifact, analysis}|
                                     |                                              |
                                     | 4. BuildEvidence (Pass state)               |
                                     |    Assembles Map output into evidence dict   |
                                     |                                              |
                                     | 5. SynthesizeVerdict (Lambda -> Bedrock)    |
                                     |    Dual output: client_letter + adjuster_    |
                                     |    summary; never mixes internal/external   |
                                     |                                              |
                                     | 6. GuardrailCheck (Choice state)            |
                                     |    DENY or confidence < 0.85               |
                                     |      -> ForceNeedsReview (Pass)             |
                                     |    else -> PassThroughVerdict (Pass)        |
                                     |                                              |
                                     | 7. WriteArtifacts (Lambda)                  |
                                     |    Writes 4 files to S3, 1 row to DynamoDB  |
                                     |                                              |
                                     | 8. RouteDecision (Choice state)             |
                                     |    APPROVE -> SES SendEmail (claimant)      |
                                     |    DENY    -> SES SendEmail (claimant)      |
                                     |    NEEDS_REVIEW -> AdjusterNotifications    |
                                     +---------------------------------------------+
                                              |
              s3://decisions/clients/{cid}/{claim_id}/
                decision.json           -- structured record
                client_letter.txt       -- claimant-safe (no red flags, no AI refs)
                adjuster_brief.md       -- internal (reasoning, red flags, raw LLM)
                evidence_bundle.json    -- full audit record
                                        DynamoDB: ClaimsDecisions
```

Key decisions to understand before building:

**Standard workflow** (not Express): 90-day execution history, per-state billing.
At ~20 state transitions per run, cost is ~$0.0005/run.

**Map state with MaxConcurrency=10**: all artifacts processed in parallel.

**Direct SDK integrations** (Rekognition, Textract, S3 HeadObject, SNS): no Lambda
wrapper needed. The ASL `arn:aws:states:::aws-sdk:*` resource pattern calls the
service directly, with the state machine role as the IAM principal. No `.sync` suffix
-- these integrations are synchronous request/response.

**ASL-native guardrail**: GuardrailCheck is a Choice state, not a Lambda. It routes
on `$.llm_verdict.recommendation == "DENY"` OR `$.llm_verdict.confidence < 0.85`.
No code required -- the logic lives in the state machine definition.

**Dual output**: The LLM generates two separate artifacts in one call: a
claimant-facing letter (no internal data) and an adjuster-facing summary (full
reasoning). They are written to separate S3 files and sent to separate SNS topics.

**Manifest-last convention**: EventBridge fires only on `manifest.json` writes.
S3 PUTs can arrive out of order; `ValidateArtifacts` (HeadObject per artifact)
is the actual guard against partial-evidence runs.

---

## S2 -- Bedrock model availability

Anthropic foundation models are automatically enabled in all commercial AWS regions.
No manual model access grant is needed.

### Cross-region inference profiles

Bedrock cross-region inference profiles load-balance requests across multiple regions
in a group. This lab uses:

- **US group** (recommended for us-east-1):
  Profile ID: `us.anthropic.claude-haiku-4-5-20251001-v1:0`
  Routes to: us-east-1, us-east-2, us-west-2

- **EU group** (recommended for eu-central-1):
  Profile ID: `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
  Routes to: eu-central-1, eu-west-1, eu-west-3, eu-north-1, eu-south-1

To confirm the profile ID is current:
1. Navigate to **Amazon Bedrock** -> **Cross-region inference** (left nav)
2. Find the profile that matches your region group
3. Copy the full Profile ID -- it may have changed since this guide was written

Save it for S7 (SynthesizeVerdictRole IAM policy) and S8 (Lambda env var):
```
Inference Profile ID: _______________________________________
```

### Why cross-region inference profiles need wildcard IAM

When you invoke an inference profile, Bedrock may route the request to any region in
the group based on capacity. The IAM policy must grant `bedrock:InvokeModel` on:
1. The inference-profile ARN in your deployment region (includes account ID)
2. The foundation-model ARN in ALL possible target regions (no account ID; region wildcard)

Using a region-specific list for (2) WILL break at runtime when AWS routes to a region
you did not enumerate -- the error points to the remote region, not your deployment
region. The correct pattern is a single wildcard-region ARN. This is explained further
in S7, Role 3.

To confirm the model is accessible, navigate to **Amazon Bedrock** -> **Foundation models**
and search for `claude-haiku`. You should see it listed.

---

## S3 -- Note your account ID

Before creating any resources, record your 12-digit AWS account ID -- you will need it
when typing resource ARNs in IAM policies and in the Step Functions state machine
definition.

1. Click your account name in the top-right corner of the AWS Console
2. Copy the **Account ID** (12 digits, no dashes needed but AWS shows it with dashes)
3. Keep it handy. Throughout this guide it is shown as `<ACCOUNT-ID>`

Also note your chosen region (from S0) -- shown as `<REGION>` throughout.

---

## S4 -- Create S3 buckets

### Intake bucket (claim uploads)

The bucket name includes your account ID to ensure global uniqueness and to match the
deterministic naming pattern used in IAM policies.

1. Open **S3** in the console
2. Click **Create bucket**
3. Bucket name: `insurance-claims-ai-pipeline-intake-<ACCOUNT-ID>`
   (replace `<ACCOUNT-ID>` with your 12 digits, no dashes)
4. Region: your chosen `<REGION>`
5. Object Ownership: **ACLs disabled** (default)
6. Block Public Access: leave all four checkboxes checked (default)
7. Versioning: leave **Disabled** (default)
8. Encryption: **Server-side encryption with Amazon S3 managed keys (SSE-S3)** (default)
9. Click **Create bucket**

After the bucket is created, enable EventBridge notifications:
1. Click the bucket name to open it
2. Go to the **Properties** tab
3. Scroll down to **Amazon EventBridge**
4. Click **Edit**, select **On**, click **Save changes**

### Decisions bucket (pipeline outputs)

1. Click **Create bucket**
2. Bucket name: `insurance-claims-ai-pipeline-decisions-<ACCOUNT-ID>`
3. Region: your chosen `<REGION>`
4. Block Public Access: all checkboxes checked (default)
5. Versioning: leave **Disabled** (default)
6. Encryption: **SSE-S3** (default)
7. Click **Create bucket**

The decisions bucket does NOT need EventBridge notifications.

---

## S5 -- Create DynamoDB table

1. Open **DynamoDB** in the console
2. Click **Create table**
3. Table name: `insurance-claims-ai-pipeline-ClaimsDecisions`
4. Partition key: `client_id` (String)
5. Sort key: `claim_id` (String)
6. Table settings: **Customize settings**
7. Table class: **DynamoDB Standard**
8. Capacity mode: **On-demand**
9. Encryption at rest: **Owned by Amazon DynamoDB** (default -- no additional cost)
10. Click **Create table**

After creation, add a Global Secondary Index for querying by status:
1. Click the table name
2. Go to the **Indexes** tab
3. Click **Create index**
4. Partition key: `final_status` (String)
5. Sort key: `processed_at` (String)
6. Index name: `StatusIndex`
7. Projected attributes: **All**
8. Click **Create index**

Also enable TTL so old records auto-expire after 90 days:
1. Go to the **Additional settings** tab
2. Under **Time to Live (TTL)**, click **Manage TTL**
3. TTL attribute: `ttl`
4. Click **Enable TTL**

---

## S6 -- Create SNS topic and verify SES identity

This pipeline sends two types of notifications:

- **Adjuster (NEEDS_REVIEW)**: SNS topic -- simple, subscription-based, no sender identity required
- **Claimant (APPROVE / DENY)**: SES direct email -- proper From/To addresses, claimant-safe letter body

### SNS: AdjusterNotifications topic

Adjuster notifications are sent here when `final_status=NEEDS_REVIEW`. The message body
is a trimmed notification: adjuster summary, reasoning, red flags, and an S3 link to the
full `adjuster_brief.md`.

1. Open **Amazon SNS** in the console
2. Click **Topics** -> **Create topic**
3. Type: **Standard**
4. Name: `insurance-claims-ai-pipeline-AdjusterNotifications`
5. Leave encryption disabled (no sensitive PII in the adjuster brief for this lab)
6. Click **Create topic**

Copy the topic ARN from the topic detail page:
```
Adjuster Topic ARN: _______________________________________
```

Optional: subscribe your email to receive adjuster notifications:
1. Click **Create subscription**
2. Protocol: **Email**
3. Endpoint: your email address
4. Click **Create subscription**
5. Confirm the subscription link that arrives by email

You must confirm the subscription before notifications are delivered.

### SES: Verify claimant sender identity

APPROVE and DENY outcomes send `client_letter.txt` directly via SES. The Step Functions
state uses `sesv2:sendEmail` with a verified From address and a fixed To address (the
`ClaimantEmail` CFN parameter).

**SES sandbox note:** New AWS accounts start in SES sandbox mode. In sandbox mode,
both the sender *and* recipient address must be verified before SES will deliver mail.
To request production access (removes the recipient restriction), see the SES console
under **Account dashboard** -> **Request production access**.

#### Step 1 -- Verify the sender address

1. Open **Amazon SES** in the console
2. Click **Verified identities** -> **Create identity**
3. Identity type: **Email address**
4. Email address: the address you want mail to come *from* (e.g., `claims@yourdomain.com`)
5. Click **Create identity**
6. Open your inbox, find the SES verification email, and click the verification link

```
Sender (From) address: _______________________________________
```

#### Step 2 -- Verify the recipient address (sandbox only)

If your account is still in sandbox mode, repeat the verification steps above for
the address you want claimant notifications delivered *to*. This is the address you
will supply as the `ClaimantEmail` CFN parameter.

Skip this step if your account has production SES access.

```
Claimant (To) address: _______________________________________
```

**Why SES instead of SNS for claimants?** SNS email requires subscription confirmation
from every recipient and lacks From/To addressing. SES delivers mail with a real sender
identity, supports domain verification and DKIM, and produces delivery and bounce
metrics. For a claimant-facing service, SES is the correct tool.

---

## S7 -- Create IAM roles

There are 6 IAM roles: one per Lambda function, one for the Step Functions state
machine, and one for EventBridge. Create them in the order shown here.

For each role, the process is the same:
- Open **IAM** -> **Roles** -> **Create role**
- Trusted entity: as specified per role
- Attach managed policies: as specified
- Add inline policy: as specified

Save the role ARNs as you go -- you will need them when creating the Lambda functions.

---

### Role 1: ReadManifestRole

Trusted entity: **AWS service** -> **Lambda**

Managed policy: `AWSLambdaBasicExecutionRole`

Inline policy name: `ReadManifestPolicy`
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::insurance-claims-ai-pipeline-intake-<ACCOUNT-ID>/clients/*"
    }
  ]
}
```

Role name: `insurance-claims-ai-pipeline-ReadManifestRole`

---

### Role 2: ReadTextRole

Trusted entity: **AWS service** -> **Lambda**

Managed policy: `AWSLambdaBasicExecutionRole`

Inline policy name: `ReadTextPolicy`
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::insurance-claims-ai-pipeline-intake-<ACCOUNT-ID>/clients/*"
    }
  ]
}
```

Role name: `insurance-claims-ai-pipeline-ReadTextRole`

---

### Role 3: SynthesizeVerdictRole

This Lambda calls Bedrock. The cross-region inference profile routes to multiple
regions dynamically -- see S2 for the full explanation.

Trusted entity: **AWS service** -> **Lambda**

Managed policy: `AWSLambdaBasicExecutionRole`

Inline policy name: `SynthesizeVerdictPolicy`

Replace `<REGION>`, `<ACCOUNT-ID>`, and `<INFERENCE-PROFILE-ID>` with values from
S2 and S3. The foundation-model ARN uses `*` for the region -- this is intentional
and required. Do not enumerate regions.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "arn:aws:bedrock:<REGION>:<ACCOUNT-ID>:inference-profile/<INFERENCE-PROFILE-ID>",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
      ]
    }
  ]
}
```

Note the ARN format differences:
- Inference profile: `arn:aws:bedrock:<REGION>:<ACCOUNT-ID>:inference-profile/...`
  Includes account ID. Deployed in your region only.
- Foundation model: `arn:aws:bedrock:*::foundation-model/...`
  No account ID (double `::`) and region is wildcard. The profile can route to any
  region in the group -- AWS controls that list and may add regions without notice.
  A wildcard is the only safe pattern here.

Role name: `insurance-claims-ai-pipeline-SynthesizeVerdictRole`

---

### Role 4: WriteArtifactsRole

This Lambda writes four files to S3 and one item to DynamoDB.

Trusted entity: **AWS service** -> **Lambda**

Managed policy: `AWSLambdaBasicExecutionRole`

Inline policy name: `WriteArtifactsPolicy`
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::insurance-claims-ai-pipeline-decisions-<ACCOUNT-ID>/clients/*"
    },
    {
      "Effect": "Allow",
      "Action": "dynamodb:PutItem",
      "Resource": "arn:aws:dynamodb:<REGION>:<ACCOUNT-ID>:table/insurance-claims-ai-pipeline-ClaimsDecisions"
    }
  ]
}
```

Role name: `insurance-claims-ai-pipeline-WriteArtifactsRole`

---

### Role 5: StateMachineRole

The Step Functions state machine calls Lambda, Rekognition, Textract, S3 (HeadObject
and GetObject for direct SDK integrations), SNS (adjuster topic), and SES (claimant
email for APPROVE and DENY).

Trusted entity: **AWS service** -> **Step Functions**

No managed policies needed.

Inline policy name: `StateMachinePolicy`

Replace `<REGION>`, `<ACCOUNT-ID>`, and `<SENDER-EMAIL>` throughout.
The adjuster topic ARN is the one you saved in S6.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": [
        "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-ReadManifest",
        "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-ReadText",
        "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-SynthesizeVerdict",
        "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-WriteArtifacts"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "s3:HeadObject",
      "Resource": "arn:aws:s3:::insurance-claims-ai-pipeline-intake-<ACCOUNT-ID>/clients/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::insurance-claims-ai-pipeline-intake-<ACCOUNT-ID>/clients/*"
    },
    {
      "Effect": "Allow",
      "Action": "rekognition:DetectLabels",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "textract:AnalyzeDocument",
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "sns:Publish",
      "Resource": "arn:aws:sns:<REGION>:<ACCOUNT-ID>:insurance-claims-ai-pipeline-AdjusterNotifications"
    },
    {
      "Effect": "Allow",
      "Action": "ses:SendEmail",
      "Resource": "arn:aws:ses:<REGION>:<ACCOUNT-ID>:identity/<SENDER-EMAIL>"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogDelivery",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:DescribeResourcePolicies",
        "logs:GetLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutLogEvents",
        "logs:PutResourcePolicy",
        "logs:UpdateLogDelivery"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets"
      ],
      "Resource": "*"
    }
  ]
}
```

Note why Rekognition and Textract need `Resource: "*"`:
- **Rekognition**: `DetectLabels` has no resource-level ARN. The action is the constraint.
- **Textract**: Has no resource-level permissions at all. `Resource: "*"` is required by the service.

Note the new `s3:HeadObject` statement: the `ValidateArtifacts` Map state calls
S3 HeadObject directly (no Lambda wrapper) using the `arn:aws:states:::aws-sdk:s3:headObject`
resource. The IAM action for HeadObject is `s3:HeadObject` -- verify your IAM documentation
because some older guides incorrectly list `s3:GetObject` here.

Role name: `insurance-claims-ai-pipeline-StateMachineRole`

---

### Role 6: EventBridgeRole

EventBridge needs permission to start the Step Functions execution when the manifest rule fires.

**Trusted entity:** AWS service -> **EventBridge**

> **Important:** In the IAM console, after selecting "AWS service", search for `EventBridge` and
> select the use case named **"EventBridge"** (not "EventBridge Scheduler" or "EventBridge Pipes").
> This sets the trust principal to `events.amazonaws.com`. Using the wrong use case is the most
> common cause of EventBridge "Invocation failed" errors.

After creating the role, go to the **Trust relationships** tab and verify the trust policy looks
exactly like this:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "events.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

No managed policies needed.

Inline policy name: `EventBridgePolicy`
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "states:StartExecution",
      "Resource": "arn:aws:states:<REGION>:<ACCOUNT-ID>:stateMachine:insurance-claims-ai-pipeline-ClaimsStateMachine"
    }
  ]
}
```

Role name: `insurance-claims-ai-pipeline-EventBridgeRole`

---

## S8 -- Create Lambda functions

Create all 4 Lambda functions. For each:
- Open **AWS Lambda** -> **Create function**
- Author from scratch
- Runtime: **Python 3.12**
- Architecture: **arm64**
- Create the function, then:
  - Paste the code into the inline editor (replace the default content in `lambda_function.py`)
  - Set environment variables under Configuration -> Environment variables
  - Set memory and timeout under Configuration -> General configuration
  - Assign the role under Configuration -> Permissions

---

### Lambda 1: ReadManifest

**Create the function:**
1. Function name: `insurance-claims-ai-pipeline-ReadManifest`
2. Runtime: Python 3.12, Architecture: arm64
3. Execution role: **Use an existing role** -> `insurance-claims-ai-pipeline-ReadManifestRole`
4. Click **Create function**

**Paste the code:**
1. In the Code tab, click `lambda_function.py` in the file tree
2. Select all and replace with:

```python
import json
import os
import boto3

INTAKE_BUCKET = os.environ["INTAKE_BUCKET"]
DECISIONS_BUCKET = os.environ["DECISIONS_BUCKET"]

s3 = boto3.client("s3")

VALID_TYPES = {"image", "document", "text"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
DOC_EXTS = {".pdf", ".png"}


class ManifestError(Exception):
    pass


def lambda_handler(event, context):
    bucket = event["detail"]["bucket"]["name"]
    key = event["detail"]["object"]["key"]

    response = s3.get_object(Bucket=bucket, Key=key)
    raw = response["Body"].read()
    if len(raw) > 100_000:
        raise ManifestError("manifest.json exceeds 100 KB size limit")

    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ManifestError(f"manifest.json is not valid JSON: {e}")

    _validate(manifest, bucket, key)

    for artifact in manifest["artifacts"]:
        artifact["intake_bucket"] = INTAKE_BUCKET

    return {
        "client_id": manifest["client_id"],
        "claim_id": manifest["claim_id"],
        "submitted_at": manifest.get("submitted_at", ""),
        "intake_bucket": INTAKE_BUCKET,
        "decisions_bucket": DECISIONS_BUCKET,
        "artifacts": manifest["artifacts"],
    }


def _validate(manifest, bucket, key):
    required = ["schema_version", "client_id", "claim_id", "artifacts"]
    for field in required:
        if field not in manifest:
            raise ManifestError(f"manifest.json missing required field: {field}")

    if not isinstance(manifest["artifacts"], list):
        raise ManifestError("artifacts must be a list")

    if len(manifest["artifacts"]) == 0:
        raise ManifestError("artifacts list is empty")

    if len(manifest["artifacts"]) > 20:
        raise ManifestError("artifacts list exceeds maximum of 20 items")

    client_id = manifest["client_id"]
    claim_id = manifest["claim_id"]
    expected_prefix = f"clients/{client_id}/{claim_id}/"

    for i, artifact in enumerate(manifest["artifacts"]):
        if "type" not in artifact:
            raise ManifestError(f"artifact[{i}] missing 'type'")
        if "key" not in artifact:
            raise ManifestError(f"artifact[{i}] missing 'key'")

        atype = artifact["type"]
        akey = artifact["key"]

        if atype not in VALID_TYPES:
            raise ManifestError(
                f"artifact[{i}] has invalid type '{atype}'. "
                f"Must be one of: {sorted(VALID_TYPES)}"
            )

        if not akey.startswith(expected_prefix):
            raise ManifestError(
                f"artifact[{i}] key '{akey}' must start with '{expected_prefix}'"
            )

        ext = "." + akey.rsplit(".", 1)[-1].lower() if "." in akey else ""
        if atype == "image" and ext not in IMAGE_EXTS:
            raise ManifestError(
                f"artifact[{i}] is type 'image' but extension is '{ext}'. "
                f"Allowed: {sorted(IMAGE_EXTS)}"
            )
        if atype == "document" and ext not in DOC_EXTS:
            raise ManifestError(
                f"artifact[{i}] is type 'document' but extension is '{ext}'. "
                f"Note: Textract sync requires single-page PDF or PNG. "
                f"Allowed: {sorted(DOC_EXTS)}"
            )
```

3. Click **Deploy**

**Configure:**
1. Go to **Configuration** -> **General configuration** -> **Edit**
2. Memory: `256 MB`, Timeout: `0 min 30 sec`
3. Click **Save**

**Set environment variables:**
1. Go to **Configuration** -> **Environment variables** -> **Edit**
2. Add:
   - Key: `INTAKE_BUCKET` | Value: `insurance-claims-ai-pipeline-intake-<ACCOUNT-ID>`
   - Key: `DECISIONS_BUCKET` | Value: `insurance-claims-ai-pipeline-decisions-<ACCOUNT-ID>`
3. Click **Save**

---

### Lambda 2: ReadText

This Lambda reads a plain text artifact from S3. It is invoked by the ProcessArtifacts
Map state only when `artifact.type == "text"`.

**Create the function:**
1. Function name: `insurance-claims-ai-pipeline-ReadText`
2. Runtime: Python 3.12, Architecture: arm64
3. Execution role: `insurance-claims-ai-pipeline-ReadTextRole`
4. Click **Create function**

**Paste the code:**

```python
import boto3

s3 = boto3.client("s3")
MAX_TEXT_BYTES = 100_000


def lambda_handler(event, context):
    artifact = event["artifact"]
    intake_bucket = artifact["intake_bucket"]
    key = artifact["key"]

    response = s3.get_object(Bucket=intake_bucket, Key=key)
    body = response["Body"].read(MAX_TEXT_BYTES + 1)

    if len(body) > MAX_TEXT_BYTES:
        body = body[:MAX_TEXT_BYTES]

    try:
        content = body.decode("utf-8")
    except UnicodeDecodeError:
        content = body.decode("utf-8", errors="replace")

    return {
        "type": "text",
        "key": key,
        "content": content,
    }
```

**Configure:**
1. Configuration -> **General configuration** -> **Edit**: Memory `256 MB`, Timeout `0 min 30 sec` -> **Save**

No environment variables needed.

---

### Lambda 3: SynthesizeVerdict

This Lambda calls Bedrock. It produces two output sections in a single API call:
`client_letter` (claimant-safe, no internal data) and `adjuster_summary` (internal,
full detail). Timeout is 120 seconds to allow for Haiku latency plus retry budget.
The boto3 client uses `max_attempts=1` -- retry logic lives in the ASL Retry block.

**Create the function:**
1. Function name: `insurance-claims-ai-pipeline-SynthesizeVerdict`
2. Runtime: Python 3.12, Architecture: arm64
3. Execution role: `insurance-claims-ai-pipeline-SynthesizeVerdictRole`
4. Click **Create function**

**Paste the code:**

```python
import json
import os
import re

import boto3
from botocore.config import Config

MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
REGION = os.environ.get("AWS_REGION", "us-east-1")

_config = Config(
    region_name=REGION,
    read_timeout=110,
    connect_timeout=10,
    retries={"max_attempts": 1},
)
bedrock = boto3.client("bedrock-runtime", config=_config)

SYSTEM_PROMPT = """You are an insurance claims adjudication assistant helping human adjusters review claim evidence.

IMPORTANT: You are drafting a recommendation for review by a licensed adjuster. You are NOT making a final decision. Your output will be validated and may be overridden before any action is taken.

Analyze the evidence provided and output a JSON object with this exact schema:
{
  "claimant_name": "<full name of the claimant extracted from the evidence; empty string if not found>",
  "recommendation": "APPROVE" | "DENY" | "NEEDS_REVIEW",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<2-4 sentences, factual: what evidence supports this>",
  "approval_rationale": "<one sentence explaining why approved; empty string if not APPROVE>",
  "denial_reasons": ["<plain-language reason>"],
  "red_flags": ["<concerns or anomalies; may be populated for any recommendation; empty list if none>"],
  "client_letter": "<professional letter body addressed to the claimant>",
  "adjuster_summary": "<one-paragraph internal summary for the adjuster>"
}

STRICT RULES for client_letter:
- Written from the perspective of the Claims Department to the claimant
- DO NOT mention: confidence scores, red flags, internal reasoning, AI, automated systems, or model names
- DO NOT include denial reasons or red flags even if DENY -- those go in denial_reasons, not here
- Use professional, empathetic, plain language
- 3-5 sentences maximum
- Do NOT include salutation (Dear...) or closing (Sincerely...) -- those are added by the system
- If recommendation is DENY: write 1-2 sentences acknowledging the claim cannot be approved; do NOT include the reasons (those go in denial_reasons); do NOT set to empty string
- If recommendation is NEEDS_REVIEW: set client_letter to empty string -- a human adjuster will draft the communication

adjuster_summary MAY reference: confidence, reasoning, red flags, denial reasons, and any internal concerns.

Guidelines:
- APPROVE: evidence is consistent, no red flags, straightforward claim
- DENY: clear evidence of fraud, excluded peril, or policy violation (rare - when in doubt use NEEDS_REVIEW)
- NEEDS_REVIEW: ambiguous evidence, conflicting signals, or anything requiring specialist judgment
- confidence: how certain you are (0.0 = no idea, 1.0 = completely certain)

Output ONLY the JSON object. No preamble, no explanation outside the JSON."""


def lambda_handler(event, context):
    client_id = event["client_id"]
    claim_id = event["claim_id"]
    evidence = event["evidence"]

    user_message = _build_evidence_message(client_id, claim_id, evidence)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2048,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_message}
        ],
    }

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )

    raw_body = json.loads(response["body"].read())
    llm_text = raw_body["content"][0]["text"].strip()

    try:
        verdict = json.loads(llm_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", llm_text, re.DOTALL)
        if match:
            verdict = json.loads(match.group())
        else:
            raise ValueError(f"Bedrock response is not valid JSON: {llm_text[:500]}")

    _validate_verdict(verdict)

    return {
        "client_id": client_id,
        "claim_id": claim_id,
        "submitted_at": event.get("submitted_at", ""),
        "decisions_bucket": event["decisions_bucket"],
        "evidence": evidence,
        "llm_verdict": verdict,
    }


def _build_evidence_message(client_id, claim_id, evidence):
    parts = [f"Claim ID: {claim_id}\nClient: {client_id}\n\n## Evidence\n"]

    for item in evidence.get("artifacts", []):
        artifact = item.get("artifact", {})
        analysis = item.get("analysis", {})
        atype = analysis.get("type", "")
        akey = artifact.get("key", "")

        if atype == "image":
            labels = ", ".join(
                f"{l.get('Name', '')} ({l.get('Confidence', 0):.0f}%)"
                for l in analysis.get("Labels", [])
            )
            parts.append(f"### Image Analysis (Rekognition): {akey}\nDetected: {labels}\n\n")

        elif atype == "document":
            blocks = analysis.get("Blocks", [])
            lines = []
            form_fields = []
            value_map = {}
            block_index = {b["Id"]: b for b in blocks}

            for block in blocks:
                btype = block.get("BlockType", "")
                if btype == "LINE":
                    lines.append(block.get("Text", ""))
                elif btype == "KEY_VALUE_SET":
                    entity = block.get("EntityTypes", [])

                    def get_child_text(blk):
                        words = []
                        for rel in blk.get("Relationships", []):
                            if rel["Type"] == "CHILD":
                                for cid in rel["Ids"]:
                                    child = block_index.get(cid, {})
                                    if child.get("BlockType") == "WORD":
                                        words.append(child.get("Text", ""))
                        return " ".join(words)

                    if "KEY" in entity:
                        key_text = get_child_text(block)
                        for rel in block.get("Relationships", []):
                            if rel["Type"] == "VALUE":
                                for vid in rel["Ids"]:
                                    value_map[vid] = key_text
                    elif "VALUE" in entity:
                        val_text = get_child_text(block)
                        key_text = value_map.get(block["Id"], "")
                        if key_text:
                            form_fields.append({"key": key_text, "value": val_text})

            parts.append(f"### Document Analysis (Textract): {akey}\n")
            if form_fields:
                fields = "; ".join(f"{f['key']}: {f['value']}" for f in form_fields[:20])
                parts.append(f"Form fields: {fields}\n")
            if lines:
                extracted = "\n".join(lines)
                parts.append(f"Extracted text:\n{extracted[:2000]}\n\n")

        elif atype == "text":
            content = analysis.get("content", "")
            parts.append(f"### Written Statement: {akey}\n{content[:2000]}\n\n")

    parts.append("\nBased on this evidence, provide your adjudication recommendation as a JSON object.")
    return "".join(parts)


def _validate_verdict(verdict):
    required = [
        "claimant_name", "recommendation", "confidence", "reasoning",
        "approval_rationale", "denial_reasons", "red_flags",
        "client_letter", "adjuster_summary",
    ]
    for field in required:
        if field not in verdict:
            raise ValueError(f"Bedrock verdict missing required field: {field}")

    if verdict["recommendation"] not in ("APPROVE", "DENY", "NEEDS_REVIEW"):
        raise ValueError(f"Invalid recommendation: {verdict['recommendation']}")

    conf = verdict["confidence"]
    if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
        raise ValueError(f"confidence must be a float 0.0-1.0, got: {conf}")

    if not isinstance(verdict["denial_reasons"], list):
        raise ValueError("denial_reasons must be a list")

    if not isinstance(verdict["red_flags"], list):
        raise ValueError("red_flags must be a list")
```

**Configure:**
1. Configuration -> **General configuration** -> **Edit**: Memory `512 MB`, Timeout `2 min 0 sec` -> **Save**

**Environment variables:**
- Key: `BEDROCK_MODEL_ID` | Value: your inference profile ID from S2
  (e.g., `us.anthropic.claude-haiku-4-5-20251001-v1:0` for us-east-1,
  or `eu.anthropic.claude-haiku-4-5-20251001-v1:0` for eu-central-1)

---

### Lambda 4: WriteArtifacts

This Lambda writes four files to the decisions bucket and one item to DynamoDB.
It also returns `client_letter` and `adjuster_brief` in its response so the downstream
SNS Publish states can use them without re-reading S3. `adjuster_brief` here is a
trimmed notification (summary + reasoning + red flags + S3 link) -- the full markdown
brief with raw Bedrock output is written to S3 only, keeping the SNS payload small.

**Create the function:**
1. Function name: `insurance-claims-ai-pipeline-WriteArtifacts`
2. Runtime: Python 3.12, Architecture: arm64
3. Execution role: `insurance-claims-ai-pipeline-WriteArtifactsRole`
4. Click **Create function**

**Paste the code:**

```python
import json
import os
from datetime import datetime, timezone, timedelta

import boto3

s3 = boto3.client("s3")
dynamodb = boto3.client("dynamodb")

TABLE_NAME = os.environ["DECISIONS_TABLE_NAME"]

_CLAIMANT_CLOSE = (
    "\n\nPlease retain your claim reference number for your records."
    " If you have questions, contact us at claims@example.com or 1-800-CLAIMS-1."
    "\n\nSincerely,\nClaims Department"
)

_NEEDS_REVIEW_LETTER = (
    "Thank you for submitting your insurance claim. We have received your claim"
    " and it is currently under review by one of our licensed adjusters.\n\n"
    "An adjuster will contact you within 5 business days with an update on the"
    " status of your claim. Please have your claim reference number available"
    " when we contact you.\n\n"
    "If you have any questions in the meantime, please contact us at"
    " claims@example.com or 1-800-CLAIMS-1."
)


def lambda_handler(event, context):
    client_id = event["client_id"]
    claim_id = event["claim_id"]
    decisions_bucket = event["decisions_bucket"]
    prefix = f"clients/{client_id}/{claim_id}"

    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    ttl = int((now + timedelta(days=90)).timestamp())

    llm_verdict = event["llm_verdict"]
    final_status = event["final_status"]
    override_reason = event.get("override_reason")

    decision = {
        "client_id": client_id,
        "claim_id": claim_id,
        "submitted_at": event.get("submitted_at", ""),
        "processed_at": created_at,
        "final_status": final_status,
        "original_recommendation": llm_verdict["recommendation"],
        "confidence": llm_verdict["confidence"],
        "override_reason": override_reason,
        "reasoning": llm_verdict.get("reasoning", ""),
        "approval_rationale": llm_verdict.get("approval_rationale", ""),
        "denial_reasons": llm_verdict.get("denial_reasons", []),
        "red_flags": llm_verdict.get("red_flags", []),
        "decision_s3_prefix": f"s3://{decisions_bucket}/{prefix}/",
    }

    s3.put_object(
        Bucket=decisions_bucket,
        Key=f"{prefix}/decision.json",
        Body=json.dumps(decision, indent=2).encode(),
        ContentType="application/json",
    )

    client_letter = _format_client_letter(claim_id, final_status, llm_verdict)
    s3.put_object(
        Bucket=decisions_bucket,
        Key=f"{prefix}/client_letter.txt",
        Body=client_letter.encode("ascii", errors="replace"),
        ContentType="text/plain",
    )

    adjuster_brief = _format_adjuster_brief(client_id, claim_id, created_at, decision, llm_verdict)
    s3.put_object(
        Bucket=decisions_bucket,
        Key=f"{prefix}/adjuster_brief.md",
        Body=adjuster_brief.encode(),
        ContentType="text/markdown",
    )

    adjuster_notification = _format_adjuster_notification(claim_id, client_id, decision, llm_verdict)

    bundle = {
        "decision": decision,
        "llm_verdict": llm_verdict,
        "evidence": event.get("evidence", {}),
    }
    s3.put_object(
        Bucket=decisions_bucket,
        Key=f"{prefix}/evidence_bundle.json",
        Body=json.dumps(bundle, indent=2).encode(),
        ContentType="application/json",
    )

    dynamodb.put_item(
        TableName=TABLE_NAME,
        Item={
            "client_id":               {"S": client_id},
            "claim_id":                {"S": claim_id},
            "submitted_at":            {"S": event.get("submitted_at", "")},
            "processed_at":            {"S": created_at},
            "final_status":            {"S": final_status},
            "original_recommendation": {"S": llm_verdict["recommendation"]},
            "confidence":              {"N": str(llm_verdict["confidence"])},
            "override_reason":         {"S": override_reason or ""},
            "reasoning":               {"S": llm_verdict.get("reasoning", "")[:1000]},
            "approval_rationale":      {"S": llm_verdict.get("approval_rationale", "")},
            "denial_reasons":          {"S": json.dumps(llm_verdict.get("denial_reasons", []))},
            "red_flags":               {"S": json.dumps(llm_verdict.get("red_flags", []))},
            "decision_s3_prefix":      {"S": f"s3://{decisions_bucket}/{prefix}/"},
            "ttl":                     {"N": str(ttl)},
        },
    )

    return {
        "client_id": client_id,
        "claim_id": claim_id,
        "final_status": final_status,
        "decision_s3_prefix": f"s3://{decisions_bucket}/{prefix}/",
        "client_letter": client_letter,
        "adjuster_brief": adjuster_notification,
    }


def _format_client_letter(claim_id, final_status, llm_verdict):
    name = llm_verdict.get("claimant_name", "").strip()
    salutation = f"Dear {name}," if name else "Dear Claimant,"

    if final_status == "APPROVE":
        body = llm_verdict.get("client_letter", "")
        return f"{salutation}\n\n{body}{_CLAIMANT_CLOSE}\n"

    elif final_status == "DENY":
        body = llm_verdict.get("client_letter", "").strip()
        if not body:
            body = "We have completed our review of your insurance claim and regret that we are unable to approve your request."
        denial_reasons = llm_verdict.get("denial_reasons", [])
        reasons_block = ""
        if denial_reasons:
            bullets = "\n".join(f"  - {r}" for r in denial_reasons)
            reasons_block = f"\n\nReason(s) for this decision:\n{bullets}"
        appeal = (
            "\n\nIf you disagree with this decision, you have the right to appeal within"
            " 30 days by contacting our Claims Appeals Department at"
            " claims-appeals@example.com or by calling 1-800-CLAIMS-2."
        )
        return f"{salutation}\n\n{body}{reasons_block}{appeal}{_CLAIMANT_CLOSE}\n"

    else:
        return (
            f"{salutation}\n\n{_NEEDS_REVIEW_LETTER}"
            f"\n\nYour claim reference: {claim_id}{_CLAIMANT_CLOSE}\n"
        )


def _format_adjuster_notification(claim_id, client_id, decision, llm_verdict):
    red_flags = llm_verdict.get("red_flags", [])
    red_flags_block = "None" if not red_flags else "\n".join(f"- {f}" for f in red_flags)
    return (
        f"Claim {claim_id} ({client_id}) requires adjuster review.\n\n"
        f"Summary:\n{llm_verdict.get('adjuster_summary', '')}\n\n"
        f"Reasoning:\n{llm_verdict.get('reasoning', '')}\n\n"
        f"Red Flags:\n{red_flags_block}\n\n"
        f"Full brief: {decision['decision_s3_prefix']}adjuster_brief.md"
    )


def _format_adjuster_brief(client_id, claim_id, created_at, decision, llm_verdict):
    final_status = decision["final_status"]
    original_rec = decision["original_recommendation"]
    confidence = decision["confidence"]
    override_reason = decision.get("override_reason") or ""

    override_section = ""
    if override_reason:
        override_section = f"\n## Override\n{override_reason}\n"

    red_flags = llm_verdict.get("red_flags", [])
    red_flags_block = "None" if not red_flags else "\n".join(f"- {f}" for f in red_flags)

    approval_rationale = llm_verdict.get("approval_rationale", "")
    approval_section = ""
    if approval_rationale:
        approval_section = f"\n## Approval Rationale\n{approval_rationale}\n"

    denial_reasons = llm_verdict.get("denial_reasons", [])
    denial_section = ""
    if denial_reasons:
        bullets = "\n".join(f"- {r}" for r in denial_reasons)
        denial_section = f"\n## Denial Reasons\n{bullets}\n"

    raw_json = json.dumps(llm_verdict, indent=2)

    return (
        f"# Adjuster Brief - Claim {claim_id}\n"
        f"**Client:** {client_id}\n"
        f"**Processed:** {created_at}\n"
        f"**Final Status:** {final_status}\n"
        f"\n## Summary\n{llm_verdict.get('adjuster_summary', '')}\n"
        f"\n## Recommendation and Confidence\n"
        f"- Original recommendation: {original_rec}\n"
        f"- Confidence: {confidence:.0%}\n"
        f"- Final status: {final_status}\n"
        f"{override_section}"
        f"\n## Reasoning\n{llm_verdict.get('reasoning', '')}\n"
        f"\n## Red Flags\n{red_flags_block}\n"
        f"{approval_section}"
        f"{denial_section}"
        f"\n## Original Bedrock Output\n```json\n{raw_json}\n```\n"
    )
```

**Configure:**
1. Configuration -> **General configuration** -> **Edit**: Memory `256 MB`, Timeout `0 min 30 sec` -> **Save**

**Environment variables:**
- Key: `DECISIONS_TABLE_NAME` | Value: `insurance-claims-ai-pipeline-ClaimsDecisions`

---

## S9 -- Create Step Functions state machine

The state machine has 17 states. This section walks through each one so you understand
what it does and how it is configured. After the walkthrough, the complete ASL JSON is
provided as a paste shortcut.

### Open Workflow Studio

1. Open **AWS Step Functions** in the console
2. Click **Create state machine**
3. Select **Blank** template -> **Standard** workflow type
4. Click **Next** to open the Workflow Studio designer

You will add states from the left-hand **States browser** panel. After adding each
state, click it on the canvas to open its properties in the right panel. Most fields
can be set in the **Configuration** and **Input/Output** tabs; complex parameter
structures require the **Definition** (Code) tab.

Throughout this section, replace:
- `<REGION>` with your region (e.g., `us-east-1`)
- `<ACCOUNT-ID>` with your 12-digit account ID

---

### State 1: ReadManifest

**What it does:** Invokes the ReadManifest Lambda, which downloads manifest.json from
S3, validates it, and returns a structured dict containing `client_id`, `claim_id`,
`artifacts` (list), `intake_bucket`, and `decisions_bucket`. This dict becomes the
execution state for all downstream states.

**State type:** Task -> Lambda -> Invoke

**In Workflow Studio:**
1. Drag **Lambda: Invoke** onto the canvas as the first state
2. State name: `ReadManifest`
3. Function name: `arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-ReadManifest`
4. Open the **Input/Output** tab:
   - Result selector: `{"body.$": "$.Payload"}`
   - Result path: `$` (replaces entire input with the Lambda response wrapper)
   - Output path: `$.body` (extracts just the Lambda return value)
5. Open the **Error handling** tab:
   - Add retry: errors `Lambda.ServiceException, Lambda.AWSLambdaException, Lambda.SdkClientException`, interval 2s, max 3, backoff 2.0
   - Add catch: error `States.ALL`, result path `$.error`, next state `RecordFailure`
6. Next state: `ValidateArtifacts`

**State definition (reference):**
```json
"ReadManifest": {
  "Type": "Task",
  "Resource": "arn:aws:states:::lambda:invoke",
  "Parameters": {
    "FunctionName": "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-ReadManifest",
    "Payload.$": "$"
  },
  "ResultSelector": { "body.$": "$.Payload" },
  "ResultPath": "$",
  "OutputPath": "$.body",
  "Retry": [
    {
      "ErrorEquals": ["Lambda.ServiceException","Lambda.AWSLambdaException","Lambda.SdkClientException"],
      "IntervalSeconds": 2, "MaxAttempts": 3, "BackoffRate": 2.0
    }
  ],
  "Catch": [{ "ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "RecordFailure" }],
  "Next": "ValidateArtifacts"
}
```

---

### State 2: ValidateArtifacts

**What it does:** Runs a parallel Map over `$.artifacts`. For each artifact, it calls
S3 `HeadObject` to confirm the file exists in the intake bucket. If any artifact is
missing (404), the Map fails and execution routes to `RecordFailure`. No output is
written -- `ResultPath: null` discards the HeadObject responses and passes the full
input state unchanged to `ProcessArtifacts`.

**State type:** Map (inline mode)
Iterator state: Task -> S3 -> HeadObject (aws-sdk integration)

**In Workflow Studio:**
1. Drag **Map** onto the canvas after `ReadManifest`
2. State name: `ValidateArtifacts`
3. **Configuration** tab:
   - Items path: `$.artifacts`
   - Item selector (transforms each item before the iterator sees it):
     `{"artifact.$": "$$.Map.Item.Value"}`
   - Max concurrency: `10`
4. **Input/Output** tab:
   - Result path: `null` (discard Map output, keep input state intact)
5. **Error handling** tab:
   - Add catch: `States.ALL`, result path `$.error`, next `RecordFailure`
6. Inside the Map iterator, add a **Task** state:
   - State name: `HeadObject`
   - Resource: `arn:aws:states:::aws-sdk:s3:headObject`
   - Parameters:
     ```json
     {
       "Bucket.$": "$.artifact.intake_bucket",
       "Key.$": "$.artifact.key"
     }
     ```
   - Result path: `null` (discard the HeadObject response)
   - Mark as **End** state of the iterator
7. Next state (after Map): `ProcessArtifacts`

**State definition (reference):**
```json
"ValidateArtifacts": {
  "Type": "Map",
  "ItemsPath": "$.artifacts",
  "ItemSelector": { "artifact.$": "$$.Map.Item.Value" },
  "MaxConcurrency": 10,
  "ResultPath": null,
  "ItemProcessor": {
    "ProcessorConfig": { "Mode": "INLINE" },
    "StartAt": "HeadObject",
    "States": {
      "HeadObject": {
        "Type": "Task",
        "Resource": "arn:aws:states:::aws-sdk:s3:headObject",
        "Parameters": {
          "Bucket.$": "$.artifact.intake_bucket",
          "Key.$": "$.artifact.key"
        },
        "ResultPath": null,
        "End": true
      }
    }
  },
  "Catch": [{ "ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "RecordFailure" }],
  "Next": "ProcessArtifacts"
}
```

---

### State 3: ProcessArtifacts

**What it does:** Runs a second parallel Map over `$.artifacts`. Each iteration
classifies the artifact type (`image`, `document`, or `text`) via a Choice state, then
routes to the appropriate AWS service call:

| Type | State | Service |
|------|-------|---------|
| `image` | `DetectLabels` | Rekognition `DetectLabels` (aws-sdk) |
| `document` | `AnalyzeDocument` | Textract `AnalyzeDocument` (aws-sdk) |
| `text` | `ReadText` | Lambda `ReadText` |

Each iteration writes its service output to `$.analysis` (`ResultPath: "$.analysis"`),
so each Map item exits as `{artifact: {...}, analysis: {...}}`. The combined array of
these objects is written to `$.artifact_results` by the Map (`ResultPath:
"$.artifact_results"`).

**State type:** Map (inline mode) with nested Choice + 3 Task states

This is the most complex state in the machine. Use the **Definition** (Code) tab to
configure the full iterator rather than building each nested state in the UI.

**In Workflow Studio:**
1. Drag **Map** after `ValidateArtifacts`
2. State name: `ProcessArtifacts`
3. **Configuration** tab:
   - Items path: `$.artifacts`
   - Item selector: `{"artifact.$": "$$.Map.Item.Value"}`
   - Max concurrency: `10`
4. **Input/Output** tab:
   - Result path: `$.artifact_results`
5. **Error handling** tab:
   - Add catch: `States.ALL`, result path `$.error`, next `RecordFailure`
6. Switch to the **Definition** tab and replace the iterator body with:

```json
{
  "ProcessorConfig": { "Mode": "INLINE" },
  "StartAt": "ClassifyArtifact",
  "States": {
    "ClassifyArtifact": {
      "Type": "Choice",
      "Choices": [
        { "Variable": "$.artifact.type", "StringEquals": "image",    "Next": "DetectLabels" },
        { "Variable": "$.artifact.type", "StringEquals": "document", "Next": "AnalyzeDocument" },
        { "Variable": "$.artifact.type", "StringEquals": "text",     "Next": "ReadText" }
      ]
    },
    "DetectLabels": {
      "Type": "Task",
      "Resource": "arn:aws:states:::aws-sdk:rekognition:detectLabels",
      "Parameters": {
        "Image": {
          "S3Object": {
            "Bucket.$": "$.artifact.intake_bucket",
            "Name.$": "$.artifact.key"
          }
        },
        "MaxLabels": 30,
        "MinConfidence": 50
      },
      "ResultSelector": { "type": "image", "Labels.$": "$.Labels" },
      "ResultPath": "$.analysis",
      "Retry": [
        {
          "ErrorEquals": ["Rekognition.ProvisionedThroughputExceededException"],
          "IntervalSeconds": 2, "MaxAttempts": 3, "BackoffRate": 2.0
        }
      ],
      "End": true
    },
    "AnalyzeDocument": {
      "Type": "Task",
      "Resource": "arn:aws:states:::aws-sdk:textract:analyzeDocument",
      "Parameters": {
        "Document": {
          "S3Object": {
            "Bucket.$": "$.artifact.intake_bucket",
            "Name.$": "$.artifact.key"
          }
        },
        "FeatureTypes": ["FORMS", "TABLES"]
      },
      "ResultSelector": { "type": "document", "Blocks.$": "$.Blocks" },
      "ResultPath": "$.analysis",
      "Retry": [
        {
          "ErrorEquals": ["Textract.ProvisionedThroughputExceededException"],
          "IntervalSeconds": 5, "MaxAttempts": 3, "BackoffRate": 2.0
        }
      ],
      "End": true
    },
    "ReadText": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-ReadText",
        "Payload.$": "$"
      },
      "ResultSelector": {
        "type.$": "$.Payload.type",
        "key.$": "$.Payload.key",
        "content.$": "$.Payload.content",
        "sentiment.$": "$.Payload.sentiment",
        "sentiment_scores.$": "$.Payload.sentiment_scores",
        "key_phrases.$": "$.Payload.key_phrases"
      },
      "ResultPath": "$.analysis",
      "Retry": [
        {
          "ErrorEquals": ["Lambda.ServiceException","Lambda.AWSLambdaException"],
          "IntervalSeconds": 2, "MaxAttempts": 3, "BackoffRate": 2.0
        }
      ],
      "End": true
    }
  }
}
```

7. Next state (after Map): `BuildEvidence`

---

### State 4: BuildEvidence

**What it does:** A Pass state that reshapes the execution input into the exact dict
shape that `SynthesizeVerdict` expects. It uses Step Functions intrinsic functions
(`States.JsonMerge`, reference paths) to pass through unchanged fields (`client_id`,
`claim_id`, `submitted_at`, `decisions_bucket`) and nest the `$.artifact_results`
array under `evidence.artifacts`.

A Pass state does no I/O with external services -- it just transforms the data in
memory within the state machine engine.

**State type:** Pass

**In Workflow Studio:**
1. Drag **Pass** after `ProcessArtifacts`
2. State name: `BuildEvidence`
3. In the **Definition** tab, set the Parameters block:

```json
"BuildEvidence": {
  "Type": "Pass",
  "Parameters": {
    "client_id.$":        "$.client_id",
    "claim_id.$":         "$.claim_id",
    "submitted_at.$":     "$.submitted_at",
    "decisions_bucket.$": "$.decisions_bucket",
    "evidence": {
      "artifacts.$": "$.artifact_results"
    }
  },
  "Next": "SynthesizeVerdict"
}
```

---

### State 5: SynthesizeVerdict

**What it does:** Invokes the SynthesizeVerdict Lambda, which sends the evidence bundle
to Bedrock Claude Haiku and parses the structured JSON response. Returns `llm_verdict`
containing the recommendation, confidence score, reasoning, client letter, and adjuster
summary.

**State type:** Task -> Lambda -> Invoke

**In Workflow Studio:**
1. Drag **Lambda: Invoke** after `BuildEvidence`
2. State name: `SynthesizeVerdict`
3. Function name: `arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-SynthesizeVerdict`
4. **Input/Output** tab:
   - Result selector: `{"llm_verdict.$": "$.Payload"}`
   - Result path: `$` (merge verdict into top-level state)
5. **Error handling** tab:
   - Add retry: `Bedrock.ThrottlingException, Bedrock.ServiceUnavailableException`, interval 5s, max 3, backoff 2.0
   - Add catch: `States.ALL`, result path `$.error`, next `RecordFailure`
6. Next state: `GuardrailCheck`

**State definition (reference):**
```json
"SynthesizeVerdict": {
  "Type": "Task",
  "Resource": "arn:aws:states:::lambda:invoke",
  "Parameters": {
    "FunctionName": "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-SynthesizeVerdict",
    "Payload.$": "$"
  },
  "ResultSelector": { "llm_verdict.$": "$.Payload" },
  "ResultPath": "$",
  "Retry": [
    {
      "ErrorEquals": ["Bedrock.ThrottlingException","Bedrock.ServiceUnavailableException"],
      "IntervalSeconds": 5, "MaxAttempts": 3, "BackoffRate": 2.0
    }
  ],
  "Catch": [{ "ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "RecordFailure" }],
  "Next": "GuardrailCheck"
}
```

---

### States 6-8: GuardrailCheck, ForceNeedsReview, PassThroughVerdict

**What they do:** GuardrailCheck is a Choice state that implements the confidence
guardrail. If the confidence score is below 0.85, the execution routes to
ForceNeedsReview. Otherwise -- including high-confidence DENY -- it routes to
PassThroughVerdict, which allows APPROVE, DENY, and NEEDS_REVIEW to all reach
RouteDecision directly.

- **ForceNeedsReview** (Pass): overrides the verdict to `NEEDS_REVIEW` and sets
  `override_reason` so downstream states and the adjuster know a downgrade occurred.
- **PassThroughVerdict** (Pass): copies `$.llm_verdict.recommendation` into
  `$.final_status` with no change.

Both Pass states re-emit the full input plus the final_status field so `WriteArtifacts`
receives a consistent shape regardless of which branch was taken.

**State type:** Choice (GuardrailCheck), Pass (ForceNeedsReview, PassThroughVerdict)

**In Workflow Studio:**
1. Drag **Choice** after `SynthesizeVerdict`
2. State name: `GuardrailCheck`
3. Add a rule with a single condition:
   - Variable: `$.llm_verdict.confidence` NumericLessThan `0.85`
   - If true -> next state: `ForceNeedsReview`
4. Default (all other cases) -> next state: `PassThroughVerdict`

5. Drag **Pass** for `ForceNeedsReview`:
   - State name: `ForceNeedsReview`
   - In the Definition tab, set Parameters:
     ```json
     {
       "client_id.$":        "$.client_id",
       "claim_id.$":         "$.claim_id",
       "submitted_at.$":     "$.submitted_at",
       "decisions_bucket.$": "$.decisions_bucket",
       "evidence.$":         "$.evidence",
       "llm_verdict.$":      "$.llm_verdict",
       "final_status":       "NEEDS_REVIEW",
       "override_reason.$":  "States.Format('Guardrail downgrade: original={}, confidence={}', $.llm_verdict.recommendation, $.llm_verdict.confidence)"
     }
     ```
   - Next state: `WriteArtifacts`

6. Drag **Pass** for `PassThroughVerdict`:
   - State name: `PassThroughVerdict`
   - In the Definition tab, set Parameters:
     ```json
     {
       "client_id.$":        "$.client_id",
       "claim_id.$":         "$.claim_id",
       "submitted_at.$":     "$.submitted_at",
       "decisions_bucket.$": "$.decisions_bucket",
       "evidence.$":         "$.evidence",
       "llm_verdict.$":      "$.llm_verdict",
       "final_status.$":     "$.llm_verdict.recommendation"
     }
     ```
   - Next state: `WriteArtifacts`

---

### State 9: WriteArtifacts

**What it does:** Invokes the WriteArtifacts Lambda, which writes 4 files to the
decisions bucket (`decision.json`, `client_letter.txt`, `adjuster_brief.md`,
`evidence_bundle.json`) and writes a record to DynamoDB. Returns `final_status`,
`client_letter`, and `adjuster_brief` so the downstream SNS states can publish the
right message body without re-reading S3.

**State type:** Task -> Lambda -> Invoke

**In Workflow Studio:**
1. Drag **Lambda: Invoke** after both `ForceNeedsReview` and `PassThroughVerdict`
2. State name: `WriteArtifacts`
3. Function name: `arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-WriteArtifacts`
4. **Input/Output** tab:
   - Result selector: `{"final_status.$":"$.Payload.final_status","claim_id.$":"$.Payload.claim_id","client_id.$":"$.Payload.client_id","client_letter.$":"$.Payload.client_letter","adjuster_brief.$":"$.Payload.adjuster_brief"}`
   - Result path: `$`
5. **Error handling** tab:
   - Add catch: `States.ALL`, result path `$.error`, next `RecordFailure`
6. Next state: `RouteDecision`

**State definition (reference):**
```json
"WriteArtifacts": {
  "Type": "Task",
  "Resource": "arn:aws:states:::lambda:invoke",
  "Parameters": {
    "FunctionName": "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-WriteArtifacts",
    "Payload.$": "$"
  },
  "ResultSelector": {
    "final_status.$":  "$.Payload.final_status",
    "claim_id.$":      "$.Payload.claim_id",
    "client_id.$":     "$.Payload.client_id",
    "client_letter.$": "$.Payload.client_letter",
    "adjuster_brief.$":"$.Payload.adjuster_brief"
  },
  "ResultPath": "$",
  "Catch": [{ "ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "RecordFailure" }],
  "Next": "RouteDecision"
}
```

---

### States 10-16: RouteDecision + notify + terminal states

**What they do:** RouteDecision is a Choice state that fans out based on `$.final_status`:

| Value | Path | Notification |
|-------|------|--------------|
| `APPROVE` | NotifyClaimantApproved -> ClaimApproved | SES email to claimant |
| `DENY` | NotifyClaimantDenied -> ClaimDenied | SES email to claimant |
| `NEEDS_REVIEW` (default) | NotifyAdjuster -> ClaimNeedsReview | SNS: AdjusterNotifications |

`NotifyClaimantApproved` and `NotifyClaimantDenied` are Task states using
`arn:aws:states:::aws-sdk:sesv2:sendEmail`. `NotifyAdjuster` uses
`arn:aws:states:::aws-sdk:sns:publish`. The three terminal states (`ClaimApproved`,
`ClaimDenied`, `ClaimNeedsReview`) are Succeed states that end the execution cleanly.

**In Workflow Studio:**
1. Drag **Choice** after `WriteArtifacts`
2. State name: `RouteDecision`
3. Rule 1: `$.final_status` StringEquals `APPROVE` -> `NotifyClaimantApproved`
4. Rule 2: `$.final_status` StringEquals `DENY` -> `NotifyClaimantDenied`
5. Default -> `NotifyAdjuster`

6. Drag **SES v2: Send email** for `NotifyClaimantApproved`:
   - State name: `NotifyClaimantApproved`
   - Resource: `arn:aws:states:::aws-sdk:sesv2:sendEmail`
   - Parameters (JSON editor):
     ```json
     {
       "FromEmailAddress": "<SENDER-EMAIL>",
       "Destination": { "ToAddresses": ["<CLAIMANT-EMAIL>"] },
       "Content": {
         "Simple": {
           "Subject": { "Data.$": "States.Format('Update on your claim {} - Approved', $.claim_id)", "Charset": "UTF-8" },
           "Body": { "Text": { "Data.$": "$.client_letter", "Charset": "UTF-8" } }
         }
       }
     }
     ```
   - Result path: `null`
   - Next: `ClaimApproved`

7. Drag **SES v2: Send email** for `NotifyClaimantDenied`:
   - Same parameters as above except Subject uses `'Update on your claim {} - Decision'`
   - Result path: `null`
   - Next: `ClaimDenied`

8. Drag **SNS: Publish** for `NotifyAdjuster`:
   - Topic ARN: `arn:aws:sns:<REGION>:<ACCOUNT-ID>:insurance-claims-ai-pipeline-AdjusterNotifications`
   - Subject: `States.Format('Claim {} requires adjuster review', $.claim_id)`
   - Message: `$.adjuster_brief`
   - Result path: `null`
   - Next: `ClaimNeedsReview`

9. Drag three **Succeed** states: `ClaimApproved`, `ClaimDenied`, `ClaimNeedsReview`

---

### State 17: RecordFailure

**What it does:** A Fail state that terminates the execution with a structured error.
No DynamoDB write -- the Step Functions execution history and CloudWatch Logs are the
failure surface.

**State type:** Fail

**In Workflow Studio:**
1. Drag **Fail** onto the canvas (it does not need to be in the main flow -- any Catch
   handler can point to it)
2. State name: `RecordFailure`
3. Error: `ClaimProcessingFailed`
4. Cause: `An error occurred during claim processing. See SFN execution history and CloudWatch Logs for details.`

---

### Configure and create

After all states are placed and wired:

1. Click **Next**
2. State machine name: `insurance-claims-ai-pipeline-ClaimsStateMachine`
3. Permissions: **Choose an existing role** -> `insurance-claims-ai-pipeline-StateMachineRole`
4. Logging:
   - Level: **ERROR** (or ALL for debugging)
   - CloudWatch log group: `/aws/states/insurance-claims-ai-pipeline-ClaimsStateMachine`
     (created automatically on first execution)
5. Tracing: leave X-Ray off
6. Click **Create**

Copy the state machine ARN from the detail page:
```
State Machine ARN: _______________________________________
```

---

### Shortcut: paste the complete ASL definition

Instead of building each state individually, you can switch to **Write your workflow
in code** mode and paste the complete definition below. This is faster for the initial
setup; use the state-by-state walkthrough above to understand what each state does.

1. Open **AWS Step Functions** -> **Create state machine**
2. Select **Write your workflow in code** -> **Standard**
3. Click **Next**

In the definition editor, delete all existing content and paste the following JSON.
**Before pasting**, do a global find-and-replace:
- Replace `<REGION>` with your region (e.g., `us-east-1` or `eu-central-1`)
- Replace `<ACCOUNT-ID>` with your 12-digit account ID

The `GuardrailCheck` state hardcodes `0.85` as the confidence threshold. You can
change this value directly in the definition to experiment with different thresholds.

```json
{
  "Comment": "insurance-claims-ai-pipeline: ReadManifest -> ValidateArtifacts (Map/HeadObject) -> ProcessArtifacts (Map) -> BuildEvidence (Pass) -> SynthesizeVerdict (Bedrock) -> GuardrailCheck (Choice) -> WriteArtifacts -> RouteDecision (3-way SNS fan-out).",
  "StartAt": "ReadManifest",
  "States": {

    "ReadManifest": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-ReadManifest",
        "Payload.$": "$"
      },
      "ResultSelector": {
        "body.$": "$.Payload"
      },
      "ResultPath": "$",
      "OutputPath": "$.body",
      "Retry": [
        {
          "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException"],
          "IntervalSeconds": 2,
          "MaxAttempts": 3,
          "BackoffRate": 2.0
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "RecordFailure"
        }
      ],
      "Next": "ValidateArtifacts"
    },

    "ValidateArtifacts": {
      "Type": "Map",
      "Comment": "HeadObject each artifact declared in the manifest. Any 404 fails the Map and routes to RecordFailure. ResultPath null discards Map output and passes the full input state downstream unchanged.",
      "ItemsPath": "$.artifacts",
      "ItemSelector": {
        "artifact.$": "$$.Map.Item.Value"
      },
      "MaxConcurrency": 10,
      "ResultPath": null,
      "ItemProcessor": {
        "ProcessorConfig": {
          "Mode": "INLINE"
        },
        "StartAt": "HeadObject",
        "States": {
          "HeadObject": {
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:s3:headObject",
            "Parameters": {
              "Bucket.$": "$.artifact.intake_bucket",
              "Key.$": "$.artifact.key"
            },
            "ResultPath": null,
            "End": true
          }
        }
      },
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "RecordFailure"
        }
      ],
      "Next": "ProcessArtifacts"
    },

    "ProcessArtifacts": {
      "Type": "Map",
      "Comment": "Fan out per artifact. Each iteration routes by type and stores service output at $.analysis. Map output: array of {artifact, analysis} objects placed at $.artifact_results.",
      "ItemsPath": "$.artifacts",
      "ItemSelector": {
        "artifact.$": "$$.Map.Item.Value"
      },
      "MaxConcurrency": 10,
      "ResultPath": "$.artifact_results",
      "ItemProcessor": {
        "ProcessorConfig": {
          "Mode": "INLINE"
        },
        "StartAt": "ClassifyArtifact",
        "States": {

          "ClassifyArtifact": {
            "Type": "Choice",
            "Choices": [
              {
                "Variable": "$.artifact.type",
                "StringEquals": "image",
                "Next": "DetectLabels"
              },
              {
                "Variable": "$.artifact.type",
                "StringEquals": "document",
                "Next": "AnalyzeDocument"
              },
              {
                "Variable": "$.artifact.type",
                "StringEquals": "text",
                "Next": "ReadText"
              }
            ],
            "Default": "UnknownArtifactType"
          },

          "DetectLabels": {
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:rekognition:detectLabels",
            "Parameters": {
              "Image": {
                "S3Object": {
                  "Bucket.$": "$.artifact.intake_bucket",
                  "Name.$": "$.artifact.key"
                }
              },
              "MaxLabels": 30,
              "MinConfidence": 50
            },
            "ResultSelector": {
              "type": "image",
              "Labels.$": "$.Labels"
            },
            "ResultPath": "$.analysis",
            "Retry": [
              {
                "ErrorEquals": ["Rekognition.ProvisionedThroughputExceededException"],
                "IntervalSeconds": 2,
                "MaxAttempts": 3,
                "BackoffRate": 2.0
              }
            ],
            "End": true
          },

          "AnalyzeDocument": {
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:textract:analyzeDocument",
            "Parameters": {
              "Document": {
                "S3Object": {
                  "Bucket.$": "$.artifact.intake_bucket",
                  "Name.$": "$.artifact.key"
                }
              },
              "FeatureTypes": ["FORMS", "TABLES"]
            },
            "ResultSelector": {
              "type": "document",
              "Blocks.$": "$.Blocks"
            },
            "ResultPath": "$.analysis",
            "Retry": [
              {
                "ErrorEquals": ["Textract.ProvisionedThroughputExceededException"],
                "IntervalSeconds": 5,
                "MaxAttempts": 3,
                "BackoffRate": 2.0
              }
            ],
            "End": true
          },

          "ReadText": {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {
              "FunctionName": "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-ReadText",
              "Payload.$": "$"
            },
            "ResultSelector": {
              "type.$": "$.Payload.type",
              "key.$": "$.Payload.key",
              "content.$": "$.Payload.content"
            },
            "ResultPath": "$.analysis",
            "Retry": [
              {
                "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException"],
                "IntervalSeconds": 2,
                "MaxAttempts": 3,
                "BackoffRate": 2.0
              }
            ],
            "End": true
          },

          "UnknownArtifactType": {
            "Type": "Fail",
            "Error": "UnknownArtifactType",
            "Cause": "Artifact type is not image, document, or text. Check manifest validation in ReadManifest Lambda."
          }

        }
      },
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "RecordFailure"
        }
      ],
      "Next": "BuildEvidence"
    },

    "BuildEvidence": {
      "Type": "Pass",
      "Comment": "Assembles ProcessArtifacts output ($.artifact_results) into the shape SynthesizeVerdict expects. Drops artifacts and artifact_results from downstream state.",
      "Parameters": {
        "client_id.$": "$.client_id",
        "claim_id.$": "$.claim_id",
        "submitted_at.$": "$.submitted_at",
        "decisions_bucket.$": "$.decisions_bucket",
        "evidence": {
          "artifacts.$": "$.artifact_results"
        }
      },
      "Next": "SynthesizeVerdict"
    },

    "SynthesizeVerdict": {
      "Type": "Task",
      "Comment": "Calls Bedrock Claude. ASL Retry owns retry logic. Lambda uses max_attempts=1 to avoid double-retry.",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-SynthesizeVerdict",
        "Payload.$": "$"
      },
      "ResultSelector": {
        "body.$": "$.Payload"
      },
      "ResultPath": "$",
      "OutputPath": "$.body",
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "States.TaskFailed"
          ],
          "IntervalSeconds": 2,
          "MaxAttempts": 3,
          "BackoffRate": 2.0
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "RecordFailure"
        }
      ],
      "Next": "GuardrailCheck"
    },

    "GuardrailCheck": {
      "Type": "Choice",
      "Comment": "Downgrades to NEEDS_REVIEW if confidence is below 0.85 (low-trust signal). High-confidence DENY passes through to RouteDecision. ASL Choice -- no Lambda needed.",
      "Choices": [
        {
          "Or": [
            {
              "Variable": "$.llm_verdict.recommendation",
              "StringEquals": "DENY"
            },
            {
              "Variable": "$.llm_verdict.confidence",
              "NumericLessThan": 0.85
            }
          ],
          "Next": "ForceNeedsReview"
        }
      ],
      "Default": "PassThroughVerdict"
    },

    "ForceNeedsReview": {
      "Type": "Pass",
      "Comment": "Guardrail override: sets final_status to NEEDS_REVIEW and records why. Original llm_verdict preserved for audit.",
      "Parameters": {
        "client_id.$": "$.client_id",
        "claim_id.$": "$.claim_id",
        "submitted_at.$": "$.submitted_at",
        "decisions_bucket.$": "$.decisions_bucket",
        "evidence.$": "$.evidence",
        "llm_verdict.$": "$.llm_verdict",
        "final_status": "NEEDS_REVIEW",
        "override_reason.$": "States.Format('Guardrail downgrade: original={}, confidence={}', $.llm_verdict.recommendation, $.llm_verdict.confidence)"
      },
      "Next": "WriteArtifacts"
    },

    "PassThroughVerdict": {
      "Type": "Pass",
      "Comment": "No override: copies llm_verdict.recommendation to final_status and passes all fields forward.",
      "Parameters": {
        "client_id.$": "$.client_id",
        "claim_id.$": "$.claim_id",
        "submitted_at.$": "$.submitted_at",
        "decisions_bucket.$": "$.decisions_bucket",
        "evidence.$": "$.evidence",
        "llm_verdict.$": "$.llm_verdict",
        "final_status.$": "$.llm_verdict.recommendation"
      },
      "Next": "WriteArtifacts"
    },

    "WriteArtifacts": {
      "Type": "Task",
      "Comment": "Writes decision.json, client_letter.txt, adjuster_brief.md, evidence_bundle.json to S3 and a record to DynamoDB. Returns final_status, client_letter, adjuster_brief for downstream SNS states.",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:insurance-claims-ai-pipeline-WriteArtifacts",
        "Payload.$": "$"
      },
      "ResultSelector": {
        "body.$": "$.Payload"
      },
      "ResultPath": "$",
      "OutputPath": "$.body",
      "Retry": [
        {
          "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException"],
          "IntervalSeconds": 2,
          "MaxAttempts": 2,
          "BackoffRate": 2.0
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "RecordFailure"
        }
      ],
      "Next": "RouteDecision"
    },

    "RouteDecision": {
      "Type": "Choice",
      "Comment": "Fan out to claimant via SES (APPROVE/DENY) or adjuster via SNS (NEEDS_REVIEW).",
      "Choices": [
        {
          "Variable": "$.final_status",
          "StringEquals": "APPROVE",
          "Next": "NotifyClaimantApproved"
        },
        {
          "Variable": "$.final_status",
          "StringEquals": "DENY",
          "Next": "NotifyClaimantDenied"
        }
      ],
      "Default": "NotifyAdjuster"
    },

    "NotifyClaimantApproved": {
      "Type": "Task",
      "Resource": "arn:aws:states:::aws-sdk:sesv2:sendEmail",
      "Parameters": {
        "FromEmailAddress": "<SENDER-EMAIL>",
        "Destination": {
          "ToAddresses": ["<CLAIMANT-EMAIL>"]
        },
        "Content": {
          "Simple": {
            "Subject": {
              "Data.$": "States.Format('Update on your claim {} - Approved', $.claim_id)",
              "Charset": "UTF-8"
            },
            "Body": {
              "Text": {
                "Data.$": "$.client_letter",
                "Charset": "UTF-8"
              }
            }
          }
        }
      },
      "ResultPath": null,
      "Next": "ClaimApproved"
    },

    "NotifyClaimantDenied": {
      "Type": "Task",
      "Resource": "arn:aws:states:::aws-sdk:sesv2:sendEmail",
      "Parameters": {
        "FromEmailAddress": "<SENDER-EMAIL>",
        "Destination": {
          "ToAddresses": ["<CLAIMANT-EMAIL>"]
        },
        "Content": {
          "Simple": {
            "Subject": {
              "Data.$": "States.Format('Update on your claim {} - Decision', $.claim_id)",
              "Charset": "UTF-8"
            },
            "Body": {
              "Text": {
                "Data.$": "$.client_letter",
                "Charset": "UTF-8"
              }
            }
          }
        }
      },
      "ResultPath": null,
      "Next": "ClaimDenied"
    },

    "NotifyAdjuster": {
      "Type": "Task",
      "Resource": "arn:aws:states:::aws-sdk:sns:publish",
      "Parameters": {
        "TopicArn": "arn:aws:sns:<REGION>:<ACCOUNT-ID>:insurance-claims-ai-pipeline-AdjusterNotifications",
        "Subject.$": "States.Format('Claim {} requires adjuster review', $.claim_id)",
        "Message.$": "$.adjuster_brief"
      },
      "ResultPath": null,
      "Next": "ClaimNeedsReview"
    },

    "ClaimApproved": {
      "Type": "Succeed",
      "Comment": "Claim approved. Check decisions S3 bucket for client_letter.txt and decision.json."
    },

    "ClaimDenied": {
      "Type": "Succeed",
      "Comment": "Claim denied. Check decisions S3 bucket for client_letter.txt with denial reasons and appeal instructions."
    },

    "ClaimNeedsReview": {
      "Type": "Succeed",
      "Comment": "Claim routed to adjuster review. Check decisions S3 bucket for adjuster_brief.md."
    },

    "RecordFailure": {
      "Type": "Fail",
      "Error": "ClaimProcessingFailed",
      "Cause": "An error occurred during claim processing. See SFN execution history and CloudWatch Logs for details."
    }

  }
}
```

6. Click **Next**
7. State machine name: `insurance-claims-ai-pipeline-ClaimsStateMachine`
8. Permissions: **Choose an existing role** -> `insurance-claims-ai-pipeline-StateMachineRole`
9. Logging:
   - Level: **ERROR** (or ALL for debugging)
   - CloudWatch log group: type `/aws/states/insurance-claims-ai-pipeline-ClaimsStateMachine`
     (created automatically on first execution)
10. Tracing: leave X-Ray tracing off for now (can enable later)
11. Click **Create**

Copy the state machine ARN from the detail page:
```
State Machine ARN: _______________________________________
```

**Teaching note -- what stays in ASL vs what needs a Lambda:**

| Task | Location | Why |
|------|----------|-----|
| Read manifest.json | Lambda | S3 GetObject + JSON parse + schema validation |
| HeadObject per artifact | ASL (Map + aws-sdk) | Direct SDK call, no business logic |
| Rekognition / Textract | ASL (Map + aws-sdk) | Direct SDK call, no transform needed |
| Read text file | Lambda | S3 GetObject + UTF-8 decode + length guard |
| Assemble evidence dict | ASL (Pass) | Pure data reshape via intrinsic functions |
| Bedrock invocation | Lambda | Prompt construction + JSON parse + validation |
| Guardrail routing | ASL (Choice + Pass) | Binary condition on two fields -- no code |
| Write 4 S3 files + DDB | Lambda | Multi-step write with formatting logic |
| SNS publish | ASL (aws-sdk) | Single API call, no transform |

---

## S10 -- Create EventBridge rule

The rule fires whenever a file named `manifest.json` is created in the intake bucket.
It starts the Step Functions execution with the S3 event as the input.

1. Open **Amazon EventBridge** in the console
2. Click **Rules** in the left nav -> **Create rule**
3. Name: `insurance-claims-ai-pipeline-IntakeManifestRule`
4. Event bus: **default**
5. Rule type: **Rule with an event pattern**
6. Click **Next**

**Event source:**
7. Event source: **AWS events or EventBridge partner events**

**Event pattern:**
8. Event pattern method: **Custom pattern (JSON editor)**
9. Paste the following pattern (replace `<ACCOUNT-ID>` with your account ID):

```json
{
  "source": ["aws.s3"],
  "detail-type": ["Object Created"],
  "detail": {
    "bucket": {
      "name": ["insurance-claims-ai-pipeline-intake-<ACCOUNT-ID>"]
    },
    "object": {
      "key": [{"suffix": "manifest.json"}]
    }
  }
}
```

10. Click **Next**

**Target:**
11. Target types: **AWS service**
12. Select a target: **Step Functions state machine**
13. State machine: select `insurance-claims-ai-pipeline-ClaimsStateMachine`
14. Execution role: **Use an existing role** -> `insurance-claims-ai-pipeline-EventBridgeRole`
15. Click **Next**
16. Tags: optional, skip
17. Click **Next** -> **Create rule**

---

## S11 -- Seed a sample claim

Sample artifacts are in the `samples/` directory of the repo. Upload them via the
S3 console. The order matters: upload all three artifacts first, then upload the
manifest LAST. The EventBridge rule fires on the manifest upload, and the
`ValidateArtifacts` Map state will HeadObject each declared artifact -- if you
upload the manifest before the photo finishes, the pipeline fails.

### Upload artifacts first

1. Open **S3** in the console
2. Navigate to `insurance-claims-ai-pipeline-intake-<ACCOUNT-ID>`
3. Create the folder path `clients/acme-corp/CLM-001/`:
   Click **Create folder**, type `clients`, click **Create folder**.
   Repeat for `acme-corp` inside `clients`, then `CLM-001` inside `acme-corp`.
   (Or use the Upload button to set the prefix directly.)

4. Navigate into `clients/acme-corp/CLM-001/`
5. Click **Upload** -> **Add files**
6. Select these three files from your local `samples/approve/` folder:
   - `photo-damage.jpg`
   - `police-report.pdf`
   - `statement.txt`
7. Click **Upload**
8. Wait for all three uploads to show **Succeeded** before continuing.

### Upload the manifest last

9. Click **Upload** -> **Add files**
10. Select `samples/approve/manifest.json`
11. Click **Upload**

The pipeline starts within about 5 seconds. Switch to Step Functions to watch
the execution before the upload dialog even closes.

---

## S12 -- Watch the execution

1. Open **Step Functions** in the console
2. Click **State machines** -> `insurance-claims-ai-pipeline-ClaimsStateMachine`
3. Click the most recent execution (status shows **Running** or just completed)

What to observe in the visual workflow:

- **ValidateArtifacts**: the Map state spawns one HeadObject branch per artifact (3
  parallel HeadObject calls for the sample set). These call S3 directly -- no Lambda.
  `ResultPath: null` means the Map output is discarded; the original input passes
  through unchanged.

- **ProcessArtifacts**: a second Map state, also running in parallel. Each iteration
  goes through ClassifyArtifact (Choice) and routes to DetectLabels, AnalyzeDocument,
  or ReadText. `ResultPath: $.analysis` merges each result into the iteration context,
  so the output of each iteration is `{artifact: {...}, analysis: {...}}`.

- **BuildEvidence**: a Pass state -- no Lambda, no API call. It reshapes
  `$.artifact_results` (the Map output) into `evidence.artifacts` and drops fields
  SynthesizeVerdict does not need.

- **SynthesizeVerdict**: expect 5-15 seconds for Haiku to respond.

- **GuardrailCheck**: a Choice state that routes based on two fields. No Lambda needed.

- **RouteDecision**: the final Choice state -- three branches, three distinct
  Succeed terminal states.

Click on any state to see its input and output. This is the primary debugging tool --
you can see exactly what data flows between each step.

---

## S13 -- Inspect outputs and trigger the guardrail

### Inspect the happy path (CLM-001)

With the clean sample set, `CLM-001` will most likely produce `final_status=APPROVE`
at confidence >= 0.85. The statement is consistent with the photo and police report,
with no red flags and a cooperating at-fault driver.

If Bedrock returns a lower confidence on your run, `final_status` will be `NEEDS_REVIEW`
via the guardrail. The guardrail fires on confidence < 0.85 regardless of recommendation.
Note: the Comprehend fields (`sentiment`, `sentiment_scores`, `key_phrases`) will be
visible in `evidence_bundle.json` under the text artifact analysis.

**Decision artifacts in S3:**

1. Open S3 -> `insurance-claims-ai-pipeline-decisions-<ACCOUNT-ID>`
2. Navigate to `clients/acme-corp/CLM-001/`
3. You should see four files:
   - `decision.json` -- structured record: final_status, confidence, override_reason,
     original_recommendation, denial_reasons, red_flags
   - `client_letter.txt` -- the claimant-facing letter. Open it and verify:
     no confidence numbers, no red flags, no mention of AI or automation
   - `adjuster_brief.md` -- the internal brief. Open it and verify: full reasoning,
     red flags, raw Bedrock JSON output for audit
   - `evidence_bundle.json` -- complete audit record: all three files above plus
     original LLM output and evidence passed to Bedrock

4. Download and compare `client_letter.txt` vs `adjuster_brief.md`:
   - Both were generated in a single Bedrock call
   - The separation is enforced at the prompt level and at the file-write level
   - The client letter will NEVER contain red flags or internal reasoning even if
     `final_status=DENY`

**DynamoDB record:**

1. Open **DynamoDB** -> **Tables** -> `insurance-claims-ai-pipeline-ClaimsDecisions`
2. Click **Explore table items**
3. You should see a row with `client_id=acme-corp`, `claim_id=CLM-001`
4. Expand all attributes: `final_status`, `confidence`, `original_recommendation`,
   `override_reason`, `denial_reasons`, `red_flags`, etc.

### Trigger the guardrail (CLM-002)

The red-flag sample set contains a claimant statement with multiple suspicious signals:
- No police report (placeholder only)
- Vague incident location and time
- Damage inconsistent with stated cause
- Coverage increased 14 days before the incident

Upload CLM-002 the same way as CLM-001, but using files from `samples/red-flag/`:
1. Create path `clients/acme-corp/CLM-002/` in the intake bucket
2. Upload `samples/red-flag/photo-damage.jpg`, `police-report.pdf`, `statement.txt`
3. Upload `samples/red-flag/manifest.json` LAST

After the execution completes, compare CLM-001 and CLM-002 in the decision artifacts:
- `decision.json -> original_recommendation`: the raw LLM recommendation (may show DENY)
- `decision.json -> final_status`: the post-guardrail effective status (NEEDS_REVIEW)
- `decision.json -> override_reason`: explains which guardrail condition fired
- `client_letter.txt` for CLM-002: uses the generic "under review" template -- the
  LLM's denial reasoning is NOT leaked to the claimant regardless of the LLM output
- `adjuster_brief.md` for CLM-002: includes denial_reasons, red_flags, override_reason

**Teaching note -- why this guardrail pattern exists:**

Real claim adjudication systems *do* auto-deny claims -- but at the deterministic
rules-engine layer: policy lapsed, claim outside the coverage window, claimant on
a fraud watchlist. These are binary, verifiable facts.

This guardrail targets a different signal: probabilistic uncertainty from an LLM.
The confidence score quantifies the model's own uncertainty. When confidence is low,
human judgment adds the most value regardless of which direction the LLM is leaning.
High-confidence decisions -- both APPROVE and DENY -- pass through and trigger the
appropriate claimant notification automatically.

This is a "low-confidence -> human-in-the-loop" escalation pattern, not a blanket
refusal to auto-deny. For deterministic rules-engine denials (policy lapse, fraud
list hit), see Extension E4.

### Try the DENY path (CLM-003)

The deny sample set contains a statement that claims a **bicycle** was damaged under
an auto insurance policy. The photo evidence (Rekognition labels: Car, Vehicle) does
not match the claimed item. This creates a clear coverage ineligibility and
evidence mismatch that Bedrock will flag with high confidence.

Upload CLM-003 the same way as CLM-001, using files from `samples/deny/`:
1. Create path `clients/acme-corp/CLM-003/` in the intake bucket
2. Upload `samples/deny/photo-damage.jpg`, `police-report.pdf`, `statement.txt`
3. Upload `samples/deny/manifest.json` LAST

After the execution completes:
- `GuardrailCheck` routes to `PassThroughVerdict` (confidence >= 0.85)
- `RouteDecision` routes to `NotifyClaimantDenied`
- `decision.json -> final_status`: `DENY`
- `client_letter.txt`: professional denial letter with plain-language `denial_reasons`
  and appeal instructions -- no internal reasoning leaked
- `adjuster_brief.md`: full `denial_reasons`, `red_flags`, raw Bedrock output
- SES email sent to claimant only (adjuster is NOT notified)

Compare the three claim outputs side by side:
- CLM-001: APPROVE -- consistent evidence, auto-notified claimant
- CLM-002: NEEDS_REVIEW -- low-confidence or ambiguous signals, adjuster notified
- CLM-003: DENY -- clear coverage/evidence mismatch, auto-notified claimant with denial

---

## S14 -- Modify the prompt

The system prompt, output schema, and evidence formatting are all in the
`SynthesizeVerdict` Lambda function. Edit them directly in the console.

1. Open **Lambda** -> `insurance-claims-ai-pipeline-SynthesizeVerdict`
2. Go to the **Code** tab
3. Locate `SYSTEM_PROMPT`

Try:
- Adding a new field to the output schema (e.g., `"suggested_investigation_steps": [...]`)
  and updating `_validate_verdict` to require it
- Changing the adjudicator role framing in the system prompt ("senior adjuster" vs
  "fraud analyst")
- Adjusting the strictness of the `client_letter` rules

4. Click **Deploy** after editing
5. Re-upload a claim manifest (S11) to trigger a new run and observe the changed output

---

## S15 -- Swap models

The model ID is stored in the `SynthesizeVerdict` Lambda environment variable.

1. Open **Lambda** -> `insurance-claims-ai-pipeline-SynthesizeVerdict`
2. Go to **Configuration** -> **Environment variables** -> **Edit**
3. Find the `BEDROCK_MODEL_ID` key

To find the current Sonnet cross-region inference profile ID for your region group:
1. Open **Amazon Bedrock** -> **Cross-region inference**
2. Look for the Sonnet profile matching your region group (`us.anthropic.claude-sonnet-*`
   or `eu.anthropic.claude-sonnet-*`)
3. Copy the full profile ID

4. Update the Lambda environment variable and re-run a claim.

Also update `SynthesizeVerdictRole` in IAM: replace the `claude-haiku-*` foundation
model ARN with the corresponding `claude-sonnet-*` ARN.

**Cost note**: Sonnet costs approximately 15-30x more than Haiku per API call.
At the same 3-artifact sample:
- Haiku run: ~$0.07 total
- Sonnet run: ~$0.09-0.10 total

The Bedrock line item jumps 15-30x; total run cost only increases ~30% because
Textract dominates. Optimizing the LLM call has diminishing returns when Textract
is the cost driver.

---

## S16 -- Observability

### CloudWatch Logs

Each Lambda has its own log group, created automatically when the Lambda first runs.

To view logs for a specific Lambda:
1. Open **CloudWatch** -> **Log groups**
2. Find the log group for your Lambda:
   - `/aws/lambda/insurance-claims-ai-pipeline-ReadManifest`
   - `/aws/lambda/insurance-claims-ai-pipeline-ReadText`
   - `/aws/lambda/insurance-claims-ai-pipeline-SynthesizeVerdict`
   - `/aws/lambda/insurance-claims-ai-pipeline-WriteArtifacts`
3. Click the most recent log stream

For `SynthesizeVerdict`, look for the Bedrock response time in the Lambda duration
line. Anything over 5 seconds indicates Bedrock throttling or cross-region routing.

To set retention on a log group:
1. Click the log group name
2. Click **Actions** -> **Edit retention setting**
3. Set to **14 days**

### Step Functions execution history

Standard workflows retain full per-state event history for 90 days:
1. Open Step Functions -> state machine -> click an execution
2. Click the **Events** tab
3. Each state transition shows: timestamp, event type, input, output

The Events tab is more detailed than the visual workflow view -- it shows the raw
input/output JSON for every state, including direct SDK integration responses.

### X-Ray tracing (optional)

1. Open the state machine -> **Edit**
2. Under Tracing, enable **X-Ray tracing**
3. Save and re-run a claim
4. Open **X-Ray** in the console to view the service map and per-state timing

---

## S17 -- Security walkthrough

Open the IAM roles you created in S7 and review the policy decisions.

### Why Rekognition and Textract need Resource: "*"

In the `StateMachineRole` inline policy:
```json
{"Action": "rekognition:DetectLabels", "Resource": "*"}
{"Action": "textract:AnalyzeDocument",  "Resource": "*"}
```

**Rekognition**: `DetectLabels` has no resource-level ARN. There is no ARN for a
"Rekognition model" -- the service is fully managed. The action is the full constraint.

**Textract**: Stronger case -- Textract has no resource-level permissions at all.
It is not possible to write a Textract policy statement with a non-wildcard Resource.

**Bedrock**: Different. Bedrock supports resource-level permissions on inference
profile ARNs and foundation-model ARNs. The `SynthesizeVerdictRole` policy covers
both: the inference profile in your deployment region, and the foundation model
with a wildcard region (required because the profile routes dynamically -- see S2).

### Least-privilege by design

- **One role per Lambda**: WriteArtifacts cannot call Rekognition; ReadManifest
  cannot write to the decisions bucket. Each Lambda has exactly the permissions it uses.
- **No unnecessary Lambdas**: ValidateArtifacts, BuildEvidence, and GuardrailCheck
  are all handled inside Step Functions. Removing those Lambdas removes three IAM
  roles entirely.
- **Path-scoped S3 resources**: all S3 policies grant on `...intake.../clients/*`
  or `...decisions.../clients/*` -- not the entire bucket.
- **No DynamoDB in StateMachineRole**: the state machine never writes to DynamoDB
  directly. WriteArtifacts Lambda owns that write, scoped to its own role.
- **SNS adjuster + SES claimant**: the StateMachineRole grants `sns:Publish` on the
  adjuster topic and `ses:SendEmail` scoped to the verified sender identity. The Choice
  state logic ensures only one notification path fires per execution.
- **DeletionPolicy**: this lab uses DynamoDB without deletion protection, appropriate
  for a demo. In production, enable deletion protection under Table -> Additional
  settings -> Deletion protection.

---

## S18 -- Teardown

Delete resources in this order to avoid dependency conflicts.

### 1. Empty S3 buckets

For each bucket (`intake` and `decisions`):
1. Open S3 -> click the bucket name
2. Click **Empty**
3. Type `permanently delete` in the confirmation box
4. Click **Empty**

Wait for the Empty operation to complete before proceeding.

### 2. Delete EventBridge rule

1. Open **EventBridge** -> **Rules**
2. Select `insurance-claims-ai-pipeline-IntakeManifestRule`
3. Click **Delete** -> confirm

### 3. Delete S3 buckets

1. Open S3
2. Select `insurance-claims-ai-pipeline-intake-<ACCOUNT-ID>` -> **Delete**
3. Type the bucket name to confirm -> **Delete bucket**
4. Repeat for `insurance-claims-ai-pipeline-decisions-<ACCOUNT-ID>`

### 4. Delete Lambda functions

1. Open **Lambda** -> **Functions**
2. Select all 4 `insurance-claims-ai-pipeline-*` functions (checkbox)
3. Click **Actions** -> **Delete**
4. Type `delete` to confirm -> **Delete**

### 5. Delete Step Functions state machine

1. Open **Step Functions** -> **State machines**
2. Select `insurance-claims-ai-pipeline-ClaimsStateMachine`
3. Click **Delete** -> **Delete state machine**

### 6. Delete SNS subscriptions and topic

1. Open **SNS** -> **Subscriptions**
2. If you created an adjuster email subscription, select it -> **Delete**
3. Open **Topics** -> select `insurance-claims-ai-pipeline-AdjusterNotifications`
4. Click **Delete** -> type `delete me` -> **Delete**

Note: SES verified identities are not stack resources and are not deleted by teardown.
They persist in your account and can be reused across labs. To remove them manually:
SES console -> **Verified identities** -> select identity -> **Delete**.

### 7. Delete DynamoDB table

1. Open **DynamoDB** -> **Tables**
2. Select `insurance-claims-ai-pipeline-ClaimsDecisions`
3. Click **Delete** -> type `delete` -> **Delete table**

### 8. Delete IAM roles

For each of the 6 roles created in S7:
1. Open **IAM** -> **Roles**
2. Search for `insurance-claims-ai-pipeline`
3. Select each role -> click **Delete** -> type the role name to confirm

Roles to delete:
- `insurance-claims-ai-pipeline-ReadManifestRole`
- `insurance-claims-ai-pipeline-ReadTextRole`
- `insurance-claims-ai-pipeline-SynthesizeVerdictRole`
- `insurance-claims-ai-pipeline-WriteArtifactsRole`
- `insurance-claims-ai-pipeline-StateMachineRole`
- `insurance-claims-ai-pipeline-EventBridgeRole`

### 9. Delete CloudWatch log groups (if created)

1. Open **CloudWatch** -> **Log groups**
2. Filter by `insurance-claims-ai-pipeline`
3. Select all matching log groups -> **Actions** -> **Delete log group(s)**

### 10. Verify teardown is complete

Before re-deploying or closing the lab, confirm no named resources remain.
If any of the following still exist, delete them now -- leftover named resources
will cause a `ResourceExistenceCheck` failure if you attempt another deployment.

```bash
aws lambda list-functions \
  --query 'Functions[?contains(FunctionName, `insurance-claims`)].FunctionName' \
  --output json

aws stepfunctions list-state-machines \
  --query 'stateMachines[?contains(name, `insurance-claims`)].name' \
  --output json

aws dynamodb list-tables \
  --query 'TableNames[?contains(@, `insurance-claims`)]' \
  --output json

aws sns list-topics \
  --query 'Topics[*].TopicArn' --output json \
  | python3 -c "import sys,json; [print(a) for a in json.load(sys.stdin) if 'insurance-claims' in a]"

aws events list-rules \
  --query 'Rules[?contains(Name, `insurance-claims`)].Name' \
  --output json
```

All five commands should return empty lists (`[]`) before you proceed.

---

## Appendix A -- CloudFormation shortcut

Everything built manually in S4-S10 can be deployed in a single command using the
CloudFormation template in `cfn/template.yaml`. This is the deployment shortcut --
use it after you have walked through the manual steps and understand what each resource
does.

### Prerequisites for CFN deploy

You need:
- AWS CLI v2: `aws --version` (must show 2.x)
- A staging S3 bucket for the template (the template exceeds the 51 KB inline limit)
- Bedrock model access confirmed (S2)

### Create a staging bucket

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1   # or eu-central-1

aws s3 mb s3://cfn-templates-${ACCOUNT} --region ${REGION}
```

Delete this bucket after deployment -- it only holds the template file.

### Verify the Bedrock model ID

The template default is `us.anthropic.claude-haiku-4-5-20251001-v1:0` (US group).
For eu-central-1, use `eu.anthropic.claude-haiku-4-5-20251001-v1:0`.

To confirm the current profile ID:
1. Open **Amazon Bedrock** -> **Cross-region inference** -> Inference profiles
2. Confirm the profile ID matches the template default

### Deploy

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1   # replace with your region

# US deployment (us-east-1):
aws cloudformation deploy \
  --template-file cfn/template.yaml \
  --stack-name insurance-claims-ai-pipeline \
  --s3-bucket cfn-templates-${ACCOUNT} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ${REGION} \
  --parameter-overrides \
      BedrockModelId=us.anthropic.claude-haiku-4-5-20251001-v1:0 \
      SenderEmail=claims@yourdomain.com \
      ClaimantEmail=you@example.com \
      AdjusterEmail=you@example.com

# EU deployment (eu-central-1):
aws cloudformation deploy \
  --template-file cfn/template.yaml \
  --stack-name insurance-claims-ai-pipeline \
  --s3-bucket cfn-templates-${ACCOUNT} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region eu-central-1 \
  --parameter-overrides \
      BedrockModelId=eu.anthropic.claude-haiku-4-5-20251001-v1:0 \
      SenderEmail=claims@yourdomain.com \
      ClaimantEmail=you@example.com \
      AdjusterEmail=you@example.com
```

Stack creation takes 3-4 minutes. When complete:

```bash
aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --region ${REGION} \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table
```

### Seed via CLI

```bash
INTAKE_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --region ${REGION} \
  --query 'Stacks[0].Outputs[?OutputKey==`IntakeBucketName`].OutputValue' \
  --output text)

PREFIX=clients/acme-corp/CLM-001

aws s3 cp samples/approve/photo-damage.jpg   s3://${INTAKE_BUCKET}/${PREFIX}/photo-damage.jpg   --region ${REGION}
aws s3 cp samples/approve/police-report.pdf  s3://${INTAKE_BUCKET}/${PREFIX}/police-report.pdf  --region ${REGION}
aws s3 cp samples/approve/statement.txt      s3://${INTAKE_BUCKET}/${PREFIX}/statement.txt      --region ${REGION}
aws s3 cp samples/approve/manifest.json      s3://${INTAKE_BUCKET}/${PREFIX}/manifest.json      --region ${REGION}
```

### CFN teardown

```bash
# Empty both buckets first (required before stack delete)
INTAKE=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline --region ${REGION} \
  --query 'Stacks[0].Outputs[?OutputKey==`IntakeBucketName`].OutputValue' --output text)

DECISIONS=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline --region ${REGION} \
  --query 'Stacks[0].Outputs[?OutputKey==`DecisionsBucketName`].OutputValue' --output text)

aws s3 rm s3://${INTAKE}    --recursive --region ${REGION}
aws s3 rm s3://${DECISIONS} --recursive --region ${REGION}

# Delete the stack
aws cloudformation delete-stack \
  --stack-name insurance-claims-ai-pipeline \
  --region ${REGION}

aws cloudformation wait stack-delete-complete \
  --stack-name insurance-claims-ai-pipeline \
  --region ${REGION}
```

Buckets are NOT versioned, so `aws s3 rm --recursive` is sufficient before the stack
delete. No additional version purge is needed.

After `stack-delete-complete` returns, confirm no named resources remain by running
the verification commands in S18 Step 10. A `DELETE_COMPLETE` stack status does not
guarantee all resources were removed -- if any named resources (Lambda, state machine,
DynamoDB table, SNS topics, EventBridge rule) still appear, delete them manually before
attempting another deployment. Leftover named resources cause a
`ResourceExistenceCheck` hook failure at changeset creation and are not obvious to
diagnose.

### Modify prompt or swap models via CFN

After editing `cfn/template.yaml` (Lambda code or parameters):
```bash
aws cloudformation deploy \
  --template-file cfn/template.yaml \
  --stack-name insurance-claims-ai-pipeline \
  --s3-bucket cfn-templates-${ACCOUNT} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ${REGION}
```

`aws cloudformation deploy` is idempotent -- it only updates resources that changed.

---

## Appendix B -- Cost estimate

Per run (3 artifacts: 1 JPEG, 1 single-page PDF, 1 text), Haiku 4.5:

| Service | Detail | Cost |
|---------|--------|------|
| Rekognition DetectLabels | 1 image | ~$0.001 |
| Textract AnalyzeDocument | FORMS + TABLES, 1 page | ~$0.065 |
| Bedrock Haiku 4.5 | ~2K input + ~500 output tokens | ~$0.001 |
| SFN Standard | ~20 state transitions @ $0.000025 each | ~$0.0005 |
| Lambda | 4 invocations, 256-512 MB, < 5s each | < $0.001 |
| DynamoDB on-demand | 1 PutItem | < $0.001 |
| S3 | GetObject reads + 4 PutObject writes | < $0.001 |
| SNS | 1 Publish | < $0.001 |
| **Total** | | **~$0.07/run** |

Per run, Sonnet swap (S15):

| Change | Cost |
|--------|------|
| All non-Bedrock costs same | ~$0.068 |
| Bedrock Sonnet | ~$0.015-0.030 |
| **Total** | **~$0.09-0.10/run** |

**Cost trap**: Standard SFN charges per state transition -- ~20 per run. 100 claims
in rapid succession = 2,000 transitions (~$0.05) + 100 Textract pages ($6.50) = ~$6.55.
Always verify the billing alarm is active before running more than a few claims.

---

## Appendix C -- Troubleshooting

### Bedrock AccessDeniedException

Symptoms: SynthesizeVerdict fails with `AccessDeniedException`, error message mentions
a region other than your deployment region.

Cause: The cross-region inference profile routed the request to a region where the
`SynthesizeVerdictRole` policy does not cover the foundation-model ARN. This happens
when the policy enumerates specific regions instead of using the wildcard.

Fix: Open **IAM** -> **Roles** -> `insurance-claims-ai-pipeline-SynthesizeVerdictRole`.
Verify the foundation-model resource is:
```
arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0
```
The `*` wildcard on region is required. Do not replace it with a region list.

### Textract UnsupportedDocumentException

Cause: The uploaded PDF has more than one page. Textract `AnalyzeDocument` (sync)
is single-page only.

Fix: Use a single-page PDF. The included `samples/approve/police-report.pdf` is single-page.
For multi-page support, see Appendix E, extension E1.

### EventBridge invocation failed

Cause: The EventBridgeRole trust policy uses the wrong service principal.

Fix:
1. Open **IAM** -> **Roles** -> `insurance-claims-ai-pipeline-EventBridgeRole`
2. Click the **Trust relationships** tab
3. Verify the principal is `"Service": "events.amazonaws.com"` -- NOT `scheduler.amazonaws.com`
   or `pipes.amazonaws.com`
4. If wrong, click **Edit trust policy** and replace with the correct JSON shown in S7, Role 6

### EventBridge rule not firing

Check in order:
1. **S3 EventBridge enabled**: intake bucket -> Properties -> Amazon EventBridge -> On
2. **Rule pattern bucket name**: the bucket name in the event pattern must match exactly,
   including the account ID suffix
3. **Object key suffix**: the rule fires on keys ending in `manifest.json` (case sensitive)
4. **Rule state**: EventBridge -> Rules -> confirm the rule is **Enabled**

### Pipeline fails at ValidateArtifacts

Cause: Manifest was uploaded before all artifact files finished uploading, or an
artifact key in the manifest does not match the actual S3 key.

Fix: Follow the two-step upload order -- artifacts first, manifest last. Wait for the
S3 upload dialog to show "Succeeded" for all three artifact files before uploading
`manifest.json`. Check the ManifestError message in SFN execution history for the
specific key that failed HeadObject.

### Lambda function not found in state machine

Cause: Lambda ARN in the ASL does not match the actual function name.

Fix: In the Step Functions state machine definition, verify each Lambda ARN uses the
correct function name and the correct region and account ID:
- `insurance-claims-ai-pipeline-ReadManifest`
- `insurance-claims-ai-pipeline-ReadText`
- `insurance-claims-ai-pipeline-SynthesizeVerdict`
- `insurance-claims-ai-pipeline-WriteArtifacts`

### DynamoDB record not appearing

Check the SFN execution: if WriteArtifacts failed (e.g., IAM permission error), no
DynamoDB record is written. Open the WriteArtifacts state in the execution history
and check the error output. A `dynamodb:PutItem` AccessDeniedException means the
WriteArtifactsRole policy is missing or incorrectly scoped.

---

## Appendix D -- Cleanup verification checklist

After completing S18 teardown, verify:

- [ ] S3: `insurance-claims-ai-pipeline-intake-*` bucket does not appear in bucket list
- [ ] S3: `insurance-claims-ai-pipeline-decisions-*` bucket does not appear in bucket list
- [ ] Lambda: no `insurance-claims-ai-pipeline-*` functions in the function list
- [ ] Step Functions: `insurance-claims-ai-pipeline-ClaimsStateMachine` does not appear
- [ ] DynamoDB: `insurance-claims-ai-pipeline-ClaimsDecisions` table does not appear
- [ ] SNS: `insurance-claims-ai-pipeline-AdjusterNotifications` topic does not appear
- [ ] EventBridge: `insurance-claims-ai-pipeline-IntakeManifestRule` does not appear
- [ ] IAM: no `insurance-claims-ai-pipeline-*` roles in the role list
- [ ] CloudWatch: no `/aws/lambda/insurance-claims-ai-pipeline-*` log groups remain
- [ ] CloudWatch: no `/aws/states/insurance-claims-ai-pipeline-*` log groups remain

---

## Appendix E -- Extension exercises

### E1 -- Multi-page PDFs via async Textract

The sync `AnalyzeDocument` API used in this lab is single-page only. To support
multi-page claim documents:

1. Replace the `AnalyzeDocument` direct SDK integration with a Lambda that calls
   `textract.start_document_analysis()` (async)
2. Use a Step Functions `.waitForTaskToken` pattern: the Lambda starts the Textract
   job and returns the task token to Textract as a callback; Textract calls back when
   analysis is complete, resuming the SFN execution
3. Use `GetDocumentAnalysis` to retrieve the result pages

Key SFN change: the state needs a `HeartbeatSeconds` timeout and the Lambda passes
the `TaskToken` as a Textract notification channel tag.

### E2 -- Bedrock Guardrails service integration

This lab implements guardrails as application logic in the ASL (confidence threshold +
DENY downgrade Choice state). Amazon Bedrock also offers a native Guardrails service
that blocks harmful content, applies topic filters, and redacts PII at the API layer.

To add Bedrock Guardrails:
1. Create a guardrail in the Bedrock console with content filters appropriate for
   insurance claim processing
2. Pass `guardrailIdentifier` and `guardrailVersion` to the `invoke_model` call
   in `SynthesizeVerdict`
3. Handle `GUARDRAIL_INTERVENED` in the response and set `final_status = NEEDS_REVIEW`

The ASL-level guardrail in this lab remains valuable for business logic (confidence
threshold, DENY escalation) even when Bedrock Guardrails handles content filtering.

### E3 -- Human-in-the-loop adjuster approval

Currently the pipeline notifies the adjuster but does not wait for a response.
To implement true human-in-the-loop approval:

1. Add a `WaitForAdjusterApproval` state after `NotifyAdjuster` using `.waitForTaskToken`
2. The SNS message includes the task token and a callback URL
3. The adjuster clicks APPROVE or REJECT in a simple web UI that calls
   `StepFunctions.sendTaskSuccess()` or `StepFunctions.sendTaskFailure()`
4. Set `HeartbeatSeconds: 86400` (24-hour timeout) so unreviewed claims auto-fail

### E4 -- Rules engine + LLM hybrid

A more robust production design uses a layered approach:

1. **Deterministic rules engine** runs first (Lambda or Parallel state):
   - Policy active? Coverage in scope? Claim filed within time limit?
   - Fraud watchlist check (external API call)
   - These produce binary APPROVE/DENY facts, not probabilistic scores
2. **LLM synthesis** runs only for claims that pass the rules engine
   - The LLM receives the structured rules output as additional evidence
   - Its role narrows to: assess evidentiary quality and draft communications
3. **Guardrails** apply only to the LLM layer, not to rules-engine decisions

This hybrid respects the difference between deterministic facts (rules engine) and
probabilistic evidence assessment (LLM), and avoids asking the LLM to make decisions
that should be made by code.

### E5 -- SES domain verification and DKIM

The pipeline already uses SES for claimant email. In the lab you verified a single
email address as the sender identity. For a production deployment, verify a full
sending domain instead:

1. SES console -> **Verified identities** -> **Create identity** -> **Domain**
2. Add the CNAME records SES generates to your DNS provider
3. Wait for verification status to show **Verified** (typically under 72 hours)
4. Update `SenderEmail` CFN parameter to use an address at your verified domain
5. Enable DKIM signing on the domain identity for better deliverability

With a verified domain, any address `@yourdomain.com` can be used as the From address
without individual verification. You can also attach a configuration set to track
bounces, complaints, and delivery events via SNS or CloudWatch.
