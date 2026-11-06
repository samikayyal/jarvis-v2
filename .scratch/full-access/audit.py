"""Print only selected acceptance events after the deployment cutoff."""

import json
import sys
from pathlib import Path

root = Path("/home/samik/Projects/Jarvis/other/runtime/data")
cutoff = sys.argv[1]
selected = {
    "terminal_proposal",
    "terminal_execution_started",
    "terminal_execution_finished",
    "terminal_execution_cancelled",
    "full_access_approval",
    "runtime_result",
    "page_reader",
    "tool_error",
}
events = []
for path in root.glob("runtime-trace.jsonl*"):
    with path.open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["timestamp"] >= cutoff and row["event"] in selected:
                events.append(row)
for row in sorted(events, key=lambda item: item["timestamp"]):
    print(json.dumps(row))
