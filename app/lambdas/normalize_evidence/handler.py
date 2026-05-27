"""
NormalizeEvidence: receives the Map state output (array of per-artifact results
from Rekognition, Textract, and ReadText) and shapes them into a single evidence
summary dict passed to SynthesizeVerdict.
"""


def handler(event, context):
    # event["artifact_results"] is the array output from the Map state.
    # Each item has the shape returned by the per-artifact branch.
    # Use original artifacts array for keys/types -- Rekognition and Textract
    # ResultSelectors return only service output fields (Labels, Blocks), not the
    # input artifact metadata. Map state preserves output order matching input order,
    # so zip is safe for index-based correlation.
    artifacts = event.get("artifacts", [])
    artifact_results = event.get("artifact_results", [])

    images = []
    documents = []
    texts = []

    for artifact, result in zip(artifacts, artifact_results):
        atype = artifact["type"]
        akey = artifact["key"]

        if atype == "image":
            labels = _extract_rekognition_labels(result)
            images.append({"key": akey, "labels": labels})

        elif atype == "document":
            text, forms, tables = _extract_textract_content(result)
            documents.append({
                "key": akey,
                "extracted_text": text[:4000],
                "form_fields": forms[:50],
                "tables": tables[:5],
            })

        elif atype == "text":
            texts.append({
                "key": akey,
                "content": result.get("content", "")[:4000],
            })

    return {
        "client_id": event["client_id"],
        "claim_id": event["claim_id"],
        "submitted_at": event.get("submitted_at", ""),
        "decisions_bucket": event["decisions_bucket"],
        "evidence": {
            "images": images,
            "documents": documents,
            "texts": texts,
        },
    }


def _extract_rekognition_labels(result):
    # Rekognition DetectLabels SDK integration returns Labels array directly
    labels_raw = result.get("Labels", [])
    return [
        {
            "name": lbl.get("Name", ""),
            "confidence": round(lbl.get("Confidence", 0), 2),
        }
        for lbl in labels_raw[:30]  # top 30 labels
    ]


def _extract_textract_content(result):
    # Textract AnalyzeDocument SDK integration returns Blocks array
    blocks = result.get("Blocks", [])

    lines = []
    form_fields = []
    tables = []

    key_map = {}   # blockId -> key text
    value_map = {} # keyId -> value blockId

    for block in blocks:
        btype = block.get("BlockType", "")

        if btype == "LINE":
            lines.append(block.get("Text", ""))

        elif btype == "KEY_VALUE_SET":
            entity = block.get("EntityTypes", [])
            if "KEY" in entity:
                key_text = _get_child_text(block, blocks)
                # find the VALUE relationship
                for rel in block.get("Relationships", []):
                    if rel["Type"] == "VALUE":
                        for vid in rel["Ids"]:
                            value_map[vid] = key_text
                key_map[block["Id"]] = key_text
            elif "VALUE" in entity:
                val_text = _get_child_text(block, blocks)
                key_text = value_map.get(block["Id"], "")
                if key_text:
                    form_fields.append({"key": key_text, "value": val_text})

    extracted_text = "\n".join(lines)
    return extracted_text, form_fields, tables


def _get_child_text(block, all_blocks):
    block_index = {b["Id"]: b for b in all_blocks}
    words = []
    for rel in block.get("Relationships", []):
        if rel["Type"] == "CHILD":
            for cid in rel["Ids"]:
                child = block_index.get(cid, {})
                if child.get("BlockType") == "WORD":
                    words.append(child.get("Text", ""))
    return " ".join(words)
