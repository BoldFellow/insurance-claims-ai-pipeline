# insurance-claims-ai-pipeline -- Session Context

## Purpose

Full-lab portfolio project for AWS Cloud Architect students. Event-driven AI pipeline:
S3 manifest upload -> EventBridge -> Step Functions Standard workflow -> Rekognition +
Textract + Bedrock Claude Haiku -> guardrails -> DynamoDB + S3 + SNS.

## Stack

- Runtime: Python 3.12 ARM64 Lambda, boto3 from Lambda runtime (no external deps)
- Orchestration: Step Functions Standard, aws-sdk: direct integrations (NO .sync)
- AI: Bedrock cross-region inference profile us.anthropic.claude-haiku-4-5-20251001-v1:0
- CFN: single self-contained template.yaml, us-east-1
- Deterministic bucket names: ${AWS::StackName}-intake-${AWS::AccountId}

## Active Work

Pipeline validated end-to-end and torn down cleanly. guide.md fixed (6 issues). ASL SNS fix applied. Ready to push.

Changes since last session:
- CLM-001 validated: NEEDS_REVIEW, confidence 0.65, 5 red flags (synthetic PDF marker)
- CLM-002 validated: NEEDS_REVIEW, confidence 0.25, 9 red flags (guardrail fired correctly)
- Teardown complete: stack DELETE_COMPLETE, DynamoDB deleted, KMS in PENDING_DELETION
- DeletionPolicy on DynamoDB changed from Retain to Delete in cfn/template.yaml
- guide.md: 6 fixes applied (S2 rewrite, S3 model ID fix, validate-template limit note,
  S7 CLM-001 expected result note, S12 decisions bucket versioned delete, S9 S2 ref removed)
- Memory saved: CFN always at end as shortcut

Files:
- cfn/template.yaml -- all 7 Lambdas inline (Handler: index.handler), no ArtifactBucket param
- app/lambdas/*/handler.py -- reference copies (canonical code is in cfn/template.yaml ZipFile)
- app/sfn/state_machine.asl.json
- samples/{manifest.json,photo-damage.jpg,police-report.pdf,statement.txt,red-flag/,README.md}
- guide.md, README.md, LICENSE, architecture.drawio, architecture.png

Deleted: scripts/ directory, app/lambdas/*/requirements.txt, build/ directory

## Key Decisions

2026-05-27: No .sync on aws-sdk: integrations -- they are synchronous request/response;
  .sync only applies to legacy optimized integrations (ecs:runTask.sync etc.)

2026-05-27: normalize_evidence uses zip(artifacts, artifact_results) not result.get("key")
  because Rekognition/Textract ResultSelectors return only service output, not input artifact
  metadata. Map state preserves output order so positional correlation is safe.

2026-05-27: Bedrock IAM grants bedrock:InvokeModel on both inference-profile ARN AND
  foundation-model ARNs in us-east-1 / us-east-2 / us-west-2 -- cross-region profiles
  fan out and missing perms in any spanned region throws AccessDeniedException at runtime.

2026-05-27: Deterministic bucket names (stack-name + account-id suffix) to avoid CFN
  circular dependency between IntakeBucket, StateMachineRole, and IntakeManifestRule.

2026-05-27: DynamoDB DeletionPolicy changed to Delete -- simplifies teardown and
  redeploy for a lab context. Retain is called out in S11 as the production pattern.

2026-05-27: LLM guardrail framing: "low-trust probabilistic signals require human
  escalation" NOT "LLMs cannot deny". Real adjudication auto-denies at the rules-engine
  layer; this guardrail reflects signal trust profile, not action severity.

2026-05-27: All Lambda code embedded inline via Code.ZipFile; Handler must be index.handler
  (ZipFile always creates index.py). No ArtifactBucket parameter; deploy uses
  aws cloudformation deploy --s3-bucket for template-only upload.

2026-05-27: SNS NotifyAdjuster sends full adjuster_email_text (from WriteArtifacts return
  value) as Message body -- not an S3 path. Files still written to S3 for audit trail.
  cfn/template.yaml was already correct; app/sfn/state_machine.asl.json fixed 2026-05-28.

## Session Notes

2026-05-27: Full project built, CFN validated, ASCII-clean, architecture PNG exported.
2026-05-27: Pipeline validated end-to-end. SNS sends full email text. Scripts eliminated.
2026-05-27: guide.md 6 fixes applied after full lab walkthrough. DynamoDB DeletionPolicy changed to Delete.
2026-05-28: ASL reference copy fixed -- NotifyAdjuster now sends adjuster_email_text (was States.Format short string). cfn/template.yaml was already correct.
