# insurance-claims-ai-pipeline -- Session Context

## Purpose

Full-lab portfolio project for AWS Cloud Architect students. Event-driven AI pipeline:
S3 manifest upload -> EventBridge -> Step Functions Standard workflow -> Rekognition +
Textract + Bedrock Claude Haiku -> guardrails -> DynamoDB + S3 + SNS.

## Stack

- Runtime: Python 3.12 ARM64 Lambda, boto3 from Lambda runtime (no external deps)
- Orchestration: Step Functions Standard, aws-sdk: direct integrations (NO .sync)
- AI: Bedrock cross-region inference profile + Rekognition + Textract + Comprehend (sentiment + key phrases)
- CFN: single self-contained template.yaml, region-flexible (<REGION> placeholder in guide)
- Deterministic bucket names: ${AWS::StackName}-intake-${AWS::AccountId}

## Active Work

Major refactor complete (2026-05-29): 7 Lambdas -> 4 Lambdas, KMS removed entirely,
dual SNS topics, new ASL (ValidateArtifacts Map + GuardrailCheck Choice + RouteDecision
Choice), dual output (client_letter.txt + adjuster_brief.md), S3 versioning removed.

Files changed in this refactor:
- cfn/template.yaml: removed KMS, 3 Lambdas removed (ValidateArtifactsPresent,
  NormalizeEvidence, ApplyGuardrails), added ClaimantNotificationsTopic, removed
  S3 versioning from both buckets, StateMachineRole updated (HeadObject added, dual
  SNS Publish, no KMS), 6 IAM roles total (was 9)
- app/sfn/state_machine.asl.json: full rewrite -- ValidateArtifacts Map (HeadObject),
  ProcessArtifacts Map (ResultPath:$.analysis per branch), BuildEvidence Pass,
  GuardrailCheck Choice, ForceNeedsReview/PassThroughVerdict Pass, RouteDecision 3-way,
  dual SNS notify states, RecordFailure is now a clean Fail state (no DynamoDB)
- app/lambdas/synthesize_verdict/handler.py: dual-output schema, new SYSTEM_PROMPT
  with strict client_letter rules, consumes flat evidence.artifacts array
- app/lambdas/write_artifacts/handler.py: writes 4 files (decision.json,
  client_letter.txt, adjuster_brief.md, evidence_bundle.json), updated DynamoDB attrs,
  returns client_letter + adjuster_brief for downstream SNS states
- guide.md: complete rewrite -- 18 sections + appendices A-E, KMS section deleted,
  region-flexible (<REGION> placeholder), 6 IAM roles documented, new ASL walkthrough,
  new Lambda code, dual SNS, simple S3 teardown (no versioning)
- samples/README.md: removed "Marked Synthetic -- for demonstration only" reference

Deleted Lambda directories:
- app/lambdas/validate_artifacts_present/
- app/lambdas/normalize_evidence/
- app/lambdas/apply_guardrails/

Completed:
- Architecture diagram updated (architecture.drawio + architecture.png): SFN swimlane
  container with internal states, ClaimantNotificationsTopic added, 3 removed Lambda
  nodes dropped.
- 4-Lambda design validated end-to-end: happy path (APPROVE, confidence 0.92),
  red-flag path (NEEDS_REVIEW via GuardrailCheck ForceNeedsReview), teardown clean.
- Template fix: ${ConfidenceThreshold} DefinitionSubstitution replaced with hardcoded
  0.85 in ASL -- EarlyValidation hook rejects unquoted numeric placeholders in
  DefinitionString JSON before substitutions are resolved.
- Root cause of repeated EarlyValidation failures: orphaned resources (Lambda, SFN,
  DynamoDB, SNS, EventBridge) from prior deploy blocked CREATE with name conflicts.
  Added teardown note to Key Decisions.

## Key Decisions

2026-05-27: No .sync on aws-sdk: integrations -- they are synchronous request/response;
  .sync only applies to legacy optimized integrations (ecs:runTask.sync etc.)

