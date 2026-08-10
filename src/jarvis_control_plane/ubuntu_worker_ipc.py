"""Strict local-socket wire adapter for the native Ubuntu worker.

Only bounded JSON objects made from primitive values cross this process
boundary.  Pickle and generic object deserialization are deliberately absent.
"""

from __future__ import annotations

import json
import queue
import secrets
import select
import socket
import struct
from collections.abc import Mapping
from threading import Event, RLock, Thread
from time import monotonic
from typing import cast

from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
)
from .terminal_policy import TerminalAction, TerminalComponent
from .ubuntu_worker import (
    UbuntuLocalAuthenticator,
    UbuntuLocalPeerExpectation,
    UbuntuWorkerService,
)
from .worker_gateway import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerProgressSink,
)

# Two independently capped 1-MiB streams can expand sixfold under JSON escaping
# (for example, NUL bytes).  Keep that overhead bounded without rejecting a
# valid maximum-size terminal result.
_MAX_FRAME_BYTES = 13 * 1024 * 1024
_MAX_PENDING_MESSAGES = 130


class UnixSocketUbuntuWorkerTransport:
    """Gateway-side worker transport over one authenticated Unix socket."""

    def __init__(
        self,
        *,
        connection: socket.socket,
        authenticator: UbuntuLocalAuthenticator,
        expected_peer: UbuntuLocalPeerExpectation,
        registered_identity: WorkerIdentity,
    ) -> None:
        if not authenticator.binds(connection):
            raise ValueError(
                "Ubuntu local authenticator must bind the exact dispatch channel"
            )
        if registered_identity.host != "ubuntu":
            raise ValueError("local Ubuntu channel requires an Ubuntu identity")
        self._connection = connection
        self._connection.setblocking(False)
        self._authenticator = authenticator
        self._expected_peer = expected_peer
        self._registered_identity = registered_identity
        self._send_lock = RLock()
        self._pending_lock = RLock()
        self._pending: dict[str, queue.Queue[Mapping[str, object]]] = {}
        self._closed = Event()
        self._reader = Thread(target=self._read_responses, daemon=True)
        self._reader.start()

    def register_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        self._call(
            "register",
            {
                "action_id": action_id,
                "timeout_seconds": timeout_seconds,
                "retention_seconds": retention_seconds,
            },
            timeout_seconds=timeout_seconds,
        )

    def authenticate(
        self, *, selected_host: str, timeout_seconds: int
    ) -> WorkerIdentity:
        peer = self._authenticator.authenticate(timeout_seconds=timeout_seconds)
        if not self._expected_peer.matches(peer):
            raise ActionDispatcherError("Ubuntu dispatch channel identity failed")
        payload = self._call(
            "authenticate",
            {"selected_host": selected_host, "timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )
        identity = _identity_from_wire(payload)
        if identity != self._registered_identity:
            raise ActionDispatcherError("registered Ubuntu worker identity changed")
        return identity

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        payload = self._call(
            "execute",
            {"invocation": _invocation_to_wire(invocation)},
            timeout_seconds=(
                invocation.deadline_seconds + invocation.cancellation_grace_seconds
            ),
            progress=progress,
        )
        return _result_from_wire(payload)

    def cancel(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> ActionCancellationResult:
        payload = self._call(
            "cancel",
            {
                "action_id": action_id,
                "timeout_seconds": timeout_seconds,
                "retention_seconds": retention_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        return ActionCancellationResult(
            _required_text(payload, "status", allowed={"status"})
        )

    def finalize_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        self._call(
            "finalize",
            {
                "action_id": action_id,
                "timeout_seconds": timeout_seconds,
                "retention_seconds": retention_seconds,
            },
            timeout_seconds=timeout_seconds,
        )

    def close(self) -> None:
        self._closed.set()
        try:
            self._connection.close()
        finally:
            self._fail_pending("Ubuntu worker channel closed")
            self._reader.join(timeout=1)

    def _call(
        self,
        operation: str,
        payload: Mapping[str, object],
        *,
        timeout_seconds: int,
        progress: WorkerProgressSink | None = None,
    ) -> Mapping[str, object]:
        if self._closed.is_set():
            raise ActionDispatcherError("Ubuntu worker channel is unavailable")
        request_id = secrets.token_hex(16)
        responses: queue.Queue[Mapping[str, object]] = queue.Queue(
            maxsize=_MAX_PENDING_MESSAGES
        )
        with self._pending_lock:
            self._pending[request_id] = responses
        deadline = monotonic() + timeout_seconds
        try:
            _send_frame(
                self._connection,
                {
                    "request_id": request_id,
                    "operation": operation,
                    "payload": dict(payload),
                },
                deadline=deadline,
                lock=self._send_lock,
            )
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Ubuntu worker {operation} timed out")
                try:
                    message = responses.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError(f"Ubuntu worker {operation} timed out") from exc
                message_type = _required_text(
                    message,
                    "type",
                    allowed={
                        "request_id",
                        "type",
                        "payload",
                        "message",
                        "may_have_dispatched",
                    },
                )
                if message_type == "progress":
                    _require_keys(message, {"request_id", "type", "payload"})
                    if progress is None:
                        raise ActionDispatcherError(
                            "Ubuntu worker sent unexpected progress"
                        )
                    progress(_progress_from_wire(_required_object(message, "payload")))
                    continue
                if message_type == "error":
                    _require_keys(
                        message,
                        {
                            "request_id",
                            "type",
                            "message",
                            "may_have_dispatched",
                        },
                    )
                    error_message = _required_text(message, "message")
                    may_have_dispatched = message.get("may_have_dispatched", False)
                    if not isinstance(may_have_dispatched, bool):
                        raise ActionDispatcherError("Ubuntu worker error was malformed")
                    raise ActionDispatcherError(
                        error_message, may_have_dispatched=may_have_dispatched
                    )
                if message_type != "result":
                    raise ActionDispatcherError("Ubuntu worker response was malformed")
                _require_keys(message, {"request_id", "type", "payload"})
                return _required_object(message, "payload")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _read_responses(self) -> None:
        try:
            while not self._closed.is_set():
                message = _recv_frame(self._connection, deadline=None)
                request_id = _required_text(message, "request_id")
                with self._pending_lock:
                    responses = self._pending.get(request_id)
                if responses is None:
                    raise ActionDispatcherError(
                        "Ubuntu worker returned an unknown request identifier"
                    )
                responses.put(message)
        except (ActionDispatcherError, OSError, EOFError):
            self._closed.set()
            self._fail_pending("Ubuntu worker channel disconnected")

    def _fail_pending(self, message: str) -> None:
        with self._pending_lock:
            pending = tuple(self._pending.items())
        for request_id, responses in pending:
            try:
                responses.put_nowait(
                    {
                        "request_id": request_id,
                        "type": "error",
                        "message": message,
                        "may_have_dispatched": True,
                    }
                )
            except queue.Full:
                pass


def serve_ubuntu_worker_connection(
    connection: socket.socket,
    worker: UbuntuWorkerService,
    *,
    stop: Event | None = None,
) -> None:
    """Serve one already-accepted local connection; no listener is created."""

    if not worker.binds(connection):
        raise ValueError("Ubuntu worker service must authenticate its exact channel")
    connection.setblocking(False)
    try:
        worker.authenticate(selected_host="ubuntu", timeout_seconds=10)
    except BaseException:
        connection.close()
        raise
    send_lock = RLock()
    active_lock = RLock()
    active_action_id: str | None = None
    retention_by_action: dict[str, int] = {}
    receiver = _PollingFrameReceiver()

    def send(request_id: str, message: Mapping[str, object]) -> None:
        _send_frame(
            connection,
            {"request_id": request_id, **message},
            deadline=monotonic() + 30,
            lock=send_lock,
        )

    def execute(request_id: str, invocation: WorkerInvocation, action_id: str) -> None:
        nonlocal active_action_id
        try:
            result = worker.execute(
                invocation,
                lambda event: send(
                    request_id,
                    {"type": "progress", "payload": _progress_to_wire(event)},
                ),
            )
            send(request_id, {"type": "result", "payload": _result_to_wire(result)})
        except ActionDispatcherError as exc:
            send(request_id, _error_to_wire(exc))
        except BaseException:  # noqa: BLE001 - close an untrusted process boundary
            send(
                request_id,
                {
                    "type": "error",
                    "message": "Ubuntu worker execution failed",
                    "may_have_dispatched": True,
                },
            )
        finally:
            with active_lock:
                if active_action_id == action_id:
                    active_action_id = None

    try:
        while stop is None or not stop.is_set():
            try:
                request = receiver.receive(
                    connection,
                    deadline=(monotonic() + 0.25 if stop is not None else None),
                )
            except TimeoutError:
                continue
            _require_keys(request, {"request_id", "operation", "payload"})
            request_id = _required_text(request, "request_id")
            operation = _required_text(request, "operation")
            payload = _required_object(request, "payload")
            try:
                if operation == "register":
                    action_id, timeout, retention = _lifecycle_payload(payload)
                    worker.register_execution(
                        action_id=action_id,
                        timeout_seconds=timeout,
                        retention_seconds=retention,
                    )
                    retention_by_action[action_id] = retention
                    send(request_id, {"type": "result", "payload": {}})
                elif operation == "authenticate":
                    _require_keys(payload, {"selected_host", "timeout_seconds"})
                    identity = worker.authenticate(
                        selected_host=_required_text(payload, "selected_host"),
                        timeout_seconds=_required_int(payload, "timeout_seconds"),
                    )
                    send(
                        request_id,
                        {"type": "result", "payload": _identity_to_wire(identity)},
                    )
                elif operation == "execute":
                    _require_keys(payload, {"invocation"})
                    invocation = _invocation_from_wire(
                        _required_object(payload, "invocation")
                    )
                    with active_lock:
                        if active_action_id is not None:
                            raise ActionDispatcherError("native Ubuntu worker is busy")
                        active_action_id = invocation.action_id
                    Thread(
                        target=execute,
                        args=(request_id, invocation, invocation.action_id),
                        daemon=True,
                    ).start()
                elif operation == "cancel":
                    action_id, timeout, retention = _lifecycle_payload(payload)
                    result = worker.cancel(
                        action_id=action_id,
                        timeout_seconds=timeout,
                        retention_seconds=retention,
                    )
                    send(
                        request_id,
                        {
                            "type": "result",
                            "payload": {
                                "status": ActionCancellationStatus(result.status).value
                            },
                        },
                    )
                elif operation == "finalize":
                    action_id, timeout, retention = _lifecycle_payload(payload)
                    worker.finalize_execution(
                        action_id=action_id,
                        timeout_seconds=timeout,
                        retention_seconds=retention,
                    )
                    retention_by_action.pop(action_id, None)
                    send(request_id, {"type": "result", "payload": {}})
                else:
                    raise ActionDispatcherError("Ubuntu worker operation is unknown")
            except ActionDispatcherError as exc:
                send(request_id, _error_to_wire(exc))
            except (TypeError, ValueError, KeyError):
                send(
                    request_id,
                    {
                        "type": "error",
                        "message": "Ubuntu worker request was malformed",
                        "may_have_dispatched": False,
                    },
                )
    except (ActionDispatcherError, OSError, EOFError):
        pass
    finally:
        try:
            for action_id, retention in tuple(retention_by_action.items()):
                try:
                    worker.cancel(
                        action_id=action_id,
                        timeout_seconds=10,
                        retention_seconds=retention,
                    )
                except (ActionDispatcherError, TypeError, ValueError):
                    pass
        finally:
            connection.close()


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
    )


def _error_to_wire(exc: ActionDispatcherError) -> dict[str, object]:
    return {
        "type": "error",
        "message": str(exc),
        "may_have_dispatched": exc.may_have_dispatched,
    }
