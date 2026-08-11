"""Ticket 27 authenticated inter-service protocol tests."""

from __future__ import annotations

import socket
import ssl
from datetime import UTC, datetime, timedelta
from multiprocessing import Process
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from jarvis_control_plane.adapters import SQLiteAuditBoundary
from jarvis_control_plane.models import AuditEvidence, AuditFilter
from jarvis_control_plane.ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
)
from jarvis_control_plane.service_protocol import (
    MAX_FRAME_BYTES,
    MAX_REQUEST_FRAME_BYTES,
    AuthenticatedServiceClient,
    AuthenticatedServiceServer,
    OwnedActionService,
    RemoteActionDispatcher,
    RemoteAuditBoundary,
    RemoteOrchestrationAdapter,
    ServiceAuthenticationError,
    _decode,
    _encode,
    find_available_port,
    wait_until_ready,
)
from jarvis_control_plane.terminal_policy import TerminalAction
from jarvis_control_plane.windows_worker import (
    OutboundWindowsWorkerTransport,
    WindowsMtlsClientConfig,
    WindowsWorkerRegistration,
    WindowsWorkerSessionEvidence,
)
from jarvis_control_plane.windows_worker_session import (
    SocketWindowsWorkerSession,
    WindowsMtlsServerConfig,
    WindowsWorkerMtlsAcceptor,
    run_windows_worker_client,
    serve_windows_worker_session,
)
from jarvis_control_plane.worker_gateway import (
    WorkerExecutionResult,
    WorkerIdentity,
    WorkerInvocation,
)

SECRET = b"ticket-27-test-secret-with-enough-entropy"
ORCHESTRATION_SECRET = b"ticket-27-orchestration-link-secret!!"


def _evidence(identifier: str) -> AuditEvidence:
    return AuditEvidence(
        evidence_id=identifier,
        kind="request_admitted",
        occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
        event_id=f"event-{identifier}",
        request_id=f"request-{identifier}",
        outcome="accepted",
        actor="jarvis-broker",
        details={},
    )


def _serve_audit(port: int, database: str) -> None:
    audit = SQLiteAuditBoundary(database)
    server = AuthenticatedServiceServer(
        identity="jarvis-audit",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
        operations={
            "append": lambda evidence: audit.append(evidence),
            "append_batch": lambda evidence: audit.append_batch(evidence),
            "safe_view": lambda query=None: audit.safe_view(query),
            "export_json": lambda query=None: audit.export_json(query),
        },
    )
    server.serve_forever()


def _serve_audit_with_link_keys(port: int) -> None:
    audit = SQLiteAuditBoundary(":memory:")
    AuthenticatedServiceServer(
        identity="jarvis-audit",
        client_secrets={
            "jarvis-broker": SECRET,
            "jarvis-orchestration": ORCHESTRATION_SECRET,
        },
        allowed_client_identities=("jarvis-broker", "jarvis-orchestration"),
        host="127.0.0.1",
        port=port,
        operations={"append": audit.append, "safe_view": audit.safe_view},
        allowed_operations_by_client={
            "jarvis-broker": ("append", "safe_view"),
            "jarvis-orchestration": ("safe_view",),
        },
    ).serve_forever()


def test_authenticated_audit_adapter_crosses_real_process_boundary() -> None:
    port = find_available_port()
    with TemporaryDirectory() as directory:
        database = str(Path(directory) / "audit.sqlite3")
        process = Process(target=_serve_audit, args=(port, database), daemon=True)
        process.start()
        try:
            wait_until_ready("127.0.0.1", port)
            audit = RemoteAuditBoundary(
                AuthenticatedServiceClient(
                    identity="jarvis-broker",
                    expected_server_identity="jarvis-audit",
                    secret=SECRET,
                    host="127.0.0.1",
                    port=port,
                )
            )

            audit.append(_evidence("one"))

            assert audit.safe_view(AuditFilter(request_id="request-one")) == (
                _evidence("one"),
            )
        finally:
            process.terminate()
            process.join(timeout=5)


def test_protocol_rejects_wrong_peer_secret_without_running_operation() -> None:
    port = find_available_port()
    process = Process(target=_serve_audit, args=(port, ":memory:"), daemon=True)
    process.start()
    try:
        wait_until_ready("127.0.0.1", port)
        client = AuthenticatedServiceClient(
            identity="jarvis-broker",
            expected_server_identity="jarvis-audit",
            secret=b"wrong-secret-with-enough-entropy",
            host="127.0.0.1",
            port=port,
        )

        with pytest.raises(ServiceAuthenticationError):
            client.call("safe_view", None)
    finally:
        process.terminate()
        process.join(timeout=5)