2026-05-27: normalize_evidence uses zip(artifacts, artifact_results) not result.get("key")
  because Rekognition/Textract ResultSelectors return only service output, not input artifact
  metadata. Map state preserves output order so positional correlation is safe.

2026-05-27: Bedrock IAM grants bedrock:InvokeModel on both inference-profile ARN AND
  foundation-model ARNs -- cross-region profiles fan out to multiple regions at runtime.

2026-05-27: Deterministic bucket names (stack-name + account-id suffix) to avoid CFN
  circular dependency between IntakeBucket, StateMachineRole, and IntakeManifestRule.

2026-05-27: DynamoDB DeletionPolicy changed to Delete -- simplifies teardown and
  redeploy for a lab context.

2026-05-27: All Lambda code embedded inline via Code.ZipFile. Template > 51KB requires
  --s3-bucket parameter for cloudformation deploy (documented in Appendix A).

2026-05-29: Wildcard region in foundation-model IAM ARN required for cross-region
  inference profiles. EU profile routed to eu-north-1 and eu-south-1 at runtime;
  enumerating regions in IAM policy fails. Use:
  arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0

2026-05-29: S3 versioning removed from both buckets -- versioning adds no demo value
  and requires s3api list-object-versions + delete-objects purge before CFN teardown
  (learned from prior teardown failure). Default SSE-S3 encryption retained.

2026-05-29: KMS removed entirely -- SSE-S3 sufficient for lab, KMS pending-deletion
  window (7-30 days) blocked previous teardown and re-deploy attempts.

2026-05-29: RecordFailure is now a clean Fail state only -- no DynamoDB write for
  errors; SFN execution history + CloudWatch Logs are the failure surface.

2026-05-29: DefinitionSubstitutions cannot carry numeric placeholders -- the CFN
  EarlyValidation hook parses the raw DefinitionString JSON before substitutions
  are resolved; ${ConfidenceThreshold} as a NumericLessThan value is invalid JSON
  at parse time. Hardcode numeric thresholds directly in the DefinitionString.

2026-05-29: Teardown must clear all named CFN resources before re-deploy. Named
  resources (Lambda, SFN, DynamoDB table, SNS topics, EventBridge rules, log groups)
  with explicit Name/FunctionName/StateMachineName properties cause EarlyValidation
  ResourceExistenceCheck failures if left orphaned. Always empty buckets first, then
  delete-stack, then verify no leftovers before next deploy.

2026-05-29: GuardrailCheck now checks only confidence (< 0.85 -> NEEDS_REVIEW). Original
  design also intercepted DENY, making RouteDecision DENY branch unreachable (dead code).
  Fixed so high-confidence DENY passes through to NotifyClaimantDenied.

2026-05-29: Comprehend detect_sentiment + detect_key_phrases added to ReadText Lambda.
  Results (sentiment, sentiment_scores, key_phrases) passed to SynthesizeVerdict as
  additional context in the Bedrock prompt. Comprehend requires Resource: * in IAM --
  no resource-level policy support for detect_* actions.

2026-05-29: CFN validate-template --template-body fails at >51KB (API limit). Fix: stage
  to S3, use --template-url. Console does this transparently. Template validated clean.

2026-05-29: samples/deny/ added (CLM-003): bicycle claimed under auto policy with car
  photo. Exercises RouteDecision DENY branch. All 3 branches now have sample sets:
  CLM-001 (APPROVE), CLM-002 (NEEDS_REVIEW), CLM-003 (DENY).

2026-05-29: Dual SNS topics -- ClaimantNotificationsTopic (APPROVE + DENY),
  AdjusterNotificationsTopic (NEEDS_REVIEW). To receive all 3 email types in testing,
  set both ClaimantEmail and AdjusterEmail CFN parameters to the same address.

