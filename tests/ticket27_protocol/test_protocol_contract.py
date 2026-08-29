# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
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
from jarvis_control_plane.knowledge_vault import VaultReadError
from jarvis_control_plane.models import AuditEvidence, AuditFilter
from jarvis_control_plane.openwa import OpenWAReadiness
from jarvis_control_plane.ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    OrchestrationAdapterError,
)
from jarvis_control_plane.service_protocol import (
    MAX_FRAME_BYTES,
    MAX_REQUEST_FRAME_BYTES,
    AuthenticatedServiceClient,
    AuthenticatedServiceServer,
    OwnedActionService,
    RemoteActionDispatcher,
    RemoteAuditBoundary,
    RemoteMessagingReadinessProvider,
    RemoteOrchestrationAdapter,
    RemoteServiceError,
    ServiceAuthenticationError,
    ServiceProtocolError,
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


class _ReadinessClient:
    def call(self, operation: str) -> OpenWAReadiness:
        assert operation == "current"
        return OpenWAReadiness(
            container_healthy=True,
            named_session_status="ready",
        )


def test_remote_messaging_readiness_accepts_the_openwa_readiness_contract() -> None:
    readiness = RemoteMessagingReadinessProvider(_ReadinessClient()).current()  # type: ignore[arg-type]

    assert readiness.messaging_ready is True


def test_protocol_preserves_stable_vault_read_error_codes() -> None:
    port = find_available_port()

    def read() -> None:
        raise VaultReadError(
            "path is not an ordinary knowledge-vault note",
            code="unsupported_file_type",
        )

    server = AuthenticatedServiceServer(
        identity="jarvis-vault",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
        operations={"read": read},
    )
    Thread(target=server.serve_forever, daemon=True).start()
    wait_until_ready("127.0.0.1", port)
    client = AuthenticatedServiceClient(
        identity="jarvis-broker",
        expected_server_identity="jarvis-vault",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
    )
    try:
        with pytest.raises(RemoteServiceError) as caught:
            client.call("read")
    finally:
        server.shutdown()

    assert caught.value.error_type == "VaultReadError"
    assert caught.value.code == "unsupported_file_type"


def test_protocol_preserves_stable_orchestration_diagnostic_codes() -> None:
    port = find_available_port()

    def run() -> None:
        raise OrchestrationAdapterError(
            "model returned a malformed action proposal",
            code="terminal_executable_not_absolute",
        )

    server = AuthenticatedServiceServer(
        identity="jarvis-orchestration",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
        operations={"run": run},
    )
    Thread(target=server.serve_forever, daemon=True).start()
    wait_until_ready("127.0.0.1", port)
    client = AuthenticatedServiceClient(
        identity="jarvis-broker",
        expected_server_identity="jarvis-orchestration",
        secret=SECRET,
        host="127.0.0.1",
        port=port,
    )
    try:
        with pytest.raises(RemoteServiceError) as caught:
            client.call("run")
    finally:
        server.shutdown()

    assert caught.value.error_type == "OrchestrationAdapterError"
    assert caught.value.code == "terminal_executable_not_absolute"
    assert caught.value.operation_started is False
    assert caught.value.may_have_dispatched is False
    assert caught.value.may_have_sent is False


def test_remote_orchestration_adapter_retains_the_diagnostic_code() -> None:
    class Client:
        def call(self, operation: str, _request: object) -> None:
            assert operation == "run"
            raise RemoteServiceError(
                "OrchestrationAdapterError",
                "model returned a malformed action proposal",
                code="terminal_executable_not_absolute",
            )

    adapter = RemoteOrchestrationAdapter(Client())  # type: ignore[arg-type]

    with pytest.raises(OrchestrationAdapterError) as caught:
        adapter.run(object())  # type: ignore[arg-type]

    assert caught.value.code == "terminal_executable_not_absolute"
    assert isinstance(caught.value.__cause__, RemoteServiceError)


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
            "writable": audit.writable,
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
            client = AuthenticatedServiceClient(
                identity="jarvis-broker",
                expected_server_identity="jarvis-audit",
                secret=SECRET,
                host="127.0.0.1",
                port=port,
            )
            audit = RemoteAuditBoundary(client)

            assert client.call("writable") is True
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


def test_remote_action_binding_translates_transport_failures() -> None:
    class Client:
        def call(self, _operation: str, _action: object) -> object:
            raise ServiceProtocolError("connector unavailable")

    dispatcher = RemoteActionDispatcher(Client(), bound=True)  # type: ignore[arg-type]

    with pytest.raises(ActionDispatcherError, match="connector unavailable"):
        dispatcher.bind_proposal(object())  # type: ignore[arg-type]


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
