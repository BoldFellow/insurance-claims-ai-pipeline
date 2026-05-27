"""
SynthesizeVerdict: calls Bedrock Claude to synthesize an adjudication
recommendation from the normalized evidence.

IMPORTANT: This Lambda is NOT the decision-maker. It produces a draft
recommendation for a human adjuster. All outputs are reviewed by
ApplyGuardrails before being finalized.
"""
import json
import os

import boto3
from botocore.config import Config

MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
REGION = os.environ.get("AWS_REGION", "us-east-1")

# boto3 retries are disabled here; ASL Retry block owns retry logic.
# read_timeout is set below the Lambda timeout (120s) to avoid SDK hanging.
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
  "recommendation": "APPROVE" | "DENY" | "NEEDS_REVIEW",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<concise explanation of the recommendation, 2-4 sentences>",
  "red_flags": ["<list of concerns or anomalies found, empty list if none>"],
  "draft_email": "<professional draft email from adjuster to claimant, 3-5 sentences>"
}

Guidelines:
- APPROVE: evidence is consistent, no red flags, straightforward claim
- DENY: clear evidence of fraud, excluded peril, or policy violation (rare - when in doubt use NEEDS_REVIEW)
- NEEDS_REVIEW: ambiguous evidence, conflicting signals, or anything requiring specialist judgment
- confidence: how certain you are (0.0 = no idea, 1.0 = completely certain)
- draft_email: written as if from the adjuster to the claimant (professional, empathetic)

Output ONLY the JSON object. No preamble, no explanation outside the JSON."""


def handler(event, context):
    # Input: output from NormalizeEvidence
    client_id = event["client_id"]
    claim_id = event["claim_id"]
    evidence = event["evidence"]

    user_message = _build_evidence_message(client_id, claim_id, evidence)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
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

    # Parse the JSON verdict
    try:
        verdict = json.loads(llm_text)
    except json.JSONDecodeError:
        # Try to extract JSON block if model included any surrounding text
        import re
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

    if evidence.get("images"):
        parts.append("### Image Analysis (Rekognition)\n")
        for img in evidence["images"]:
            labels = ", ".join(
                f"{l['name']} ({l['confidence']:.0f}%)" for l in img["labels"]
            )
            parts.append(f"File: {img['key']}\nDetected: {labels}\n\n")

    if evidence.get("documents"):
        parts.append("### Document Analysis (Textract)\n")
        for doc in evidence["documents"]:
            parts.append(f"File: {doc['key']}\n")
            if doc["form_fields"]:
                fields = "; ".join(
                    f"{f['key']}: {f['value']}" for f in doc["form_fields"][:20]
                )
                parts.append(f"Form fields: {fields}\n")
            if doc["extracted_text"]:
                parts.append(f"Extracted text:\n{doc['extracted_text'][:2000]}\n\n")

    if evidence.get("texts"):
        parts.append("### Written Statements\n")
        for txt in evidence["texts"]:
            parts.append(f"File: {txt['key']}\n{txt['content'][:2000]}\n\n")

    parts.append(
        "\nBased on this evidence, provide your adjudication recommendation as a JSON object."
    )
    return "".join(parts)


def _validate_verdict(verdict):
    required = ["recommendation", "confidence", "reasoning", "red_flags", "draft_email"]
    for field in required:
        if field not in verdict:
            raise ValueError(f"Bedrock verdict missing required field: {field}")

    if verdict["recommendation"] not in ("APPROVE", "DENY", "NEEDS_REVIEW"):
        raise ValueError(f"Invalid recommendation: {verdict['recommendation']}")

    conf = verdict["confidence"]
    if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
        raise ValueError(f"confidence must be a float 0.0-1.0, got: {conf}")
