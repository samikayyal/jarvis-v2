from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from jarvis_control_plane import (
    ActionCancellationStatus,
    ActionDispatcherError,
    ControlledWindowsWorkerSession,
    OutboundWindowsWorkerTransport,
    WindowsMtlsClientConfig,
    WindowsWorkerRegistration,
    WindowsWorkerSessionEvidence,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
)
from jarvis_control_plane.terminal_policy import TerminalAction

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


def _invocation(action_id: str = "action-001") -> WorkerInvocation:
    return WorkerInvocation(
        action_id=action_id,
        action=TerminalAction(
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
    transport = OutboundWindowsWorkerTransport(registration=REGISTRATION)
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


def test_offline_disconnect_and_heartbeat_expiry_are_unavailable_without_queueing() -> (
    None
):
    now = [100.0]
    transport = OutboundWindowsWorkerTransport(
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
    transport = OutboundWindowsWorkerTransport(registration=REGISTRATION)
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
    transport = OutboundWindowsWorkerTransport(registration=REGISTRATION)
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
    transport = OutboundWindowsWorkerTransport(registration=REGISTRATION)
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


def test_disconnect_during_started_action_returns_unknown_after_reconnect() -> None:
    started = Event()
    release = Event()

    def execute(_invocation: WorkerInvocation) -> WorkerExecutionResult:
        started.set()
        assert release.wait(timeout=5)
        return WorkerExecutionResult.completed(stdout="may have completed")

    first = ControlledWindowsWorkerSession(evidence=_evidence(), execution_hook=execute)
    transport = OutboundWindowsWorkerTransport(registration=REGISTRATION)
    transport.attach(first)
    transport.register_execution(
        action_id="ambiguous", timeout_seconds=1, retention_seconds=60
    )

    errors: list[BaseException] = []

    def run() -> None:
        try:
            transport.execute(_invocation("ambiguous"), lambda _event: None)
        except BaseException as exc:  # noqa: BLE001 - assertion captures boundary
            errors.append(exc)

    worker = Thread(target=run)
    worker.start()
    try:
        assert started.wait(timeout=5)
        transport.disconnect(first)
        transport.attach(ControlledWindowsWorkerSession(evidence=_evidence()))
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ActionDispatcherError)
    assert errors[0].may_have_dispatched is True
