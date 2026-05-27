# insurance-claims-ai-pipeline

Event-driven insurance claim processing pipeline using AWS Step Functions, Rekognition,
Textract, and Bedrock. Designed as a full-lab portfolio project for AWS Cloud Architect
students.

## What it does

An S3 manifest upload triggers a Step Functions Standard workflow that:

1. Reads and validates the claim manifest
2. Fans out per-artifact in parallel (Rekognition for images, Textract for documents,
   Lambda reader for text files)
3. Calls Bedrock Claude Haiku to synthesize a recommendation with confidence score,
   reasoning, and a draft adjuster email
4. Applies deterministic guardrails: low-confidence or DENY signals are downgraded
   to NEEDS_REVIEW for human adjuster review
5. Persists decision artifacts to S3 and DynamoDB
6. Publishes an SNS notification when human review is required

## Architecture

```
S3 intake bucket  ->  EventBridge rule  ->  Step Functions Standard Workflow
                                                    |
                         +---------------------------------------------+
                         |  ReadManifest + ValidateArtifactsPresent     |
                         |  ProcessArtifacts (Map, parallel fan-out)    |
                         |    image  -> Rekognition DetectLabels         |
                         |    doc    -> Textract AnalyzeDocument         |
                         |    text   -> ReadText Lambda                  |
                         |  NormalizeEvidence -> SynthesizeVerdict       |
                         |    -> Bedrock Claude Haiku 4.5                |
                         |  ApplyGuardrails -> WriteArtifacts            |
                         |  ShouldNotifyAdjuster -> SNS (if needed)      |
                         +---------------------------------------------+
                                    |
              S3 decisions bucket + DynamoDB ClaimsDecisions
```

## Prerequisites

- AWS account, us-east-1, with Bedrock model access granted for Claude Haiku 4.5
- AWS CLI v2

See `guide.md S0` for full prerequisites and cost guardrails.

## Quick start

All Lambda code is embedded inline in `cfn/template.yaml`. No build step required.

```bash
# Create a staging bucket for the CFN template (> 51 KB inline limit)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws s3 mb s3://cfn-templates-${ACCOUNT} --region us-east-1

# Deploy the stack
aws cloudformation deploy \
  --template-file cfn/template.yaml \
  --stack-name insurance-claims-ai-pipeline \
  --s3-bucket cfn-templates-${ACCOUNT} \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1 \
  --parameter-overrides \
      BedrockModelId=us.anthropic.claude-haiku-4-5-20251001-v1:0 \
      AdjusterEmail=you@example.com

# Seed a sample claim (artifacts first, manifest last)
INTAKE=$(aws cloudformation describe-stacks \
  --stack-name insurance-claims-ai-pipeline \
  --query 'Stacks[0].Outputs[?OutputKey==`IntakeBucketName`].OutputValue' \
  --output text)

aws s3 cp samples/photo-damage.jpg  s3://${INTAKE}/clients/acme-corp/CLM-001/photo-damage.jpg
aws s3 cp samples/police-report.pdf s3://${INTAKE}/clients/acme-corp/CLM-001/police-report.pdf
aws s3 cp samples/statement.txt     s3://${INTAKE}/clients/acme-corp/CLM-001/statement.txt
aws s3 cp samples/manifest.json     s3://${INTAKE}/clients/acme-corp/CLM-001/manifest.json
```

See `guide.md S5` for the red-flag guardrail demo and `guide.md S12` for teardown.

## Repository layout

```
cfn/
  template.yaml              single self-contained CloudFormation stack
                             (all Lambda code embedded inline via Code.ZipFile)
app/
  lambdas/
    read_manifest/           validates S3 EventBridge event + manifest schema
    validate_artifacts_present/ HeadObject on every declared artifact
    read_text/               reads text artifacts from S3
    normalize_evidence/      shapes Map output into Bedrock evidence dict
    synthesize_verdict/      calls Bedrock Claude, parses structured JSON output
    apply_guardrails/        DENY + low-confidence -> NEEDS_REVIEW escalation
    write_artifacts/         writes decision.json, adjuster_email.md, DynamoDB
  sfn/
    state_machine.asl.json   Step Functions ASL definition
samples/
  manifest.json              sample claim manifest (normal path)
  photo-damage.jpg           JPEG placeholder for Rekognition
  police-report.pdf          single-page synthetic incident report
  statement.txt              claimant statement
  red-flag/                  alternate artifacts that trigger the guardrail
  README.md                  sample file provenance and license notes
guide.md                     full lab guide S0-S12 + Appendices A-D
architecture.drawio
architecture.png
```

## Estimated cost per run

~$0.07 (dominated by Textract ~$0.065/page). See guide.md Appendix A for full
breakdown. Set a billing alarm before seeding.

## Lab guide

See [guide.md](guide.md) for step-by-step instructions, architecture walkthrough,
security analysis, observability, and extension exercises.

## License

MIT -- see LICENSE
