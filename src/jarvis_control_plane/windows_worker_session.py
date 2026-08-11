"""Bounded RPC over the Windows worker's outbound TLS 1.3 session."""

from __future__ import annotations

import json
import secrets
import socket
import ssl
import struct
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
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
    handshake_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.bind_host or self.bind_host.strip() != self.bind_host:
            raise ValueError("Windows mTLS bind host must be canonical")
        if self.bind_host in {"0.0.0.0", "::"}:
            raise ValueError("Windows mTLS listener must bind one overlay address")
        if isinstance(self.bind_port, bool) or not 1 <= self.bind_port <= 65535:
            raise ValueError("Windows mTLS bind port is invalid")
        if (
            isinstance(self.handshake_timeout_seconds, bool)
            or not isinstance(self.handshake_timeout_seconds, (int, float))
            or self.handshake_timeout_seconds <= 0
        ):
            raise ValueError("Windows mTLS handshake timeout must be positive")
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
        try:
            chunk = connection.recv(length - len(chunks))
        except OSError as exc:
            raise ActionDispatcherError("Windows worker session disconnected") from exc
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
        self._send_lock = RLock()
        self._pending_lock = RLock()
        self._pending: dict[str, Queue[object]] = {}
        self._reader = Thread(target=self._read_responses, daemon=True)
        self._reader.start()

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
            timeout_seconds=(
                invocation.deadline_seconds + invocation.cancellation_grace_seconds
            ),
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
        request_id = secrets.token_hex(16)
        response_queue: Queue[object] = Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = response_queue
        try:
            with self._send_lock:
                _send_frame(
                    self._connection,
                    {
                        "request_id": request_id,
                        "operation": operation,
                        "arguments": arguments,
                    },
                )
            response = response_queue.get(timeout=timeout_seconds)
        except Empty as exc:
            self._close()
            raise ActionDispatcherError(
                "Windows worker session call timed out",
                may_have_dispatched=operation == "execute",
            ) from exc
        except (OSError, ActionDispatcherError) as exc:
            raise ActionDispatcherError(
                "Windows worker session call failed",
                may_have_dispatched=operation == "execute",
            ) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if isinstance(response, BaseException):
            raise ActionDispatcherError(
                "Windows worker session call failed",
                may_have_dispatched=operation == "execute",
            ) from response
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ActionDispatcherError(
                "Windows worker rejected the bounded operation",
                may_have_dispatched=operation == "execute",
            )
        return response.get("result")

    def _read_responses(self) -> None:
        try:
            while True:
                response = _receive_frame(self._connection)
                request_id = response.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    raise ActionDispatcherError(
                        "Windows worker response request ID is invalid"
                    )
                with self._pending_lock:
                    response_queue = self._pending.get(request_id)
                if response_queue is None:
                    raise ActionDispatcherError(
                        "Windows worker response request ID is unknown"
                    )
                response_queue.put(response)
        except (OSError, ActionDispatcherError) as exc:
            self._fail_pending(exc)

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            queues = tuple(self._pending.values())
        for response_queue in queues:
            try:
                response_queue.put_nowait(error)
            except Full:
                pass

    def _close(self) -> None:
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._connection.close()


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
                tls: ssl.SSLSocket | None = None
                try:
                    raw.settimeout(self.config.handshake_timeout_seconds)
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
                    tls.settimeout(None)
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
                    (tls if tls is not None else raw).close()

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

    send_lock = RLock()

    def handle(request: dict[str, object]) -> None:
        request_id = request.get("request_id")
        operation = request.get("operation")
        arguments = request.get("arguments")
        if not isinstance(request_id, str) or not request_id:
            raise ActionDispatcherError("Windows worker request ID is invalid")
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
            response = {"request_id": request_id, "ok": True, "result": result}
        except Exception:  # noqa: BLE001 - translate worker failures at the boundary
            response = {"request_id": request_id, "ok": False}
        with send_lock:
            _send_frame(connection, response)

    while True:
        request = _receive_frame(connection)
        if request.get("operation") == "execute":
            Thread(target=handle, args=(request,), daemon=True).start()
        else:
            handle(request)


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
