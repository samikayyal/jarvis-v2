from __future__ import annotations

import os
import socket
import sys
from threading import Event, Thread

import pytest

from jarvis_control_plane import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ControlledUbuntuLocalAuthenticator,
    ControlledUbuntuProcessScope,
    FrozenActionProposal,
    SystemdUbuntuProcessScope,
    UbuntuLocalPeerIdentity,
    UbuntuWorkerReadiness,
    UbuntuWorkerTransport,
    UnixSocketUbuntuLocalAuthenticator,
    WorkerExecutionError,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
    WorkerIdentity,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
)


def _local_peer(
    *,
    peer_uid: int = 1100,
    socket_path: str = "/run/jarvis/ubuntu-worker.sock",
    connection_id: str = "local-boot-01",
) -> UbuntuLocalPeerIdentity:
    return UbuntuLocalPeerIdentity(
        peer_pid=42,
        peer_uid=peer_uid,
        peer_gid=1100,
        socket_path=socket_path,
        socket_owner_uid=1200,
        socket_mode=0o600,
        connection_id=connection_id,
    )


def _worker(
    *,
    peer: UbuntuLocalPeerIdentity | None = None,
    readiness: UbuntuWorkerReadiness = UbuntuWorkerReadiness.READY,
    process_scope: ControlledUbuntuProcessScope | None = None,
) -> UbuntuWorkerTransport:
    return UbuntuWorkerTransport(
        worker_id="ubuntu-01",
        expected_peer_uid=1100,
        expected_socket_owner_uid=1200,
        expected_socket_path="/run/jarvis/ubuntu-worker.sock",
        authenticator=ControlledUbuntuLocalAuthenticator(peer or _local_peer()),
        readiness=lambda: readiness,
        process_scope=process_scope or ControlledUbuntuProcessScope(),
    )


def _proposal(action_id: str = "action-ubuntu-001") -> FrozenActionProposal:
    return FrozenActionProposal.create(
        action_id=action_id,
        request_id="request-ubuntu-001",
        kind="terminal",
        preview="Print controlled output.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/printf",
            "arguments": ["hello"],
            "cwd": "/workspace",
        },
    )


def test_ubuntu_worker_authenticates_one_ready_local_host_identity() -> None:
    worker = _worker()

    identity = worker.authenticate(selected_host="ubuntu", timeout_seconds=10)

    assert identity.host == "ubuntu"
    assert identity.worker_id == "ubuntu-01"
    assert identity.connection_id == "local-boot-01"


@pytest.mark.parametrize(
    ("worker", "selected_host", "error"),
    [
        (_worker(), "windows", "bound only to ubuntu"),
        (_worker(peer=_local_peer(peer_uid=9999)), "ubuntu", "local peer identity"),
        (
            _worker(peer=_local_peer(socket_path="/run/other.sock")),
            "ubuntu",
            "local peer identity",
        ),
        (
            _worker(readiness=UbuntuWorkerReadiness.DEGRADED),
            "ubuntu",
            "degraded",
        ),
        (
            _worker(readiness=UbuntuWorkerReadiness.UNAVAILABLE),
            "ubuntu",
            "unavailable",
        ),
    ],
)
def test_ubuntu_worker_rejects_wrong_host_peer_or_readiness(
    worker: UbuntuWorkerTransport, selected_host: str, error: str
) -> None:
    with pytest.raises(ActionDispatcherError, match=error):
        worker.authenticate(selected_host=selected_host, timeout_seconds=10)


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERCRED is Linux-specific")
def test_local_authenticator_uses_peer_credentials_and_restricted_socket(
    tmp_path: object,
) -> None:
    path = os.fspath(tmp_path) + "/ubuntu-worker.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(path)
        os.chmod(path, 0o600)
        listener.listen(1)
        client.connect(path)
        connection, _ = listener.accept()
        try:
            authenticator = UnixSocketUbuntuLocalAuthenticator(
                connection=connection,
                socket_path=path,
                connection_id="local-socket-01",
            )

            identity = authenticator.authenticate(timeout_seconds=10)

            assert identity.peer_uid == os.getuid()
            assert identity.socket_owner_uid == os.getuid()
            assert identity.socket_mode == 0o600
        finally:
            connection.close()
    finally:
        client.close()
        listener.close()


