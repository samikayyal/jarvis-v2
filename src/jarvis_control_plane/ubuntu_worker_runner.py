"""Internal no-shell runner for structured Ubuntu compound actions.

This module is launched as the main process of the transient systemd unit.  It
interprets only the already-authorized component graph; it is not a parser and
never accepts shell text.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

COMPOUND_RESULT_MARKER = b"\n__JARVIS_COMPOUND_RESULT_V1__:"
_OPERATORS = {"", "|", "&&", "||", ";"}


def main(arguments: Sequence[str] | None = None) -> int:
    values = tuple(sys.argv[1:] if arguments is None else arguments)
    if len(values) != 1:
        return 125
    try:
        plan = _decode_plan(values[0])
        return _run_plan(plan)
    except (TypeError, ValueError, OSError):
        return 125


def _decode_plan(encoded: str) -> tuple[tuple[str, tuple[str, ...], str], ...]:
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode((encoded + padding).encode())
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("compound plan is invalid") from exc
    if not isinstance(value, list) or not value:
        raise ValueError("compound plan must be a non-empty list")
    plan: list[tuple[str, tuple[str, ...], str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {
            "executable",
            "arguments",
            "operator_before",
        }:
            raise ValueError("compound component is invalid")
        executable = item["executable"]
        arguments = item["arguments"]
        operator = item["operator_before"]
        if not isinstance(executable, str) or not os.path.isabs(executable):
            raise ValueError("compound executable is invalid")
        if not isinstance(arguments, list) or any(
            not isinstance(argument, str) or not argument for argument in arguments
        ):
            raise ValueError("compound arguments are invalid")
        if not isinstance(operator, str) or operator not in _OPERATORS:
            raise ValueError("compound operator is invalid")
        if (index == 0 and operator) or (index > 0 and not operator):
            raise ValueError("compound operator placement is invalid")
        plan.append((executable, tuple(arguments), operator))
    return tuple(plan)


def _run_plan(plan: tuple[tuple[str, tuple[str, ...], str], ...]) -> int:
    started: list[int] = []
    completed: list[int] = []
    previous_status = 0
    index = 0
    try:
        while index < len(plan):
            end = index + 1
            while end < len(plan) and plan[end][2] == "|":
                end += 1
            operator = plan[index][2]
            should_run = (
                index == 0
                or operator == ";"
                or (operator == "&&" and previous_status == 0)
                or (operator == "||" and previous_status != 0)
            )
            if should_run:
                previous_status = _run_pipeline(
                    plan[index:end], index, started, completed
                )
            index = end
    finally:
        metadata = json.dumps(
            {"started": started, "completed": completed}, separators=(",", ":")
        ).encode()
        os.write(2, COMPOUND_RESULT_MARKER + metadata + b"\n")
    return previous_status


def _run_pipeline(
    components: tuple[tuple[str, tuple[str, ...], str], ...],
    offset: int,
    started: list[int],
    completed: list[int],
) -> int:
    processes: list[subprocess.Popen[bytes]] = []
    previous_stdout = None
    try:
        for relative_index, (executable, arguments, _operator) in enumerate(components):
            process = subprocess.Popen(
                (executable, *arguments),
                stdin=previous_stdout if previous_stdout is not None else None,
                stdout=(
                    subprocess.PIPE if relative_index < len(components) - 1 else None
                ),
                stderr=None,
                shell=False,
                close_fds=True,
            )
            if previous_stdout is not None:
                previous_stdout.close()
            previous_stdout = process.stdout
            processes.append(process)
            started.append(offset + relative_index)
    except OSError:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait()
        completed.extend(
            offset + relative_index for relative_index in range(len(processes))
        )
        return 126
    statuses = [process.wait() for process in processes]
    completed.extend(offset + relative_index for relative_index in range(len(statuses)))
    return statuses[-1]


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
