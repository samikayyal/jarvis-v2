import json
from pathlib import Path
import sys

path = Path("/home/samik/Projects/Jarvis/other/runtime/data/runtime-trace.jsonl")
marker = Path(
    "/home/samik/Projects/Jarvis/other/runtime/web-acceptance/trace-marker.json"
)
if sys.argv[1] == "mark":
    from datetime import datetime, timezone

    marker.write_text(json.dumps({"start": datetime.now(timezone.utc).isoformat()}))
    print("Trace marker recorded")
else:
    start = json.loads(marker.read_text())["start"]
    records = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row["timestamp"] < start:
            continue
        event, payload = row["event"], row["payload"]
        if event == "tool_call":
            records.append(
                {
                    "timestamp": row["timestamp"],
                    "event": event,
                    "name": payload.get("name"),
                    "arguments": payload.get("arguments"),
                }
            )
        elif event == "tool_result":
            output = payload.get("output", "")
            try:
                output = json.loads(output)
            except (ValueError, TypeError):
                output = {}
            result = {
                "timestamp": row["timestamp"],
                "event": event,
                "name": payload.get("name"),
            }
            if isinstance(output, dict):
                for key in ("outcome", "remaining", "loaded", "reason"):
                    if key in output:
                        result[key] = output[key]
                for key in ("results", "pages"):
                    if key in output:
                        result[key] = [
                            {
                                k: v
                                for k, v in item.items()
                                if k
                                in (
                                    "title",
                                    "url",
                                    "outcome",
                                    "reason",
                                    "evidence_type",
                                )
                            }
                            for item in output[key]
                        ]
            records.append(result)
        elif event in ("request_cleanup_error", "request_error"):
            records.append({"timestamp": row["timestamp"], "event": event})
    print(json.dumps(records, indent=2))
