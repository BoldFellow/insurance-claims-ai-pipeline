import boto3

s3 = boto3.client("s3")
comprehend = boto3.client("comprehend")

MAX_TEXT_BYTES = 100_000
MAX_COMPREHEND_BYTES = 5_000


def lambda_handler(event, context):
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

    # Comprehend has a 5000-byte limit per call; truncate if needed
    comprehend_input = content[:MAX_COMPREHEND_BYTES]

    sentiment_response = comprehend.detect_sentiment(
        Text=comprehend_input,
        LanguageCode="en",
    )

    phrases_response = comprehend.detect_key_phrases(
        Text=comprehend_input,
        LanguageCode="en",
    )

    top_phrases = [
        p["Text"]
        for p in sorted(phrases_response["KeyPhrases"], key=lambda x: x["Score"], reverse=True)[:10]
    ]

    return {
        "type": "text",
        "key": key,
        "content": content,
        "sentiment": sentiment_response["Sentiment"],
        "sentiment_scores": sentiment_response["SentimentScore"],
        "key_phrases": top_phrases,
    }
