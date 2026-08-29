# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from jarvis_control_plane import (
    ActionCancellationStatus,
    ActionDispatcherError,
    ControlledOutboundWindowsWorkerTransport,
    ControlledWindowsWorkerSession,
    OutboundWindowsWorkerTransport,
    SubprocessWindowsJobObjectExecutor,
    WindowsMtlsClientConfig,
    WindowsWorkerRegistration,
    WindowsWorkerSessionEvidence,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    authenticate_windows_worker_session,
    open_windows_worker_mtls_session,
)
from jarvis_control_plane import windows_worker as windows_worker_module
from jarvis_control_plane.terminal_policy import TerminalAction
from jarvis_control_plane.windows_worker_session import SocketWindowsWorkerSession

WINDOWS_IDENTITY = WorkerIdentity(
    host="windows", worker_id="windows-01", connection_id="boot-01"
)
REGISTRATION = WindowsWorkerRegistration(
    identity=WINDOWS_IDENTITY,
    certificate_identity="spiffe://jarvis/workers/windows-01",
    application_identity="jarvis-windows-worker/windows-01",
)


def _evidence(**changes: str) -> WindowsWorkerSessionEvidence:
    values = {
        "host": "windows",
        "worker_id": "windows-01",
        "connection_id": "boot-01",
        "certificate_identity": "spiffe://jarvis/workers/windows-01",
        "application_identity": "jarvis-windows-worker/windows-01",
    }
    values.update(changes)
    return WindowsWorkerSessionEvidence(**values)


def _invocation(
    action_id: str = "action-001", *, action: TerminalAction | None = None
) -> WorkerInvocation:
    return WorkerInvocation(
        action_id=action_id,
        action=action
        or TerminalAction(
            host="windows",
            executable="C:\\Windows\\System32\\whoami.exe",
            arguments=(),
            cwd="C:\\Windows\\System32",
        ),
        interactive=False,
        deadline_seconds=30,
        stdout_limit_bytes=1024 * 1024,
        stderr_limit_bytes=1024 * 1024,
        cancellation_grace_seconds=5,
        progress_event_limit=32,
        milestone_limit_bytes=4096,
        worker_identity=WINDOWS_IDENTITY,
    )


def test_terminal_identity_rejects_a_relative_redirection_target() -> None:
    with pytest.raises(ValueError, match="redirection target must be canonical"):
        TerminalAction(
            host="windows",
            executable="C:\\Windows\\System32\\whoami.exe",
            arguments=(),
            cwd="C:\\Windows\\System32",
            components=(
                {
                    "executable": "C:\\Windows\\System32\\whoami.exe",
                    "arguments": [],
                    "redirections": ["relative.txt"],
                },
            ),
        )


def test_heartbeat_interval_uses_ten_seconds_and_rejects_above_hard_maximum() -> None:
    assert REGISTRATION.heartbeat_interval_seconds == 10

    with pytest.raises(ValueError, match="between one and 15 seconds"):
        WindowsWorkerRegistration(
            identity=WINDOWS_IDENTITY,
            certificate_identity="spiffe://jarvis/workers/windows-01",
            application_identity="jarvis-windows-worker/windows-01",
            heartbeat_interval_seconds=16,
        )

    with pytest.raises(ValueError, match="cover two heartbeat intervals"):
        ControlledOutboundWindowsWorkerTransport(
            registration=REGISTRATION, readiness_expiry_seconds=19
        )


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("certificate_identity", "spiffe://jarvis/workers/attacker"),
        ("application_identity", "jarvis-windows-worker/attacker"),
        ("host", "ubuntu"),
        ("worker_id", "windows-02"),
        ("connection_id", "boot-02"),
    ],
)
def test_session_requires_matching_certificate_and_application_identity(
    field: str, wrong_value: str
) -> None:
    transport = ControlledOutboundWindowsWorkerTransport(registration=REGISTRATION)
    session = ControlledWindowsWorkerSession(evidence=_evidence(**{field: wrong_value}))

    with pytest.raises(ActionDispatcherError, match="identity mismatch"):
        transport.attach(session)

    with pytest.raises(ActionDispatcherError, match="unavailable"):
        transport.authenticate(selected_host="windows", timeout_seconds=1)


def test_mtls_configuration_requires_absolute_credentials_and_bounded_endpoint() -> (
    None
):
    config = WindowsMtlsClientConfig(
        overlay_host="100.64.0.10",
        overlay_port=8443,
        server_name="worker-gateway.jarvis.internal",
        ca_file=Path("C:/Jarvis/credentials/worker-ca.pem"),
        certificate_file=Path("C:/Jarvis/credentials/windows-01.pem"),
        private_key_file=Path("C:/Jarvis/credentials/windows-01.key"),
    )

    assert config.overlay_host == "100.64.0.10"
    assert config.overlay_port == 8443

    with pytest.raises(ValueError, match="must be absolute"):
        WindowsMtlsClientConfig(
            overlay_host="100.64.0.10",
            overlay_port=8443,
            server_name="worker-gateway.jarvis.internal",
            ca_file=Path("relative-ca.pem"),
            certificate_file=config.certificate_file,
            private_key_file=config.private_key_file,
        )


