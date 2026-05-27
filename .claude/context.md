# insurance-claims-ai-pipeline -- Session Context

## Purpose

Full-lab portfolio project for AWS Cloud Architect students. Event-driven AI pipeline:
S3 manifest upload -> EventBridge -> Step Functions Standard workflow -> Rekognition +
Textract + Bedrock Claude Haiku -> guardrails -> DynamoDB + S3 + SNS.

## Stack

- Runtime: Python 3.12 ARM64 Lambda, boto3 pinned >=1.38.0
- Orchestration: Step Functions Standard, aws-sdk: direct integrations (NO .sync)
- AI: Bedrock cross-region inference profile us.anthropic.claude-haiku-4-5-20251001-v1:0
- CFN: single self-contained template.yaml, us-east-1
- Deterministic bucket names: ${AWS::StackName}-intake-${AWS::AccountId}

## Active Work

All files written. Repo ready to git init and push to GitHub.

Files complete:
- cfn/template.yaml (CFN-validated, ASCII-clean)
- app/sfn/state_machine.asl.json
- app/lambdas/{read_manifest,validate_artifacts_present,read_text,normalize_evidence,
  synthesize_verdict,apply_guardrails,write_artifacts}/handler.py + requirements.txt
- scripts/{deploy.sh,teardown.sh,seed-sample-claim.sh,check-bedrock-access.sh,generate-samples.py}
- scripts/samples/{manifest.json,statement.txt,README.md} + red-flag/ variants
- guide.md, README.md, LICENSE, architecture.drawio, architecture.png

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

2026-05-27: DynamoDB DeletionPolicy: Retain -- protects audit records from accidental
  stack deletion; teardown.sh explicitly notes the table persists.

2026-05-27: LLM guardrail framing: "low-trust probabilistic signals require human
  escalation" NOT "LLMs cannot deny". Real adjudication auto-denies at the rules-engine
  layer; this guardrail reflects signal trust profile, not action severity.

## Session Notes

2026-05-27: Full project built in two context windows; all files written, CFN validated,
  ASCII-clean, architecture PNG exported. Ready for git init and GitHub push.
