from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jarvis_personal_runtime.trace import TRACE_FAILURE_WARNING, JsonlRuntimeTrace

NOW = datetime(2026, 8, 30, 12, 34, 56, tzinfo=UTC)


def test_trace_writes_verbatim_payload_as_one_json_line(tmp_path: Path) -> None:
    path = tmp_path / "trace" / "runtime.jsonl"
    trace = JsonlRuntimeTrace(path, max_bytes=10_000, backup_count=2, clock=lambda: NOW)

    payload = {"message": "café\nsecond line", "items": [{"secret": "verbatim"}]}
    trace.record("authorized_message", payload)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "timestamp": "2026-08-30T12:34:56Z",
        "event": "authorized_message",
        "payload": payload,
    }


def test_trace_rotates_complete_json_lines_before_crossing_size_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.jsonl"
    trace = JsonlRuntimeTrace(path, max_bytes=120, backup_count=2, clock=lambda: NOW)

    trace.record("first", {"value": "a" * 25})
    first_line = path.read_text(encoding="utf-8")
    trace.record("second", {"value": "b" * 25})

    assert (tmp_path / "runtime.jsonl.1").read_text(encoding="utf-8") == first_line
    assert json.loads(path.read_text(encoding="utf-8"))["event"] == "second"


def test_trace_failure_warns_once_and_never_blocks_work(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    warnings: list[str] = []
    trace = JsonlRuntimeTrace(
        parent_file / "runtime.jsonl",
        max_bytes=1_000,
        backup_count=1,
        warning=warnings.append,
        clock=lambda: NOW,
    )

    trace.record("first", {"work": "continued"})
    trace.record("second", {"work": "continued"})

    assert warnings == [TRACE_FAILURE_WARNING]
    assert trace.take_warning() == TRACE_FAILURE_WARNING
    assert trace.take_warning() is None