def test_ubuntu_worker_runs_one_bounded_noninteractive_scope_with_tagged_output() -> (
    None
):
    process_scope = ControlledUbuntuProcessScope(
        result=WorkerExecutionResult.completed(stdout="hello", stderr="warning"),
        progress_events=(
            WorkerProgressEvent(
                sequence=2,
                kind=WorkerProgressKind.MILESTONE,
                text="scope-started",
            ),
            WorkerProgressEvent(
                sequence=3,
                kind=WorkerProgressKind.OUTPUT,
                stream=WorkerOutputStream.STDOUT,
                text="hello",
            ),
            WorkerProgressEvent(
                sequence=4,
                kind=WorkerProgressKind.OUTPUT,
                stream=WorkerOutputStream.STDERR,
                text="warning",
            ),
        ),
    )
    worker = _worker(process_scope=process_scope)
    identity = WorkerIdentity(
        host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker}, registered_identities={"ubuntu": identity}
    )

    result = gateway.dispatch(_proposal())

    assert result.status.value == "completed"
    assert [event.stream for event in result.progress_events[2:]] == [
        WorkerOutputStream.STDOUT,
        WorkerOutputStream.STDERR,
    ]
    assert len(process_scope.invocations) == 1
    invocation = process_scope.invocations[0]
    assert invocation.interactive is False
    assert invocation.deadline_seconds == 120
    assert invocation.stdout_limit_bytes == 1024 * 1024
    assert invocation.stderr_limit_bytes == 1024 * 1024


def test_systemd_scope_is_noninteractive_bounded_and_never_uses_a_shell() -> None:
    scope = SystemdUbuntuProcessScope(
        systemd_run_path="/usr/bin/systemd-run",
        systemctl_path="/usr/bin/systemctl",
        process_limit=32,
    )
    invocation = _invocation(
        "action-ubuntu-command",
        WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )

    command = scope.command_for(invocation)

    assert command[0] == "/usr/bin/systemd-run"
    assert "--property=TasksMax=32" in command
    assert "--property=NoNewPrivileges=yes" in command
    assert "--pipe" in command
    assert "--wait" in command
    assert command[-3:] == ("--", "/usr/bin/printf", "hello")
    assert all("docker" not in argument for argument in command)


@pytest.mark.parametrize("process_limit", [0, 65, True])
def test_systemd_scope_rejects_an_invalid_process_tree_bound(
    process_limit: object,
) -> None:
    with pytest.raises((TypeError, ValueError), match="process limit"):
        SystemdUbuntuProcessScope(process_limit=process_limit)  # type: ignore[arg-type]


def test_ubuntu_worker_cancellation_stops_the_active_process_scope() -> None:
    started = Event()
    release = Event()

    def execute(_invocation: object) -> WorkerExecutionResult:
        started.set()
        assert release.wait(timeout=10)
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.CANCELLED,
            started_components=(0,),
            process_tree_stopped=True,
        )

    def cancel(_action_id: str, _timeout_seconds: int) -> ActionCancellationResult:
        release.set()
        return ActionCancellationResult(ActionCancellationStatus.STOPPED)

    process_scope = ControlledUbuntuProcessScope(
        execution_hook=execute, cancellation_hook=cancel
    )
    worker = _worker(process_scope=process_scope)
    identity = WorkerIdentity(
        host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker}, registered_identities={"ubuntu": identity}
    )
    handle = gateway.prepare(_proposal("action-ubuntu-cancel"))
    failures: list[BaseException] = []

    def run() -> None:
        try:
            handle.run()
        except BaseException as exc:  # noqa: BLE001 - test observes worker boundary
            failures.append(exc)

    thread = Thread(target=run)
    thread.start()
    assert started.wait(timeout=10)

    cancellation = handle.cancel()
    thread.join(timeout=10)

    assert cancellation.status is ActionCancellationStatus.STOPPED
    assert not thread.is_alive()
    assert process_scope.cancellations == [("action-ubuntu-cancel", 10)]
    assert failures
    assert isinstance(failures[0], WorkerExecutionError)
    assert failures[0].result.process_tree_stopped is True