def test_mtls_connect_timeout_is_cleared_after_the_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRawSocket:
        def close(self) -> None:
            raise AssertionError("successful handshake must not close the raw socket")

    class FakeTlsSocket:
        def __init__(self) -> None:
            self.timeouts: list[float | None] = []

        def settimeout(self, value: float | None) -> None:
            self.timeouts.append(value)

    tls = FakeTlsSocket()

    class FakeContext:
        minimum_version: object
        maximum_version: object
        verify_mode: object
        check_hostname: bool

        def load_verify_locations(self, *, cafile: str) -> None:
            assert cafile.endswith("ca.pem")

        def load_cert_chain(self, *, certfile: str, keyfile: str) -> None:
            assert certfile.endswith("worker.pem")
            assert keyfile.endswith("worker.key")

        def wrap_socket(
            self, raw: FakeRawSocket, *, server_hostname: str
        ) -> FakeTlsSocket:
            assert server_hostname == "worker-gateway.jarvis.internal"
            return tls

    monkeypatch.setattr(
        windows_worker_module.ssl, "SSLContext", lambda _: FakeContext()
    )
    monkeypatch.setattr(
        windows_worker_module.socket,
        "create_connection",
        lambda *_args, **_kwargs: FakeRawSocket(),
    )
    config = WindowsMtlsClientConfig(
        overlay_host="100.64.0.10",
        overlay_port=8443,
        server_name="worker-gateway.jarvis.internal",
        ca_file=Path("C:/Jarvis/credentials/ca.pem"),
        certificate_file=Path("C:/Jarvis/credentials/worker.pem"),
        private_key_file=Path("C:/Jarvis/credentials/worker.key"),
    )

    assert open_windows_worker_mtls_session(config) is tls
    assert tls.timeouts == [None]


def test_production_transport_accepts_only_certificate_bound_application_hello() -> (
    None
):
    class FakeTlsSocket:
        def version(self) -> str:
            return "TLSv1.3"

        def getpeercert(self) -> dict[str, object]:
            return {"subjectAltName": (("URI", "spiffe://jarvis/workers/windows-01"),)}

    self_asserted = ControlledWindowsWorkerSession(evidence=_evidence())
    transport = OutboundWindowsWorkerTransport(registration=REGISTRATION)
    with pytest.raises(ActionDispatcherError, match="not mTLS authenticated"):
        transport.attach(self_asserted)

    evidence = authenticate_windows_worker_session(
        registration=REGISTRATION,
        tls_socket=FakeTlsSocket(),  # type: ignore[arg-type]
        application_hello=(
            b'{"host":"windows","worker_id":"windows-01",'
            b'"connection_id":"boot-01",'
            b'"application_identity":"jarvis-windows-worker/windows-01",'
            b'"heartbeat_interval_seconds":10}'
        ),
    )
    transport.attach(ControlledWindowsWorkerSession(evidence=evidence))

    assert transport.authenticate(selected_host="windows", timeout_seconds=1) == (
        WINDOWS_IDENTITY
    )


def test_authenticated_session_rejects_certificate_or_closed_hello_mismatch() -> None:
    class WrongCertificateTlsSocket:
        def version(self) -> str:
            return "TLSv1.3"

        def getpeercert(self) -> dict[str, object]:
            return {"subjectAltName": (("URI", "spiffe://jarvis/workers/other"),)}

    with pytest.raises(ActionDispatcherError, match="certificate identity mismatch"):
        authenticate_windows_worker_session(
            registration=REGISTRATION,
            tls_socket=WrongCertificateTlsSocket(),  # type: ignore[arg-type]
            application_hello=b"{}",
        )


def test_attachment_rechecks_the_authenticated_heartbeat_registration() -> None:
    class FakeTlsSocket:
        def version(self) -> str:
            return "TLSv1.3"

        def getpeercert(self) -> dict[str, object]:
            return {"subjectAltName": (("URI", "spiffe://jarvis/workers/windows-01"),)}

    fifteen_second_registration = WindowsWorkerRegistration(
        identity=WINDOWS_IDENTITY,
        certificate_identity=REGISTRATION.certificate_identity,
        application_identity=REGISTRATION.application_identity,
        heartbeat_interval_seconds=15,
    )
    evidence = authenticate_windows_worker_session(
        registration=fifteen_second_registration,
        tls_socket=FakeTlsSocket(),  # type: ignore[arg-type]
        application_hello=(
            b'{"host":"windows","worker_id":"windows-01",'
            b'"connection_id":"boot-01",'
            b'"application_identity":"jarvis-windows-worker/windows-01",'
            b'"heartbeat_interval_seconds":15}'
        ),
    )
    transport = OutboundWindowsWorkerTransport(registration=REGISTRATION)

    with pytest.raises(ActionDispatcherError, match="session identity mismatch"):
        transport.attach(ControlledWindowsWorkerSession(evidence=evidence))


