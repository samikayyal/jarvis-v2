from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

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


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_failed_job_assignment_terminates_and_reaps_the_suspended_child() -> None:
    class FailingAssignmentExecutor(SubprocessWindowsJobObjectExecutor):
        spawned: subprocess.Popen[bytes] | None = None

        def _assign_process(
            self, job_handle: int, process: subprocess.Popen[bytes]
        ) -> None:
            del job_handle
            self.spawned = process
            raise OSError("controlled assignment failure")

    executor = FailingAssignmentExecutor()

    with pytest.raises(OSError, match="controlled assignment failure"):
        executor.execute(_invocation("assignment-failure"), lambda _event: None)

    assert executor.spawned is not None
    assert executor.spawned.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_native_job_executor_preserves_structured_compound_component_progress() -> None:
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "print('first')"),
        cwd=str(Path.cwd()),
        components=(
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('first')"],
            },
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('second')"],
                "operator_before": "&&",
            },
        ),
    )

    result = SubprocessWindowsJobObjectExecutor().execute(
        _invocation("compound", action=action), lambda _event: None
    )

    assert result.status is WorkerExecutionStatus.COMPLETED
    assert result.started_components == (0, 1)
    assert result.completed_components == (0, 1)
    assert result.stdout.splitlines() == ["first", "second"]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_native_job_executor_preserves_pipeline_and_redirection_structure(
    tmp_path: Path,
) -> None:
    redirected = tmp_path / "redirected.txt"
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "print('pipe me')"),
        cwd=str(tmp_path),
        components=(
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('pipe me')"],
            },
            {
                "executable": sys.executable,
                "arguments": [
                    "-c",
                    "import sys; print(sys.stdin.read().upper(), end='')",
                ],
                "operator_before": "|",
                "redirections": [str(redirected)],
            },
        ),
    )

    result = SubprocessWindowsJobObjectExecutor().execute(
        _invocation("pipeline-redirection", action=action), lambda _event: None
    )

    assert result.status is WorkerExecutionStatus.COMPLETED
    assert result.started_components == (0, 1)
    assert result.completed_components == (0, 1)
    assert result.stdout == ""
    assert redirected.read_text() == "PIPE ME\n"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows reparse paths")
def test_native_redirection_rechecks_the_frozen_reparse_target(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    redirected = linked / "out.txt"
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "print('blocked')"),
        cwd=str(tmp_path),
        components=(
            {
                "executable": sys.executable,
                "arguments": ["-c", "print('blocked')"],
                "redirections": [str(redirected)],
            },
        ),
    )

    with pytest.raises(ActionDispatcherError, match="changed through a reparse path"):
        SubprocessWindowsJobObjectExecutor().execute(
            _invocation("reparse-redirection", action=action), lambda _event: None
        )

    assert not (actual / "out.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_native_output_overflow_is_visibly_marked_and_traceable() -> None:
    action = TerminalAction(
        host="windows",
        executable=sys.executable,
        arguments=("-c", "print('x' * 100)"),
        cwd=str(Path.cwd()),
    )
    invocation = replace(_invocation("truncated", action=action), stdout_limit_bytes=32)
    events = []

    result = SubprocessWindowsJobObjectExecutor().execute(invocation, events.append)

    assert len(result.stdout.encode()) <= 32
    assert result.stdout.endswith("[truncated]")
    assert result.stdout_truncated is True
    assert events[0].truncated is True


def test_disconnect_during_started_action_returns_unknown_after_reconnect() -> None:
    started = Event()
    release = Event()

    def execute(_invocation: WorkerInvocation) -> WorkerExecutionResult:
        started.set()
        assert release.wait(timeout=5)
        return WorkerExecutionResult.completed(stdout="may have completed")

    first = ControlledWindowsWorkerSession(evidence=_evidence(), execution_hook=execute)
    transport = ControlledOutboundWindowsWorkerTransport(registration=REGISTRATION)
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
