"""Validation and bounded-result helpers for the worker gateway."""

from __future__ import annotations

from dataclasses import replace

from ..models import FrozenActionProposal
from ..ports import ActionDispatcherError
from ..terminal_policy import (
    TerminalAction,
    TerminalComponent,
    terminal_action_from_proposal,
)
from .contracts import (
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
)


def _terminal_action(action: FrozenActionProposal) -> TerminalAction:
    if action.kind != "terminal":
        raise ActionDispatcherError("worker gateway accepts terminal actions only")
    try:
        return terminal_action_from_proposal(action)
    except (TypeError, ValueError) as exc:
        raise ActionDispatcherError("terminal action payload is invalid") from exc


def _bounded_result(
    result: WorkerExecutionResult,
    *,
    limits: WorkerExecutionLimits,
    components: tuple[TerminalComponent, ...],
) -> WorkerExecutionResult:
    if not isinstance(result, WorkerExecutionResult):
        raise ActionDispatcherError("worker returned an invalid execution result")
    component_count = len(components)
    if any(index >= component_count for index in result.started_components):
        raise ActionDispatcherError("worker reported an unknown command component")
    if result.status is WorkerExecutionStatus.COMPLETED and result.started_components:
        started = set(result.started_components)
        if result.completed_components != result.started_components:
            raise ActionDispatcherError("worker reported incomplete completed progress")
        if 0 not in started or any(
            component.operator_before == ";" and index not in started
            for index, component in enumerate(components)
        ):
            raise ActionDispatcherError("worker reported impossible compound progress")
        if any(
            component.operator_before == "|"
            and ((index in started) != (index - 1 in started))
            for index, component in enumerate(components)
        ):
            raise ActionDispatcherError("worker reported a partial pipeline")
    stdout_overflow = len(result.stdout.encode()) > limits.stdout_limit_bytes
    stderr_overflow = len(result.stderr.encode()) > limits.stderr_limit_bytes
    return replace(
        result,
        stdout=_truncate_output(result.stdout, limits.stdout_limit_bytes),
        stderr=_truncate_output(result.stderr, limits.stderr_limit_bytes),
        stdout_truncated=result.stdout_truncated or stdout_overflow,
        stderr_truncated=result.stderr_truncated or stderr_overflow,
    )


def _truncate_output(value: str, limit: int) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    suffix = "\n[truncated]"
    if limit <= 0:
        return ""
    if limit <= len(suffix.encode()):
        return encoded[:limit].decode(errors="ignore")
    prefix = encoded[: limit - len(suffix.encode())].decode(errors="ignore")
    return f"{prefix}{suffix}"