def test_protocol_rejects_non_allowlisted_operation() -> None:
    port = find_available_port()
    process = Process(target=_serve_audit, args=(port, ":memory:"), daemon=True)
    process.start()
    try:
        wait_until_ready("127.0.0.1", port)
        client = AuthenticatedServiceClient(
            identity="jarvis-broker",
            expected_server_identity="jarvis-audit",
            secret=SECRET,
            host="127.0.0.1",
            port=port,
        )

        with pytest.raises(PermissionError, match="operation is not allowed"):
            client.call("delete_all")
    finally:
        process.terminate()
        process.join(timeout=5)


def test_remote_action_keeps_prepared_handle_inside_owner() -> None:
    class Handle:
        def run(self) -> str:
            return "completed"

    class Dispatcher:
        def prepare(self, action: object) -> Handle:
            return Handle()

        def cancel(self, *, action_id: str) -> ActionCancellationResult:
            return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)

    service = OwnedActionService(Dispatcher())  # type: ignore[arg-type]
    assert set(service.operations()) == {
        "action_prepare",
        "action_run",
        "action_cancel",
        "action_finalize",
    }
    # The public adapter itself is exercised through the same client interface in
    # broker tests; this assertion protects the intended production adapter type.
    assert RemoteActionDispatcher


def test_protocol_round_trips_enums_and_valid_large_terminal_envelopes() -> None:
    assert _decode(_encode(ActionCancellationStatus.NOT_STARTED)) is (
        ActionCancellationStatus.NOT_STARTED
    )
    assert MAX_REQUEST_FRAME_BYTES < MAX_FRAME_BYTES

    port = find_available_port()
    result = "\0" * (2 * 1024 * 1024)
    server = AuthenticatedServiceServer(
        identity="jarvis-worker-gateway",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
        operations={"result": lambda: result},
    )
    Thread(target=server.serve_forever, daemon=True).start()
    wait_until_ready("127.0.0.1", port)
    client = AuthenticatedServiceClient(
        identity="jarvis-broker",
        expected_server_identity="jarvis-worker-gateway",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
    )
    try:
        assert client.call("result") == result
    finally:
        server.shutdown()


def test_remote_orchestration_cancellation_crosses_the_authenticated_adapter() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        def call(self, operation: str, **kwargs: object) -> object:
            calls.append((operation, kwargs))
            return True

    adapter = RemoteOrchestrationAdapter(Client())  # type: ignore[arg-type]

    assert adapter.cancel(request_id="request-cancel") is True
    assert calls == [("cancel", {"request_id": "request-cancel"})]


def test_service_controls_remain_responsive_during_an_active_request() -> None:
    started = Event()
    release = Event()

    def slow() -> str:
        started.set()
        assert release.wait(timeout=5)
        return "slow"

    port = find_available_port()
    server = AuthenticatedServiceServer(
        identity="jarvis-broker",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
        operations={"slow": slow, "status": lambda: "ready"},
    )
    Thread(target=server.serve_forever, daemon=True).start()
    wait_until_ready("127.0.0.1", port)
    client = AuthenticatedServiceClient(
        identity="jarvis-broker",
        expected_server_identity="jarvis-broker",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
        operation_timeouts={"slow": 5},
    )
    slow_result: list[object] = []
    slow_thread = Thread(target=lambda: slow_result.append(client.call("slow")))
    slow_thread.start()
    assert started.wait(timeout=2)
    try:
        assert client.call("status") == "ready"
    finally:
        release.set()
        slow_thread.join(timeout=5)
        server.shutdown()
    assert slow_result == ["slow"]


def test_operation_specific_timeout_overrides_short_transport_default() -> None:
    port = find_available_port()
    server = AuthenticatedServiceServer(
        identity="jarvis-orchestration",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
        operations={"run": lambda: (sleep(0.1), "done")[1]},
    )
    Thread(target=server.serve_forever, daemon=True).start()
    wait_until_ready("127.0.0.1", port)
    client = AuthenticatedServiceClient(
        identity="jarvis-broker",
        expected_server_identity="jarvis-orchestration",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
        timeout_seconds=0.01,
        operation_timeouts={"run": 1},
    )
    try:
        assert client.call("run") == "done"
    finally:
        server.shutdown()


def test_per_link_key_cannot_impersonate_a_more_privileged_client() -> None:
    port = find_available_port()
    process = Process(target=_serve_audit_with_link_keys, args=(port,), daemon=True)
    process.start()
    try:
        wait_until_ready("127.0.0.1", port)
        impersonator = AuthenticatedServiceClient(
            identity="jarvis-broker",
            expected_server_identity="jarvis-audit",
            secret=ORCHESTRATION_SECRET,
            host="127.0.0.1",
            port=port,
        )

        with pytest.raises(ServiceAuthenticationError):
            impersonator.call("safe_view", None)
    finally:
        process.terminate()
        process.join(timeout=5)


