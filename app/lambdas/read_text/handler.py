import boto3

s3 = boto3.client("s3")

MAX_TEXT_BYTES = 100_000


def handler(event, context):
    # Input from Map state ItemSelector:
    # {"type": "text", "key": "clients/...", "intake_bucket": "..."}
    artifact = event["artifact"]
    intake_bucket = artifact["intake_bucket"]
    key = artifact["key"]

    response = s3.get_object(Bucket=intake_bucket, Key=key)
    body = response["Body"].read(MAX_TEXT_BYTES + 1)

    if len(body) > MAX_TEXT_BYTES:
        body = body[:MAX_TEXT_BYTES]

    try:
        content = body.decode("utf-8")
    except UnicodeDecodeError:
        content = body.decode("utf-8", errors="replace")

    return {
        "type": "text",
        "key": key,
        "content": content,
    }
