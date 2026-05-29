"""
SynthesizeVerdict: calls Bedrock Claude to synthesize an adjudication
recommendation from the evidence assembled by ProcessArtifacts + BuildEvidence.

Input: evidence.artifacts = [{artifact: {type, key, intake_bucket}, analysis: {...}}, ...]
Output: passes through input + llm_verdict dict with new dual-output schema.

IMPORTANT: This Lambda is NOT the decision-maker. It produces a draft
recommendation reviewed by the ASL GuardrailCheck state.
"""
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
            key_map = {}
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
                        key_map[block["Id"]] = key_text
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
            sentiment = analysis.get("sentiment", "")
            key_phrases = analysis.get("key_phrases", [])
            phrases_str = ", ".join(key_phrases) if key_phrases else "none"
            parts.append(
                f"### Written Statement: {akey}\n"
                f"Comprehend sentiment: {sentiment}\n"
                f"Key phrases: {phrases_str}\n\n"
                f"{content[:2000]}\n\n"
            )

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
