"""Gateway-side Unix-socket transport and reconnect adapter for Ubuntu."""

from __future__ import annotations

import queue
import secrets
import socket
from collections.abc import Callable, Mapping
from threading import Event, RLock, Thread
from time import monotonic
from typing import cast

from ..ports import (
    ActionCancellationResult,
    ActionDispatcherError,
)
from .contracts import (
    WorkerExecutionResult,
    WorkerIdentity,
    WorkerInvocation,
    WorkerProgressSink,
)
from .ipc_protocol import (
    _MAX_PENDING_MESSAGES,
    _identity_from_wire,
    _invocation_to_wire,
    _progress_from_wire,
    _recv_frame,
    _require_keys,
    _required_object,
    _required_text,
    _result_from_wire,
    _send_frame,
)
from .ubuntu_authentication import (
    UbuntuLocalAuthenticator,
    UbuntuLocalPeerExpectation,
)


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

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

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


class ReconnectingUnixSocketUbuntuWorkerTransport:
    """Replace a failed native-worker channel before the next operation.

    An in-flight operation is never retried: its result remains conservative.
    Only a later readiness probe or action gets a freshly authenticated socket.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], UnixSocketUbuntuWorkerTransport],
        initial: UnixSocketUbuntuWorkerTransport,
    ) -> None:
        if not callable(connect):
            raise TypeError("connect must be callable")
        if not isinstance(initial, UnixSocketUbuntuWorkerTransport):
            raise TypeError("initial must be a UnixSocketUbuntuWorkerTransport")
        self._connect = connect
        self._current_transport: UnixSocketUbuntuWorkerTransport | None = initial
        self._lock = RLock()
        self._closed = False

    def register_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        self._invoke(
            "register_execution",
            action_id=action_id,
            timeout_seconds=timeout_seconds,
            retention_seconds=retention_seconds,
        )

    def authenticate(
        self, *, selected_host: str, timeout_seconds: int
    ) -> WorkerIdentity:
        return cast(
            WorkerIdentity,
            self._invoke(
                "authenticate",
                selected_host=selected_host,
                timeout_seconds=timeout_seconds,
            ),
        )

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        return cast(
            WorkerExecutionResult,
            self._invoke("execute", invocation, progress),
        )

    def cancel(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> ActionCancellationResult:
        return cast(
            ActionCancellationResult,
            self._invoke(
                "cancel",
                action_id=action_id,
                timeout_seconds=timeout_seconds,
                retention_seconds=retention_seconds,
            ),
        )

    def finalize_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        self._invoke(
            "finalize_execution",
            action_id=action_id,
            timeout_seconds=timeout_seconds,
            retention_seconds=retention_seconds,
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            transport = self._current_transport
            self._current_transport = None
        if transport is not None:
            transport.close()

    def _transport(self) -> UnixSocketUbuntuWorkerTransport:
        stale: UnixSocketUbuntuWorkerTransport | None = None
        with self._lock:
            if self._closed:
                raise ActionDispatcherError("Ubuntu worker channel is unavailable")
            transport = self._current_transport
            if transport is None or transport.is_closed:
                stale = transport
                transport = self._connect()
                self._current_transport = transport
        if stale is not None:
            stale.close()
        return transport

    def _invoke(self, method: str, *args: object, **kwargs: object) -> object:
        transport = self._transport()
        try:
            return getattr(transport, method)(*args, **kwargs)
        finally:
            if transport.is_closed:
                with self._lock:
                    if self._current_transport is transport:
                        self._current_transport = None
                transport.close()


__all__ = [
    "ReconnectingUnixSocketUbuntuWorkerTransport",
    "UnixSocketUbuntuWorkerTransport",
]
