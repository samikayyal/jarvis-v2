"""Export sanitized metadata for the post-deployment acceptance only."""

import json
from pathlib import Path

start = "2026-10-18T15:14:51Z"
path = Path("/home/samik/Projects/Jarvis/other/runtime/data/runtime-trace.jsonl")
records = []
for line in path.read_text().splitlines():
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if row["timestamp"] < start:
        continue
    event, payload = row["event"], row["payload"]
    record = {"timestamp": row["timestamp"], "event": event}
    if event == "tool_call":
        record["name"] = payload.get("name")
        if record["name"] in {"search_web", "read_web_pages", "load_skill"}:
            record["arguments"] = payload.get("arguments")
    elif event == "tool_result":
        record["name"] = payload.get("name")
        try:
            output = json.loads(payload.get("output", ""))
        except (ValueError, TypeError):
            output = {}
        if isinstance(output, dict):
            for key in ("outcome", "remaining", "reason"):
                if key in output:
                    record[key] = output[key]
            for key in ("results", "pages"):
                if key in output:
                    record[key] = [
                        {
                            k: v
                            for k, v in item.items()
                            if k
                            in {"title", "url", "outcome", "reason", "evidence_type"}
                        }
                        for item in output[key]
                    ]
    elif event not in {"request_started", "request_error", "request_cleanup_error"}:
        continue
    records.append(record)
print(json.dumps(records, ensure_ascii=False, indent=2))
