"""
WriteArtifacts: persists the claim decision to S3 (four files) and DynamoDB.

S3 output: s3://{decisions_bucket}/clients/{client_id}/{claim_id}/
    decision.json        - structured decision record
    client_letter.txt    - plain text, claimant-safe (no red flags, no AI references)
    adjuster_brief.md    - markdown, internal-only (reasoning, red flags, raw Bedrock output)
    evidence_bundle.json - full evidence + original LLM output for audit

DynamoDB record:
    PK: client_id  SK: claim_id
    Adds: override_reason, denial_reasons, approval_rationale
    Drops: draft_email (replaced by client_letter.txt in S3)

Return value carries client_letter and adjuster_brief text so downstream
SNS Publish states can use them without re-reading S3.
"""
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
            "client_id":              {"S": client_id},
            "claim_id":               {"S": claim_id},
            "submitted_at":           {"S": event.get("submitted_at", "")},
            "processed_at":           {"S": created_at},
            "final_status":           {"S": final_status},
            "original_recommendation": {"S": llm_verdict["recommendation"]},
            "confidence":             {"N": str(llm_verdict["confidence"])},
            "override_reason":        {"S": override_reason or ""},
            "reasoning":              {"S": llm_verdict.get("reasoning", "")[:1000]},
            "approval_rationale":     {"S": llm_verdict.get("approval_rationale", "")},
            "denial_reasons":         {"S": json.dumps(llm_verdict.get("denial_reasons", []))},
            "red_flags":              {"S": json.dumps(llm_verdict.get("red_flags", []))},
            "decision_s3_prefix":     {"S": f"s3://{decisions_bucket}/{prefix}/"},
            "ttl":                    {"N": str(ttl)},
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
        return f"{salutation}\n\n{_NEEDS_REVIEW_LETTER}\n\nYour claim reference: {claim_id}{_CLAIMANT_CLOSE}\n"


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
