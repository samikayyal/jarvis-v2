"""Read a bounded acceptance interval without exporting provider request bodies.

Output may contain the operator's request, tools and note content. Keep it private;
commit only the reviewed measurements and factual acceptance record.
"""

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--since", required=True, help="UTC ISO timestamp")
parser.add_argument("--until", help="Inclusive UTC ISO endpoint for a finished run")
args = parser.parse_args()
rows = []
since_timestamp = datetime.fromisoformat(args.since).timestamp()
for path in Path("/home/samik/Projects/Jarvis/other/runtime/data").glob(
    "runtime-trace.jsonl*"
):
    if path.stat().st_mtime < since_timestamp:
        continue
    for line in path.open():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # A writer may still be appending the last line.
        if row["timestamp"] >= args.since and (
            args.until is None or row["timestamp"] <= args.until
        ):
            rows.append(row)
rows.sort(key=lambda row: row["timestamp"])
counts = Counter(row["event"] for row in rows)
selected = {
    "authorized_message",
    "request_started",
    "runtime_result",
    "openwa_send_attempt",
    "openwa_send_result",
    "openwa_send_error",
    "openwa_reply_discarded",
    "openwa_progress_attempt",
    "openwa_progress_result",
    "openwa_progress",
    "openwa_progress_error",
    "tool_call",
    "tool_result",
    "tool_error",
    "request_error",
    "request_timeout",
    "playwright_reader_finished",
    "foreground_cancelled",
}
for row in rows:
    if row["event"] in selected:
        print(json.dumps(row))
    elif row["event"] in {
        "responses_request",
        "responses_output",
        "playwright_reader_request",
        "playwright_reader_output",
    }:
        # Timing and usage only; omit transcripts, screenshots and model bodies.
        print(
            json.dumps(
                {
                    **row,
                    "payload": {
                        key: value
                        for key, value in row["payload"].items()
                        if key
                        in {
                            "run_id",
                            "reader_id",
                            "assignment_id",
                            "model_call",
                            "elapsed_seconds",
                            "usage",
                        }
                    },
                }
            )
        )


def stamp(value):
    return datetime.fromisoformat(value)


requests = [row for row in rows if row["event"] == "request_started"]
deliveries = [row for row in rows if row["event"] == "openwa_send_result"]
timings = []
for start in requests:
    message_id = start["payload"]["message_id"]
    admission = next(
        (
            row
            for row in rows
            if row["event"] == "authorized_message"
            and row["payload"]["message_id"] == message_id
        ),
        start,
    )
    sends = [row for row in deliveries if row["payload"]["message_id"] == message_id]
    if sends:
        timings.append(
            {
                "message_id": message_id,
                "admission": admission["timestamp"],
                "delivered": sends[-1]["timestamp"],
                "admission_to_delivery_seconds": (
                    stamp(sends[-1]["timestamp"]) - stamp(admission["timestamp"])
                ).total_seconds(),
            }
        )
summary = {
    "event_counts": counts,
    "requests": timings,
    "main_calls": counts["responses_request"],
    "reader_calls": counts["playwright_reader_request"],
    "main_model_seconds": sum(
        row["payload"].get("elapsed_seconds", 0)
        for row in rows
        if row["event"] == "responses_output"
    ),
    "reader_model_seconds_nested_within_reader_wall": sum(
        row["payload"].get("elapsed_seconds", 0)
        for row in rows
        if row["event"] == "playwright_reader_output"
    ),
    "readers": [
        row["payload"] for row in rows if row["event"] == "playwright_reader_finished"
    ],
}
print(json.dumps({"summary": summary}))
