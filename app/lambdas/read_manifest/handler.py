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


def handler(event, context):
    # Input: EventBridge S3 "Object Created" event for the manifest.json file.
    # The state machine starts with this event as its input.
    bucket = event["detail"]["bucket"]["name"]
    key = event["detail"]["object"]["key"]

    # Read the manifest from S3
    response = s3.get_object(Bucket=bucket, Key=key)
    raw = response["Body"].read()
    if len(raw) > 100_000:
        raise ManifestError("manifest.json exceeds 100 KB size limit")

    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ManifestError(f"manifest.json is not valid JSON: {e}")

    _validate(manifest, bucket, key)

    # Embed intake_bucket into each artifact so the Map state has it inline
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
                f"Note: Textract sync AnalyzeDocument requires single-page PDF or PNG. "
                f"Allowed: {sorted(DOC_EXTS)}"
            )