def test_ubuntu_worker_rejects_a_second_action_while_its_scope_is_active() -> None:
    started = Event()
    release = Event()

    def execute(_invocation: object) -> WorkerExecutionResult:
        started.set()
        assert release.wait(timeout=10)
        return WorkerExecutionResult.completed()

    process_scope = ControlledUbuntuProcessScope(execution_hook=execute)
    worker = _worker(process_scope=process_scope)
    identity = worker.authenticate(selected_host="ubuntu", timeout_seconds=10)
    first = _invocation("action-ubuntu-first", identity)
    second = _invocation("action-ubuntu-second", identity)
    worker.register_execution(
        action_id=first.action_id, timeout_seconds=10, retention_seconds=900
    )
    worker.register_execution(
        action_id=second.action_id, timeout_seconds=10, retention_seconds=900
    )
    first_result: list[WorkerExecutionResult] = []
    thread = Thread(
        target=lambda: first_result.append(worker.execute(first, lambda _event: None))
    )
    thread.start()
    assert started.wait(timeout=10)

    with pytest.raises(ActionDispatcherError, match="busy"):
        worker.execute(second, lambda _event: None)

    release.set()
    thread.join(timeout=10)
    assert first_result[0].status is WorkerExecutionStatus.COMPLETED


def test_ubuntu_worker_rechecks_the_local_connection_before_process_start() -> None:
    authenticator = ControlledUbuntuLocalAuthenticator(_local_peer())
    process_scope = ControlledUbuntuProcessScope()
    worker = UbuntuWorkerTransport(
        worker_id="ubuntu-01",
        expected_peer_uid=1100,
        expected_socket_owner_uid=1200,
        expected_socket_path="/run/jarvis/ubuntu-worker.sock",
        authenticator=authenticator,
        readiness=lambda: UbuntuWorkerReadiness.READY,
        process_scope=process_scope,
    )
    identity = worker.authenticate(selected_host="ubuntu", timeout_seconds=10)
    invocation = _invocation("action-ubuntu-rebind", identity)
    worker.register_execution(
        action_id=invocation.action_id,
        timeout_seconds=10,
        retention_seconds=900,
    )
    authenticator.identity = _local_peer(connection_id="local-boot-02")

    with pytest.raises(ActionDispatcherError, match="connection identity changed"):
        worker.execute(invocation, lambda _event: None)

    assert process_scope.invocations == []


def test_ubuntu_worker_bounds_each_terminal_output_independently() -> None:
    oversized = "x" * (1024 * 1024 + 1)
    process_scope = ControlledUbuntuProcessScope(
        result=WorkerExecutionResult.completed(stdout=oversized, stderr=oversized)
    )
    worker = _worker(process_scope=process_scope)
    identity = worker.authenticate(selected_host="ubuntu", timeout_seconds=10)
    invocation = _invocation("action-ubuntu-output-bounds", identity)
    worker.register_execution(
        action_id=invocation.action_id,
        timeout_seconds=10,
        retention_seconds=900,
    )

    result = worker.execute(invocation, lambda _event: None)

    assert len(result.stdout.encode()) == 1024 * 1024
    assert len(result.stderr.encode()) == 1024 * 1024
    assert result.stdout.endswith("[output truncated]")
    assert result.stderr.endswith("[output truncated]")


def _invocation(action_id: str, identity: WorkerIdentity):
    from jarvis_control_plane.terminal_policy import TerminalAction
    from jarvis_control_plane.worker_gateway import WorkerInvocation

    return WorkerInvocation(
        action_id=action_id,
        action=TerminalAction(
            host="ubuntu",
            executable="/usr/bin/printf",
            arguments=("hello",),
            cwd="/workspace",
        ),
        interactive=False,
        deadline_seconds=120,
        stdout_limit_bytes=1024 * 1024,
        stderr_limit_bytes=1024 * 1024,
        cancellation_grace_seconds=10,
        progress_event_limit=128,
        milestone_limit_bytes=4096,
        worker_identity=identity,
    )