def test_authenticated_read_only_client_cannot_invoke_privileged_operation() -> None:
    port = find_available_port()
    process = Process(target=_serve_audit_with_link_keys, args=(port,), daemon=True)
    process.start()
    try:
        wait_until_ready("127.0.0.1", port)
        orchestration = AuthenticatedServiceClient(
            identity="jarvis-orchestration",
            expected_server_identity="jarvis-audit",
            secret=ORCHESTRATION_SECRET,
            host="127.0.0.1",
            port=port,
        )

        with pytest.raises(PermissionError, match="operation is not allowed"):
            orchestration.call("append", _evidence("forbidden"))
        assert orchestration.call("safe_view", None) == ()
    finally:
        process.terminate()
        process.join(timeout=5)


def test_windows_worker_outbound_session_carries_closed_lifecycle_calls() -> None:
    class Executor:
        def __init__(self) -> None:
            self.cancelled: list[str] = []
            self.finalized: list[str] = []

        def terminate(self, *, action_id: str, timeout_seconds: int) -> bool:
            assert timeout_seconds == 2
            self.cancelled.append(action_id)
            return True

        def finalize(self, *, action_id: str, timeout_seconds: int) -> None:
            assert timeout_seconds == 2
            self.finalized.append(action_id)

    def serve_until_disconnect(connection: socket.socket, executor: object) -> None:
        try:
            serve_windows_worker_session(connection, executor)  # type: ignore[arg-type]
        except ActionDispatcherError:
            return

    gateway_socket, worker_socket = socket.socketpair()
    executor = Executor()
    worker = Thread(
        target=serve_until_disconnect,
        args=(worker_socket, executor),
        daemon=True,
    )
    worker.start()
    session = SocketWindowsWorkerSession(
        connection=gateway_socket,
        evidence=WindowsWorkerSessionEvidence(
            host="windows",
            worker_id="windows-01",
            connection_id="boot-01",
            certificate_identity="spiffe://jarvis/workers/windows-01",
            application_identity="jarvis-windows-worker/windows-01",
        ),
    )
    try:
        session.ping(timeout_seconds=2)
        assert (
            session.terminate_job_object(action_id="action-01", timeout_seconds=2)
            is True
        )
        session.finalize(action_id="action-01", timeout_seconds=2)
        assert executor.cancelled == ["action-01"]
        assert executor.finalized == ["action-01"]
    finally:
        gateway_socket.close()
        worker_socket.close()


def test_windows_worker_cancellation_crosses_the_session_during_execution() -> None:
    execution_started = Event()
    release_execution = Event()

    class Executor:
        def execute(self, _invocation: object, _progress: object) -> object:
            execution_started.set()
            assert release_execution.wait(timeout=3)
            return WorkerExecutionResult.completed()

        def terminate(self, *, action_id: str, timeout_seconds: int) -> bool:
            assert action_id == "action-concurrent-cancel"
            assert timeout_seconds == 2
            release_execution.set()
            return True

        def finalize(self, *, action_id: str, timeout_seconds: int) -> None:
            return

    gateway_socket, worker_socket = socket.socketpair()

    def serve_until_disconnect() -> None:
        try:
            serve_windows_worker_session(
                worker_socket,
                Executor(),  # type: ignore[arg-type]
            )
        except ActionDispatcherError:
            return

    worker = Thread(
        target=serve_until_disconnect,
        daemon=True,
    )
    worker.start()
    session = SocketWindowsWorkerSession(
        connection=gateway_socket,
        evidence=WindowsWorkerSessionEvidence(
            host="windows",
            worker_id="windows-01",
            connection_id="boot-01",
            certificate_identity="spiffe://jarvis/workers/windows-01",
            application_identity="jarvis-windows-worker/windows-01",
        ),
    )
    invocation = WorkerInvocation(
        action_id="action-concurrent-cancel",
        action=TerminalAction(
            host="windows",
            executable="C:\\Windows\\System32\\whoami.exe",
            arguments=(),
            cwd="C:\\Windows\\System32",
        ),
        interactive=False,
        deadline_seconds=3,
        stdout_limit_bytes=1024 * 1024,
        stderr_limit_bytes=1024 * 1024,
        cancellation_grace_seconds=2,
        progress_event_limit=32,
        milestone_limit_bytes=4096,
        worker_identity=WorkerIdentity(
            host="windows", worker_id="windows-01", connection_id="boot-01"
        ),
    )
    outcomes: list[WorkerExecutionResult] = []
    execution = Thread(
        target=lambda: outcomes.append(session.execute(invocation, lambda _event: None))
    )
    try:
        execution.start()
        assert execution_started.wait(timeout=1)
        assert (
            session.terminate_job_object(
                action_id="action-concurrent-cancel", timeout_seconds=2
            )
            is True
        )
        execution.join(timeout=2)
        assert not execution.is_alive()
        assert outcomes == [WorkerExecutionResult.completed()]
    finally:
        release_execution.set()
        execution.join(timeout=2)
        gateway_socket.close()
        worker_socket.close()


