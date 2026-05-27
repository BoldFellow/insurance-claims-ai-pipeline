import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")


class MissingArtifactError(Exception):
    pass


def handler(event, context):
    # Input: output from ReadManifest (manifest contents).
    # HeadObject each declared artifact. Fail fast if any are missing.
    # This guards against the manifest-last convention's non-atomicity:
    # a client might drop the manifest before all uploads complete.
    intake_bucket = event["intake_bucket"]
    artifacts = event["artifacts"]
    missing = []

    for artifact in artifacts:
        key = artifact["key"]
        try:
            s3.head_object(Bucket=intake_bucket, Key=key)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("404", "NoSuchKey"):
                missing.append(key)
            else:
                raise

    if missing:
        raise MissingArtifactError(
            f"Manifest lists {len(missing)} artifact(s) not yet present in S3. "
            f"Missing keys: {missing}. "
            f"Ensure all artifacts are uploaded before dropping manifest.json."
        )

    # Pass state data through unchanged
    return event
