"""Summarize one bounded live acceptance interval without private trace bodies.

Terminal intervals use execution events, not batch-result timestamps. Reader
intervals include cleanup. Gateway acknowledgements are not recipient receipts.
Run against the live trace directory using the installed release interpreter.
"""

import argparse
import hashlib
import json
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--since", required=True)
parser.add_argument("--until", required=True)
args = parser.parse_args()


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


since, until = stamp(args.since), stamp(args.until)
rows = []
for path in Path("/home/samik/Projects/Jarvis/other/runtime/data").glob(
    "runtime-trace.jsonl*"
):
    if path.stat().st_mtime < since:
        continue
    for line in path.open():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since <= stamp(row["timestamp"]) <= until:
            rows.append(row)
rows.sort(key=lambda row: row["timestamp"])
counts = Counter(row["event"] for row in rows)
batches = []
terminal_starts = defaultdict(deque)
ambiguous_commands = set()
terminals = []
readers = []
results = []
transport = []
tool_results = []
admissions = []
call_labels = {}
message_labels = {}
for row in rows:
    event, payload, at = row["event"], row["payload"], row["timestamp"]
    if event == "authorized_message":
        text = payload["text"]
        label = message_labels.setdefault(
            payload["message_id"], f"message_{len(message_labels) + 1}"
        )
        admissions.append(
            {
                "at": at,
                "message": label,
                "control": text
                if text
                in {
                    "/status",
                    "/cancel",
                    "/new",
                    "/full-access",
                    "/full-access off",
                    "1",
                    "9",
                }
                else "ordinary_request",
            }
        )
    elif event == "responses_output":
        calls = [
            item for item in payload["output"] if item.get("type") == "function_call"
        ]
        for call in calls:
            call_labels.setdefault(call["call_id"], f"call_{len(call_labels) + 1}")
        if calls:
            batches.append(
                {
                    "at": at,
                    "calls": [
                        {"id": call_labels[call["call_id"]], "tool": call["name"]}
                        for call in calls
                    ],
                }
            )
    elif event == "terminal_execution_started":
        key = (payload["host"], payload["command"])
        if terminal_starts[key]:
            ambiguous_commands.add(hashlib.sha256(key[1].encode()).hexdigest())
        terminal_starts[key].append(at)
    elif event in {
        "terminal_execution_finished",
        "terminal_execution_cancelled",
        "terminal_execution_error",
    }:
        key = (payload["host"], payload["command"])
        starts = terminal_starts[key]
        start = starts.popleft() if starts else None
        terminals.append(
            {
                "command_sha256": hashlib.sha256(key[1].encode()).hexdigest(),
                "start": start,
                "end": at,
                "event": event,
                "seconds": round(stamp(at) - stamp(start), 6) if start else None,
                "exit_code": payload.get("exit_code"),
                "timed_out": payload.get("timed_out"),
                "remote_effect_uncertain": payload.get("remote_effect_uncertain"),
            }
        )
    elif event == "playwright_reader_finished":
        readers.append(
            {
                "end": at,
                **{
                    key: payload.get(key)
                    for key in (
                        "state",
                        "browser_state",
                        "model_turns",
                        "elapsed_seconds",
                    )
                },
            }
        )
    elif event == "runtime_result":
        results.append(
            {
                "at": at,
                "disposition": payload["disposition"],
                "reply_characters": sum(map(len, payload.get("replies", []))),
            }
        )
    elif event in {
        "openwa_send_attempt",
        "openwa_send_result",
        "openwa_send_error",
        "openwa_reply_discarded",
    }:
        transport.append(
            {
                "at": at,
                "event": event,
                "message": message_labels.get(
                    payload.get("message_id"), "outside_interval"
                ),
                **{
                    key: payload[key]
                    for key in (
                        "disposition",
                        "code",
                        "may_have_sent",
                        "reason",
                        "chunk_index",
                    )
                    if key in payload
                },
            }
        )
    elif event == "tool_result":
        output = payload.get("output", "")
        tool_results.append(
            {
                "at": at,
                "id": call_labels.get(payload["call_id"], "unknown"),
                "tool": payload["name"],
                "characters": len(output),
                "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            }
        )

overlaps = []
for interval in terminals:
    if interval["command_sha256"] in ambiguous_commands:
        interval["start"] = None
        interval["seconds"] = None
        interval["attribution"] = "ambiguous_concurrent_identical_commands"
for index, left in enumerate(terminals):
    for right_index, right in enumerate(terminals[index + 1 :], index + 1):
        if not left["start"] or not right["start"]:
            continue
        seconds = min(stamp(left["end"]), stamp(right["end"])) - max(
            stamp(left["start"]), stamp(right["start"])
        )
        if seconds > 0:
            overlaps.append(
                {"terminal_indices": [index, right_index], "seconds": round(seconds, 6)}
            )

print(
    json.dumps(
        {
            "since": args.since,
            "until": args.until,
            "event_counts": counts,
            "admissions": admissions,
            "main_model_calls": counts["responses_request"],
            "reader_model_calls": counts["playwright_reader_request"],
            "main_model_seconds": sum(
                row["payload"].get("elapsed_seconds", 0)
                for row in rows
                if row["event"] == "responses_output"
            ),
            "batches": batches,
            "parallel_batch_count": sum(len(batch["calls"]) > 1 for batch in batches),
            "terminal_intervals": terminals,
            "terminal_pair_overlaps": overlaps,
            "unmatched_terminal_starts": sum(map(len, terminal_starts.values())),
            "reader_terminal_events_including_cleanup": readers,
            "tool_results": tool_results,
            "runtime_results": results,
            "transport": transport,
            "recipient_receipt_time": None,
        },
        indent=2,
    )
)