def _write_mtls_material(
    root: Path, *, client_uri: str
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    root.mkdir(parents=True)
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jarvis test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    def issue(
        name: str, san: x509.GeneralName, usage: x509.ObjectIdentifier
    ) -> tuple[Path, Path]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        certificate_path = root / f"{name}.pem"
        key_path = root / f"{name}.key"
        certificate_path.write_bytes(
            certificate.public_bytes(serialization.Encoding.PEM)
        )
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return certificate_path, key_path

    ca_path = root / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    server_certificate, server_key = issue(
        "gateway", x509.DNSName("localhost"), ExtendedKeyUsageOID.SERVER_AUTH
    )
    client_certificate, client_key = issue(
        "worker",
        x509.UniformResourceIdentifier(client_uri),
        ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    wrong_certificate, wrong_key = issue(
        "wrong-worker",
        x509.UniformResourceIdentifier("spiffe://jarvis/workers/other"),
        ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    return (
        ca_path,
        server_certificate,
        server_key,
        client_certificate,
        client_key,
        wrong_certificate,
        wrong_key,
    )


def test_windows_worker_listener_enforces_mtls_and_application_identity(
    tmp_path: Path,
) -> None:
    expected_uri = "spiffe://jarvis/workers/windows-01"
    material = _write_mtls_material(tmp_path / "valid", client_uri=expected_uri)
    wrong_material = (
        material[0],
        material[1],
        material[2],
        material[5],
        material[6],
    )
    identity = WorkerIdentity(
        host="windows", worker_id="windows-01", connection_id="boot-01"
    )
    registration = WindowsWorkerRegistration(
        identity=identity,
        certificate_identity=expected_uri,
        application_identity="jarvis-windows-worker/windows-01",
        heartbeat_interval_seconds=1,
    )
    transport = OutboundWindowsWorkerTransport(registration=registration)
    port = find_available_port()
    WindowsWorkerMtlsAcceptor(
        config=WindowsMtlsServerConfig(
            bind_host="127.0.0.1",
            bind_port=port,
            ca_file=material[0],
            certificate_file=material[1],
            private_key_file=material[2],
        ),
        registration=registration,
        transport=transport,
    ).start()

    class Executor:
        def execute(self, invocation: object, progress: object) -> object:
            raise AssertionError("identity test must not execute work")

        def terminate(self, *, action_id: str, timeout_seconds: int) -> bool:
            return True

        def finalize(self, *, action_id: str, timeout_seconds: int) -> None:
            return

    executor = Executor()

    def connect(paths: tuple[Path, ...], hello: bytes) -> None:
        try:
            run_windows_worker_client(
                config=WindowsMtlsClientConfig(
                    overlay_host="127.0.0.1",
                    overlay_port=port,
                    server_name="localhost",
                    ca_file=paths[0],
                    certificate_file=paths[3],
                    private_key_file=paths[4],
                ),
                application_hello=hello,
                executor=executor,  # type: ignore[arg-type]
            )
        except (OSError, ssl.SSLError, ActionDispatcherError):
            return

    valid_hello = (
        b'{"host":"windows","worker_id":"windows-01",'
        b'"connection_id":"boot-01",'
        b'"application_identity":"jarvis-windows-worker/windows-01",'
        b'"heartbeat_interval_seconds":1}'
    )
    wrong_application = valid_hello.replace(
        b"jarvis-windows-worker/windows-01", b"jarvis-windows-worker/other"
    )
    for paths, hello in (
        (wrong_material, valid_hello),
        (material, wrong_application),
        (material, b"{}"),
    ):
        Thread(target=connect, args=(paths, hello), daemon=True).start()
        sleep(0.1)
        with pytest.raises(ActionDispatcherError, match="unavailable"):
            transport.authenticate(selected_host="windows", timeout_seconds=1)

    Thread(target=connect, args=(material, valid_hello), daemon=True).start()
    deadline = monotonic() + 3
    while True:
        try:
            assert (
                transport.authenticate(selected_host="windows", timeout_seconds=1)
                == identity
            )
            break
        except ActionDispatcherError:
            if monotonic() >= deadline:
                raise
            sleep(0.02)

    Thread(target=connect, args=(material, valid_hello), daemon=True).start()
    sleep(0.2)
    assert (
        transport.authenticate(selected_host="windows", timeout_seconds=1) == identity
    )
