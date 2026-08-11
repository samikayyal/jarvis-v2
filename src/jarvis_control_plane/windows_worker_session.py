"""Bounded RPC over the Windows worker's outbound TLS 1.3 session."""

from __future__ import annotations

import json
import socket
import ssl
import struct
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock, Thread
from time import sleep

from .ports import ActionDispatcherError
from .service_protocol import MAX_FRAME_BYTES, _decode, _encode
from .windows_worker import (
    OutboundWindowsWorkerTransport,
    WindowsJobObjectExecutor,
    WindowsMtlsClientConfig,
    WindowsWorkerRegistration,
    WindowsWorkerSessionEvidence,
    authenticate_windows_worker_session,
    open_windows_worker_mtls_session,
)
from .worker_gateway import (
    WorkerExecutionResult,
    WorkerInvocation,
    WorkerProgressEvent,
    WorkerProgressSink,
)


@dataclass(frozen=True, slots=True)
class WindowsMtlsServerConfig:
    """Overlay-only listener and gateway certificate material."""

    bind_host: str
    bind_port: int
    ca_file: Path
    certificate_file: Path
    private_key_file: Path

    def __post_init__(self) -> None:
        if not self.bind_host or self.bind_host.strip() != self.bind_host:
            raise ValueError("Windows mTLS bind host must be canonical")
        if self.bind_host in {"0.0.0.0", "::"}:
            raise ValueError("Windows mTLS listener must bind one overlay address")
        if isinstance(self.bind_port, bool) or not 1 <= self.bind_port <= 65535:
            raise ValueError("Windows mTLS bind port is invalid")
        for name in ("ca_file", "certificate_file", "private_key_file"):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise ValueError(
                    f"Windows mTLS {name.replace('_', ' ')} must be absolute"
                )
            object.__setattr__(self, name, value)


