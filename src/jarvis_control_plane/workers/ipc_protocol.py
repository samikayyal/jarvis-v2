"""Framing and typed wire codecs for the native Ubuntu worker channel."""

from __future__ import annotations

import json
import select
import socket
import struct
from collections.abc import Mapping
from threading import RLock
from time import monotonic
from typing import cast

from ..ports import ActionDispatcherError
from ..terminal_policy import TerminalAction, TerminalComponent
from .contracts import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
)

_MAX_FRAME_BYTES = 13 * 1024 * 1024
_MAX_PENDING_MESSAGES = 130


class _PollingFrameReceiver:
    """Retain partial frame bytes while periodically yielding to a stop event."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._body_length: int | None = None

    def receive(
        self, connection: socket.socket, *, deadline: float | None
    ) -> Mapping[str, object]:
        while True:
            if self._body_length is None and len(self._buffer) >= 4:
                body_length = struct.unpack("!I", self._buffer[:4])[0]
                if not 2 <= body_length <= _MAX_FRAME_BYTES:
                    raise ActionDispatcherError("Ubuntu worker frame length is invalid")
                self._body_length = body_length
            if (
                self._body_length is not None
                and len(self._buffer) >= 4 + self._body_length
            ):
                end = 4 + self._body_length
                body = bytes(self._buffer[4:end])
                del self._buffer[:end]
                self._body_length = None
                return _decode_frame_body(body)
            timeout = None if deadline is None else max(deadline - monotonic(), 0)
            if timeout == 0:
                raise TimeoutError("Ubuntu worker receive timed out")
            readable, _, _ = select.select([connection], [], [], timeout)
            if not readable:
                raise TimeoutError("Ubuntu worker receive timed out")
            chunk = connection.recv(64 * 1024)
            if not chunk:
                raise EOFError("Ubuntu worker channel closed")
            self._buffer.extend(chunk)


def _send_frame(
    connection: socket.socket,
    value: Mapping[str, object],
    *,
    deadline: float,
    lock: RLock,
) -> None:
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    if len(body) > _MAX_FRAME_BYTES:
        raise ActionDispatcherError("Ubuntu worker frame exceeds its bound")
    frame = memoryview(struct.pack("!I", len(body)) + body)
    with lock:
        while frame:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Ubuntu worker send timed out")
            _, writable, _ = select.select([], [connection], [], remaining)
            if not writable:
                raise TimeoutError("Ubuntu worker send timed out")
            sent = connection.send(frame)
            if sent < 1:
                raise EOFError("Ubuntu worker channel closed")
            frame = frame[sent:]


def _recv_frame(
    connection: socket.socket, *, deadline: float | None
) -> Mapping[str, object]:
    header = _recv_exact(connection, 4, deadline=deadline)
    length = struct.unpack("!I", header)[0]
    if length < 2 or length > _MAX_FRAME_BYTES:
        raise ActionDispatcherError("Ubuntu worker frame length is invalid")
    raw = _recv_exact(connection, length, deadline=deadline)
    return _decode_frame_body(raw)


def _decode_frame_body(raw: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ActionDispatcherError("Ubuntu worker frame is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ActionDispatcherError("Ubuntu worker frame must be an object")
    return value


def _recv_exact(
    connection: socket.socket, size: int, *, deadline: float | None
) -> bytes:
    value = bytearray()
    while len(value) < size:
        timeout = None if deadline is None else max(deadline - monotonic(), 0)
        if timeout == 0:
            raise TimeoutError("Ubuntu worker receive timed out")
        readable, _, _ = select.select([connection], [], [], timeout)
        if not readable:
            raise TimeoutError("Ubuntu worker receive timed out")
        chunk = connection.recv(size - len(value))
        if not chunk:
            raise EOFError("Ubuntu worker channel closed")
        value.extend(chunk)
    return bytes(value)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _require_keys(value: Mapping[str, object], allowed: set[str]) -> None:
    if set(value) != allowed:
        raise ValueError("unknown or missing fields")


def _required_object(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value[key]
    if not isinstance(item, dict):
        raise TypeError(f"{key} must be an object")
    return item


def _required_text(
    value: Mapping[str, object], key: str, *, allowed: set[str] | None = None
) -> str:
    if allowed is not None and not set(value) <= allowed:
        raise ValueError("response has unknown fields")
    item = value[key]
    if not isinstance(item, str) or not item:
        raise TypeError(f"{key} must be non-blank text")
    return item


def _required_text_allow_empty(value: Mapping[str, object], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise TypeError(f"{key} must be text")
    return item


def _required_int(value: Mapping[str, object], key: str) -> int:
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, int):
        raise TypeError(f"{key} must be an integer")
    return item


def _required_bool(value: Mapping[str, object], key: str) -> bool:
    item = value[key]
    if not isinstance(item, bool):
        raise TypeError(f"{key} must be boolean")
    return item


def _required_text_list(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    item = value[key]
    if not isinstance(item, list) or any(not isinstance(part, str) for part in item):
        raise TypeError(f"{key} must be a text list")
    return tuple(item)


def _lifecycle_payload(
    payload: Mapping[str, object],
) -> tuple[str, int, int]:
    _require_keys(payload, {"action_id", "timeout_seconds", "retention_seconds"})
    return (
        _required_text(payload, "action_id"),
        _required_int(payload, "timeout_seconds"),
        _required_int(payload, "retention_seconds"),
    )


def _identity_to_wire(identity: WorkerIdentity) -> dict[str, object]:
    return {
        "host": identity.host,
        "worker_id": identity.worker_id,
        "connection_id": identity.connection_id,
    }


def _identity_from_wire(value: Mapping[str, object]) -> WorkerIdentity:
    _require_keys(value, {"host", "worker_id", "connection_id"})
    return WorkerIdentity(
        host=_required_text(value, "host"),
        worker_id=_required_text(value, "worker_id"),
        connection_id=_required_text(value, "connection_id"),
    )


def _invocation_to_wire(invocation: WorkerInvocation) -> dict[str, object]:
    return {
        "action_id": invocation.action_id,
        "action": {
            "host": invocation.action.host,
            "executable": invocation.action.executable,
            "arguments": list(invocation.action.arguments),
            "cwd": invocation.action.cwd,
            "components": [
                {
                    "executable": component.executable,
                    "arguments": list(component.arguments),
                    "operator_before": component.operator_before,
                    "redirections": list(component.redirections),
                }
                for raw_component in invocation.action.components
                for component in (cast(TerminalComponent, raw_component),)
            ],
        },
        "interactive": invocation.interactive,
        "deadline_seconds": invocation.deadline_seconds,
        "stdout_limit_bytes": invocation.stdout_limit_bytes,
        "stderr_limit_bytes": invocation.stderr_limit_bytes,
        "cancellation_grace_seconds": invocation.cancellation_grace_seconds,
        "progress_event_limit": invocation.progress_event_limit,
        "milestone_limit_bytes": invocation.milestone_limit_bytes,
        "worker_identity": _identity_to_wire(invocation.worker_identity),
    }


def _invocation_from_wire(value: Mapping[str, object]) -> WorkerInvocation:
    fields = {
        "action_id",
        "action",
        "interactive",
        "deadline_seconds",
        "stdout_limit_bytes",
        "stderr_limit_bytes",
        "cancellation_grace_seconds",
        "progress_event_limit",
        "milestone_limit_bytes",
        "worker_identity",
    }
    _require_keys(value, fields)
    action = _required_object(value, "action")
    _require_keys(action, {"host", "executable", "arguments", "cwd", "components"})
    components = action["components"]
    if not isinstance(components, list):
        raise TypeError("components must be a list")
    return WorkerInvocation(
        action_id=_required_text(value, "action_id"),
        action=TerminalAction.from_mapping(
            {
                "host": _required_text(action, "host"),
                "executable": _required_text(action, "executable"),
                "arguments": list(_required_text_list(action, "arguments")),
                "cwd": _required_text(action, "cwd"),
                "components": components,
            }
        ),
        interactive=_required_bool(value, "interactive"),
        deadline_seconds=_required_int(value, "deadline_seconds"),
        stdout_limit_bytes=_required_int(value, "stdout_limit_bytes"),
        stderr_limit_bytes=_required_int(value, "stderr_limit_bytes"),
        cancellation_grace_seconds=_required_int(value, "cancellation_grace_seconds"),
        progress_event_limit=_required_int(value, "progress_event_limit"),
        milestone_limit_bytes=_required_int(value, "milestone_limit_bytes"),
        worker_identity=_identity_from_wire(_required_object(value, "worker_identity")),
    )


def _progress_to_wire(event: WorkerProgressEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "kind": WorkerProgressKind(event.kind).value,
        "text": event.text,
        "stream": (
            WorkerOutputStream(event.stream).value if event.stream is not None else None
        ),
        "truncated": event.truncated,
    }


def _progress_from_wire(value: Mapping[str, object]) -> WorkerProgressEvent:
    _require_keys(value, {"sequence", "kind", "text", "stream", "truncated"})
    stream = value["stream"]
    if stream is not None and not isinstance(stream, str):
        raise TypeError("stream must be text or null")
    return WorkerProgressEvent(
        sequence=_required_int(value, "sequence"),
        kind=WorkerProgressKind(_required_text(value, "kind")),
        text=_required_text_allow_empty(value, "text"),
        stream=WorkerOutputStream(stream) if stream is not None else None,
        truncated=_required_bool(value, "truncated"),
    )


def _result_to_wire(result: WorkerExecutionResult) -> dict[str, object]:
    return {
        "status": WorkerExecutionStatus(result.status).value,
        "started_components": list(result.started_components),
        "completed_components": list(result.completed_components),
        "process_tree_stopped": result.process_tree_stopped,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
    }


def _result_from_wire(value: Mapping[str, object]) -> WorkerExecutionResult:
    _require_keys(
        value,
        {
            "status",
            "started_components",
            "completed_components",
            "process_tree_stopped",
            "stdout",
            "stderr",
            "stdout_truncated",
            "stderr_truncated",
        },
    )
    started = value["started_components"]
    completed = value["completed_components"]
    if not isinstance(started, list) or not isinstance(completed, list):
        raise TypeError("component progress must be a list")
    return WorkerExecutionResult(
        status=WorkerExecutionStatus(_required_text(value, "status")),
        started_components=tuple(started),
        completed_components=tuple(completed),
        process_tree_stopped=_required_bool(value, "process_tree_stopped"),
        stdout=_required_text_allow_empty(value, "stdout"),
        stderr=_required_text_allow_empty(value, "stderr"),
        stdout_truncated=_required_bool(value, "stdout_truncated"),
        stderr_truncated=_required_bool(value, "stderr_truncated"),
    )


def _error_to_wire(exc: ActionDispatcherError) -> dict[str, object]:
    return {
        "type": "error",
        "message": str(exc),
        "may_have_dispatched": exc.may_have_dispatched,
    }