2026-05-29: claimant_name added to Bedrock verdict schema. Bedrock extracts the name
  from evidence text; write_artifacts uses it as salutation (fallback: "Dear Claimant,").
  temperature=0 added to Bedrock call for deterministic output across runs.

2026-05-29: ASL ResultSelector bug: ReadText state only forwarded type/key/content,
  silently dropping sentiment/sentiment_scores/key_phrases. Fixed to forward all 6 fields.

2026-05-29: CLM-003 deny statement rewritten -- claimant now explicitly acknowledges
  bicycle is not a motor vehicle, photo is wrong asset, no police report, requesting
  goodwill exception. Bedrock returns DENY at 0.95 confidence deterministically.

2026-05-29: ClaimantNotifications SNS replaced with SES v2 (sesv2:sendEmail direct SDK
  integration). SNS email lacks From/To addressing and delivery metrics. SES is the
  correct tool for claimant-facing email. SenderEmail (From) and ClaimantEmail (To) are
  required CFN parameters; both must be SES-verified if account is in sandbox mode.
  IAM permission: ses:SendEmail scoped to arn:aws:ses:<region>:<account>:identity/<sender>.

2026-05-29: NEEDS_REVIEW token optimization -- Bedrock no longer generates client_letter
  for NEEDS_REVIEW (rule added to SYSTEM_PROMPT: set client_letter to empty string).
  AdjusterNotifications SNS body trimmed to summary + reasoning + red_flags + S3 link
  via new _format_adjuster_notification helper. Full adjuster_brief.md (with raw Bedrock
  JSON) still written to S3 only. Verified end-to-end: CLM-002 SNS body is trimmed,
  S3 adjuster_brief.md remains complete.

2026-05-29: Two bugs found during deploy+test:
  (1) CFN inline Lambda code used def handler() but Handler property was index.handler;
  source files used def lambda_handler(). Fixed: all 4 CFN ZipFile blocks renamed to
  lambda_handler, Handler property updated to index.lambda_handler in all 4 Lambda
  resources and source files are now canonical. Direct Lambda update required (not CFN
  redeploy) since template exceeded 51KB and Lambda code update is faster.
  (2) DENY client_letter was empty -- SYSTEM_PROMPT had no explicit DENY rule, so Bedrock
  inferred "don't include reasons -> write nothing". Fixed: added explicit DENY rule to
  SYSTEM_PROMPT ("write 1-2 sentences acknowledging denial; do NOT set to empty string").
  Added fallback in write_artifacts._format_client_letter for robustness.
  Both fixes applied to source files AND CFN template inline code.

2026-05-29: Deployed and tested end-to-end:
  CLM-001 (APPROVE): confidence 0.92, all 4 S3 files, DDB record, SES email sent.
  CLM-003 (DENY): confidence 0.95, full client_letter with intro + reasons + appeal,
  SES email sent to ilyawv@gmail.com. Both paths SUCCEEDED.

## Session Notes

2026-05-29: CFN validated via S3; diagram updated (Comprehend icon, corrected labels).
2026-05-29: samples/approve/ subfolder created; CLM-001 files moved out of samples/ root.
2026-05-29: ClaimantNotifications SNS replaced with SES v2 sendEmail. SenderEmail + ClaimantEmail required CFN parameters.
2026-05-29: Deployed stack, tested APPROVE+DENY end-to-end. Fixed handler name mismatch (handler->lambda_handler) and empty DENY client_letter (SYSTEM_PROMPT + fallback). Both paths SUCCEEDED with SES email delivery.
2026-05-29: Full guide.md review complete. Fixed 9 stale references: Comprehend in ReadText code listing, ReadTextRole IAM (missing comprehend:Detect*), ASL ReadText ResultSelector (missing 3 Comprehend fields), GuardrailCheck in overview/paste block/teaching notes/S12 (stale DENY intercept), SynthesizeVerdict text branch, cost table (SES/SNS), E2 (stale DENY escalation mention).