def _receive_frame(connection: socket.socket) -> dict[str, object]:
    header = _receive_exact(connection, 4)
    length = struct.unpack("!I", header)[0]
    if not 0 < length <= MAX_FRAME_BYTES:
        raise ActionDispatcherError("Windows worker frame length is invalid")
    try:
        value = json.loads(_receive_exact(connection, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActionDispatcherError("Windows worker frame is malformed") from exc
    if not isinstance(value, dict):
        raise ActionDispatcherError("Windows worker frame must be an object")
    return value


def _receive_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise ActionDispatcherError("Windows worker session disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


def _send_frame(connection: socket.socket, value: dict[str, object]) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ActionDispatcherError("Windows worker frame is oversized")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


class SocketWindowsWorkerSession:
    """Gateway-side session that keeps all worker calls on the outbound socket."""

    def __init__(
        self, *, connection: socket.socket, evidence: WindowsWorkerSessionEvidence
    ) -> None:
        self._connection = connection
        self._evidence = evidence
        self._lock = RLock()

    @property
    def evidence(self) -> WindowsWorkerSessionEvidence:
        return self._evidence

    def ping(self, *, timeout_seconds: int) -> None:
        self._call("heartbeat", {}, timeout_seconds=timeout_seconds)

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        payload = self._call(
            "execute",
            {"invocation": _encode(invocation)},
            timeout_seconds=invocation.deadline_seconds,
        )
        if not isinstance(payload, dict):
            raise ActionDispatcherError("Windows worker execution response is invalid")
        events = _decode(payload.get("progress"))
        result = _decode(payload.get("result"))
        if not isinstance(events, tuple) or not all(
            isinstance(event, WorkerProgressEvent) for event in events
        ):
            raise ActionDispatcherError("Windows worker progress response is invalid")
        if not isinstance(result, WorkerExecutionResult):
            raise ActionDispatcherError("Windows worker result is invalid")
        for event in events:
            progress(event)
        return result

    def terminate_job_object(self, *, action_id: str, timeout_seconds: int) -> bool:
        return (
            self._call(
                "cancel",
                {"action_id": action_id, "timeout_seconds": timeout_seconds},
                timeout_seconds=timeout_seconds,
            )
            is True
        )

    def finalize(self, *, action_id: str, timeout_seconds: int) -> None:
        self._call(
            "finalize",
            {"action_id": action_id, "timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )

    def _call(
        self, operation: str, arguments: dict[str, object], *, timeout_seconds: int
    ) -> object:
        with self._lock:
            self._connection.settimeout(timeout_seconds)
            try:
                _send_frame(
                    self._connection,
                    {"operation": operation, "arguments": arguments},
                )
                response = _receive_frame(self._connection)
            except (OSError, ActionDispatcherError) as exc:
                raise ActionDispatcherError(
                    "Windows worker session call failed",
                    may_have_dispatched=operation == "execute",
                ) from exc
            if response.get("ok") is not True:
                raise ActionDispatcherError(
                    "Windows worker rejected the bounded operation",
                    may_have_dispatched=operation == "execute",
                )
            return response.get("result")


class WindowsWorkerMtlsAcceptor:
    """Accept and attach one authenticated worker-initiated overlay session."""

    def __init__(
        self,
        *,
        config: WindowsMtlsServerConfig,
        registration: WindowsWorkerRegistration,
        transport: OutboundWindowsWorkerTransport,
    ) -> None:
        self.config = config
        self.registration = registration
        self.transport = transport

    def start(self) -> Thread:
        """Validate credentials, bind the overlay address, then accept in background."""

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=str(self.config.ca_file))
        context.load_cert_chain(
            certfile=str(self.config.certificate_file),
            keyfile=str(self.config.private_key_file),
        )
        listener = socket.create_server((self.config.bind_host, self.config.bind_port))
        thread = Thread(
            target=self._serve_forever,
            args=(listener, context),
            daemon=True,
        )
        thread.start()
        return thread

    def _serve_forever(self, listener: socket.socket, context: ssl.SSLContext) -> None:
        with listener:
            while True:
                raw, _address = listener.accept()
                try:
                    tls = context.wrap_socket(raw, server_side=True)
                    hello_frame = _receive_frame(tls)
                    if set(hello_frame) != {"hello"}:
                        raise ActionDispatcherError("Windows worker hello is invalid")
                    hello = hello_frame["hello"]
                    if not isinstance(hello, str):
                        raise ActionDispatcherError("Windows worker hello is invalid")
                    evidence = authenticate_windows_worker_session(
                        registration=self.registration,
                        tls_socket=tls,
                        application_hello=hello.encode("utf-8"),
                    )
                    session = SocketWindowsWorkerSession(
                        connection=tls, evidence=evidence
                    )
                    self.transport.attach(session)
                    Thread(
                        target=self._maintain_session,
                        args=(session,),
                        daemon=True,
                    ).start()
                except Exception:  # noqa: BLE001 - reject one unauthenticated session
                    raw.close()

    def _maintain_session(self, session: SocketWindowsWorkerSession) -> None:
        interval = self.registration.heartbeat_interval_seconds
        try:
            while True:
                sleep(interval)
                session.ping(timeout_seconds=interval)
                self.transport.heartbeat(session)
        except Exception:  # noqa: BLE001 - any session failure revokes readiness
            try:
                self.transport.disconnect(session)
            except ActionDispatcherError:
                pass


def serve_windows_worker_session(
    connection: socket.socket, executor: WindowsJobObjectExecutor
) -> None:
    """Run the Windows side of the closed session after mTLS connects."""

    while True:
        request = _receive_frame(connection)
        operation = request.get("operation")
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            raise ActionDispatcherError("Windows worker request arguments are invalid")
        try:
            if operation == "heartbeat":
                result: object = None
            elif operation == "execute":
                invocation = _decode(arguments.get("invocation"))
                if not isinstance(invocation, WorkerInvocation):
                    raise ActionDispatcherError("Windows worker invocation is invalid")
                progress: list[WorkerProgressEvent] = []
                execution = executor.execute(invocation, progress.append)
                result = {
                    "result": _encode(execution),
                    "progress": _encode(tuple(progress)),
                }
            elif operation == "cancel":
                result = executor.terminate(
                    action_id=str(arguments.get("action_id")),
                    timeout_seconds=int(arguments.get("timeout_seconds", 0)),
                )
            elif operation == "finalize":
                executor.finalize(
                    action_id=str(arguments.get("action_id")),
                    timeout_seconds=int(arguments.get("timeout_seconds", 0)),
                )
                result = None
            else:
                raise ActionDispatcherError("Windows worker operation is not allowed")
            _send_frame(connection, {"ok": True, "result": result})
        except Exception:  # noqa: BLE001 - translate worker failures at the boundary
            _send_frame(connection, {"ok": False})


def run_windows_worker_client(
    *,
    config: WindowsMtlsClientConfig,
    application_hello: bytes,
    executor: WindowsJobObjectExecutor,
    stop: Event | None = None,
) -> None:
    """Connect outbound and serve bounded worker operations until disconnect."""

    if stop is not None and stop.is_set():
        return
    connection = open_windows_worker_mtls_session(config)
    try:
        _send_frame(connection, {"hello": application_hello.decode("utf-8")})
        serve_windows_worker_session(connection, executor)
    finally:
        connection.close()