def test_offline_disconnect_and_heartbeat_expiry_are_unavailable_without_queueing() -> (
    None
):
    now = [100.0]
    transport = ControlledOutboundWindowsWorkerTransport(
        registration=REGISTRATION,
        readiness_expiry_seconds=30,
        clock=lambda: now[0],
    )

    with pytest.raises(ActionDispatcherError, match="unavailable"):
        transport.register_execution(
            action_id="offline", timeout_seconds=1, retention_seconds=60
        )

    first = ControlledWindowsWorkerSession(evidence=_evidence())
    transport.attach(first)
    assert transport.authenticate(selected_host="windows", timeout_seconds=1) == (
        WINDOWS_IDENTITY
    )

    now[0] += 31
    with pytest.raises(ActionDispatcherError, match="heartbeat expired"):
        transport.register_execution(
            action_id="expired", timeout_seconds=1, retention_seconds=60
        )

    transport.heartbeat(first)
    transport.disconnect(first)
    with pytest.raises(ActionDispatcherError, match="unavailable"):
        transport.register_execution(
            action_id="disconnected", timeout_seconds=1, retention_seconds=60
        )


def test_reconnect_never_runs_an_action_reserved_on_the_disconnected_session() -> None:
    transport = ControlledOutboundWindowsWorkerTransport(registration=REGISTRATION)
    first = ControlledWindowsWorkerSession(evidence=_evidence())
    transport.attach(first)
    transport.register_execution(
        action_id="stale", timeout_seconds=1, retention_seconds=60
    )
    transport.disconnect(first)

    replacement = ControlledWindowsWorkerSession(evidence=_evidence())
    transport.attach(replacement)

    with pytest.raises(ActionDispatcherError, match="reserved session disconnected"):
        transport.execute(_invocation("stale"), lambda _event: None)

    assert replacement.invocations == []


def test_windows_worker_runs_one_noninteractive_job_object_action_at_a_time() -> None:
    started = Event()
    release = Event()

    def execute(invocation: WorkerInvocation) -> WorkerExecutionResult:
        assert invocation.interactive is False
        started.set()
        assert release.wait(timeout=5)
        return WorkerExecutionResult.completed(stdout="windows-01\\operator\n")

    session = ControlledWindowsWorkerSession(
        evidence=_evidence(), execution_hook=execute
    )
    transport = ControlledOutboundWindowsWorkerTransport(registration=REGISTRATION)
    transport.attach(session)
    transport.register_execution(
        action_id="running", timeout_seconds=1, retention_seconds=60
    )

    result: list[WorkerExecutionResult] = []
    worker = Thread(
        target=lambda: result.append(
            transport.execute(_invocation("running"), lambda _event: None)
        )
    )
    worker.start()
    try:
        assert started.wait(timeout=5)
        with pytest.raises(ActionDispatcherError, match="already has an action"):
            transport.register_execution(
                action_id="queued", timeout_seconds=1, retention_seconds=60
            )
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert result[0].status is WorkerExecutionStatus.COMPLETED
    assert [item.action_id for item in session.invocations] == ["running"]


def test_windows_session_execute_timeout_includes_cleanup_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, int] = {}

    def call(
        _self: object,
        _operation: str,
        _arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> object:
        captured["timeout_seconds"] = timeout_seconds
        raise RuntimeError("stop after capturing timeout")

    monkeypatch.setattr(SocketWindowsWorkerSession, "_call", call)
    invocation = replace(_invocation("cleanup-timeout"), cancellation_grace_seconds=10)

    with pytest.raises(RuntimeError, match="capturing timeout"):
        object.__new__(SocketWindowsWorkerSession).execute(
            invocation, lambda _event: None
        )

    assert captured["timeout_seconds"] == 40


def test_cancellation_reports_stopped_only_after_job_object_tree_termination() -> None:
    started = Event()
    release = Event()

    def execute(_invocation: WorkerInvocation) -> WorkerExecutionResult:
        started.set()
        assert release.wait(timeout=5)
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.CANCELLED,
            process_tree_stopped=True,
        )

    session = ControlledWindowsWorkerSession(
        evidence=_evidence(), execution_hook=execute
    )
    transport = ControlledOutboundWindowsWorkerTransport(registration=REGISTRATION)
    transport.attach(session)
    transport.register_execution(
        action_id="cancel", timeout_seconds=1, retention_seconds=60
    )

    outcome: list[WorkerExecutionResult] = []
    worker = Thread(
        target=lambda: outcome.append(
            transport.execute(_invocation("cancel"), lambda _event: None)
        )
    )
    worker.start()
    try:
        assert started.wait(timeout=5)
        cancelled = transport.cancel(
            action_id="cancel", timeout_seconds=5, retention_seconds=60
        )
        assert cancelled.status is ActionCancellationStatus.STOPPED
        assert session.job_object_terminations == ["cancel"]
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
