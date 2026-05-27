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
- AWS CLI v2, Python 3.10+
- An S3 bucket for Lambda deployment artifacts

See `guide.md S0` for full prerequisites and cost guardrails.

## Quick start

```bash
# Generate sample files
python3 scripts/generate-samples.py

# Deploy (creates stack + uploads Lambda zips)
./scripts/deploy.sh \
  --stack-name insurance-claims-ai-pipeline \
  --artifact-bucket your-deploy-bucket \
  --adjuster-email you@example.com

# Seed a sample claim (triggers pipeline)
./scripts/seed-sample-claim.sh acme-corp CLM-001

# Trigger the guardrail demo
./scripts/seed-sample-claim.sh acme-corp CLM-002 --red-flag

# Teardown
./scripts/teardown.sh
```

## Repository layout

```
cfn/
  template.yaml              single self-contained CloudFormation stack
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
scripts/
  deploy.sh
  teardown.sh
  seed-sample-claim.sh
  check-bedrock-access.sh
  generate-samples.py
  samples/                   synthetic claim artifacts (normal + red-flag)
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
