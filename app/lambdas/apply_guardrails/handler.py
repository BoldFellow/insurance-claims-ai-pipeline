"""
ApplyGuardrails: enforces signal-trust rules on the Bedrock recommendation.

This is NOT "LLMs cannot deny claims". Real adjudication systems do auto-deny
at the deterministic rules-engine layer (policy lapsed, excluded peril, fraud
list match). This guardrail reflects a different concern: probabilistic LLM
outputs have a lower trust profile than deterministic rules. A low-confidence
or DENY signal from an LLM should escalate to a human, not terminate a claim.

Rules:
  - confidence < THRESHOLD  -> force NEEDS_REVIEW
  - recommendation == DENY  -> force NEEDS_REVIEW
  - APPROVE with low confidence -> force NEEDS_REVIEW (do not auto-approve either)

The original LLM output is preserved in the output for audit.
"""
import os

CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.85"))


def handler(event, context):
    llm_verdict = event["llm_verdict"]
    recommendation = llm_verdict["recommendation"]
    confidence = float(llm_verdict["confidence"])

    original_recommendation = recommendation
    override_reason = None

    if recommendation == "DENY":
        override_reason = (
            "LLM-derived DENY signals require human review. "
            "Deterministic denial (policy lapse, fraud list) must be handled "
            "by a rules engine, not an LLM recommendation alone."
        )
        recommendation = "NEEDS_REVIEW"

    elif confidence < CONFIDENCE_THRESHOLD:
        override_reason = (
            f"LLM confidence {confidence:.2f} is below threshold {CONFIDENCE_THRESHOLD:.2f}. "
            "Low-confidence signals escalate to human review."
        )
        recommendation = "NEEDS_REVIEW"

    return {
        "client_id": event["client_id"],
        "claim_id": event["claim_id"],
        "submitted_at": event.get("submitted_at", ""),
        "decisions_bucket": event["decisions_bucket"],
        "evidence": event["evidence"],
        "llm_verdict": llm_verdict,
        "final_status": recommendation,
        "original_recommendation": original_recommendation,
        "confidence": confidence,
        "override_reason": override_reason,
        "reasoning": llm_verdict.get("reasoning", ""),
        "red_flags": llm_verdict.get("red_flags", []),
        "draft_email": llm_verdict.get("draft_email", ""),
        "requires_adjuster_review": recommendation == "NEEDS_REVIEW",
    }
