from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic, sleep
from typing import Protocol, cast

import pytest

import jarvis_control_plane.ubuntu_worker as ubuntu_worker_module
from jarvis_control_plane import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ControlledUbuntuLocalAuthenticator,
    ControlledUbuntuProcessScope,
    FrozenActionProposal,
    SystemdUbuntuProcessScope,
    UbuntuLocalPeerExpectation,
    UbuntuLocalPeerIdentity,
    UbuntuWorkerReadiness,
    UbuntuWorkerService,
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
    serve_ubuntu_worker_connection,
    ubuntu_worker_runner,
)
from jarvis_control_plane.terminal_policy import TerminalAction, TerminalComponent
from jarvis_control_plane.ubuntu_worker_ipc import (
    ReconnectingUnixSocketUbuntuWorkerTransport,
)


class _CancellableHandle(Protocol):
    def run(self) -> object | None: ...

    def cancel(self) -> ActionCancellationResult: ...


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
    channel: socket.socket | None = None,
) -> UbuntuWorkerService:
    return UbuntuWorkerService(
        worker_id="ubuntu-01",
        expected_peer=_peer_expectation(),
        authenticator=ControlledUbuntuLocalAuthenticator(
            peer or _local_peer(), connection=channel
        ),
        readiness=lambda: readiness,
        process_scope=process_scope or ControlledUbuntuProcessScope(),
    )


def _peer_expectation() -> UbuntuLocalPeerExpectation:
    return UbuntuLocalPeerExpectation(
        peer_uid=1100,
        socket_owner_uid=1200,
        socket_path="/run/jarvis/ubuntu-worker.sock",
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
    worker: UbuntuWorkerService, selected_host: str, error: str
) -> None:
    with pytest.raises(ActionDispatcherError, match=error):
        worker.authenticate(selected_host=selected_host, timeout_seconds=10)


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERCRED is Linux-specific")
def test_local_authenticator_uses_peer_credentials_and_restricted_socket(
    tmp_path: Path,
) -> None:
    path = os.fspath(tmp_path) + "/ubuntu-worker.sock"
    unix_family = socket.AF_UNIX  # type: ignore[attr-defined]
    listener = socket.socket(unix_family, socket.SOCK_STREAM)
    client = socket.socket(unix_family, socket.SOCK_STREAM)
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
            client_identity = UnixSocketUbuntuLocalAuthenticator(
                connection=client,
                socket_path=path,
                connection_id="local-socket-01",
            ).authenticate(timeout_seconds=10)

            current_uid = os.getuid()  # type: ignore[attr-defined]
            assert identity.peer_uid == current_uid
            assert client_identity.peer_uid == current_uid
            assert identity.socket_owner_uid == current_uid
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


def test_gateway_dispatch_and_result_cross_the_authenticated_local_channel() -> None:
    gateway_connection, worker_connection = socket.socketpair()
    process_scope = ControlledUbuntuProcessScope(
        result=WorkerExecutionResult.completed(stdout="over-local-channel"),
        progress_events=(
            WorkerProgressEvent(
                sequence=2,
                kind=WorkerProgressKind.OUTPUT,
                stream=WorkerOutputStream.STDOUT,
                text="over-local-channel",
            ),
        ),
    )
    service = _worker(process_scope=process_scope, channel=worker_connection)
    server = Thread(
        target=serve_ubuntu_worker_connection,
        args=(worker_connection, service),
        daemon=True,
    )
    server.start()
    identity = WorkerIdentity(
        host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
    )
    authenticator = ControlledUbuntuLocalAuthenticator(
        _local_peer(), connection=gateway_connection
    )
    transport = UbuntuWorkerTransport(
        connection=gateway_connection,
        authenticator=authenticator,
        expected_peer=_peer_expectation(),
        registered_identity=identity,
    )
    gateway = WorkerGateway(
        workers={"ubuntu": transport}, registered_identities={"ubuntu": identity}
    )
    try:
        result = gateway.dispatch(_proposal("action-ubuntu-ipc"))

        assert result.stdout == "over-local-channel"
        assert result.progress_events[-1].text == "over-local-channel"
        assert [item.action_id for item in process_scope.invocations] == [
            "action-ubuntu-ipc"
        ]
    finally:
        transport.close()
        server.join(timeout=10)
    assert not server.is_alive()


def test_worker_server_rejects_an_authenticator_for_another_socket() -> None:
    gateway_connection, worker_connection = socket.socketpair()
    unrelated_gateway, unrelated_worker = socket.socketpair()
    service = _worker(channel=unrelated_worker)
    try:
        with pytest.raises(ValueError, match="exact channel"):
            serve_ubuntu_worker_connection(worker_connection, service)
    finally:
        gateway_connection.close()
        worker_connection.close()
        unrelated_gateway.close()
        unrelated_worker.close()


def test_worker_server_authenticates_before_accepting_registration() -> None:
    gateway_connection, worker_connection = socket.socketpair()
    service = _worker(
        peer=_local_peer(peer_uid=9999),
        channel=worker_connection,
    )
    server = Thread(
        target=lambda: _capture_failure(
            lambda: serve_ubuntu_worker_connection(worker_connection, service), []
        ),
        daemon=True,
    )
    server.start()
    transport = UbuntuWorkerTransport(
        connection=gateway_connection,
        authenticator=ControlledUbuntuLocalAuthenticator(
            _local_peer(), connection=gateway_connection
        ),
        expected_peer=_peer_expectation(),
        registered_identity=WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    try:
        with pytest.raises(ActionDispatcherError, match="channel"):
            transport.register_execution(
                action_id="action-unauthenticated-register",
                timeout_seconds=2,
                retention_seconds=900,
            )
    finally:
        transport.close()
        server.join(timeout=2)
    assert not server.is_alive()


def test_worker_server_stop_event_wakes_an_idle_connection() -> None:
    gateway_connection, worker_connection = socket.socketpair()
    authenticated = Event()
    stop = Event()

    def readiness() -> UbuntuWorkerReadiness:
        authenticated.set()
        return UbuntuWorkerReadiness.READY

    service = UbuntuWorkerService(
        worker_id="ubuntu-01",
        expected_peer=_peer_expectation(),
        authenticator=ControlledUbuntuLocalAuthenticator(
            _local_peer(), connection=worker_connection
        ),
        readiness=readiness,
        process_scope=ControlledUbuntuProcessScope(),
    )
    server = Thread(
        target=serve_ubuntu_worker_connection,
        args=(worker_connection, service),
        kwargs={"stop": stop},
        daemon=True,
    )
    server.start()
    assert authenticated.wait(timeout=2)

    stop.set()
    server.join(timeout=2)

    gateway_connection.close()
    assert not server.is_alive()


def test_transport_disconnect_returns_a_typed_ambiguous_error() -> None:
    gateway_connection, worker_connection = socket.socketpair()
    transport = UbuntuWorkerTransport(
        connection=gateway_connection,
        authenticator=ControlledUbuntuLocalAuthenticator(
            _local_peer(), connection=gateway_connection
        ),
        expected_peer=_peer_expectation(),
        registered_identity=WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    failures: list[BaseException] = []
    call = Thread(
        target=lambda: _capture_failure(
            lambda: transport.register_execution(
                action_id="action-disconnected-register",
                timeout_seconds=5,
                retention_seconds=900,
            ),
            failures,
        )
    )
    call.start()
    assert worker_connection.recv(4096)

    worker_connection.close()
    call.join(timeout=2)

    transport.close()
    assert not call.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ActionDispatcherError)
    assert failures[0].may_have_dispatched is True


def test_gateway_reconnects_native_ubuntu_transport_before_the_next_action() -> None:
    first_gateway, first_worker = socket.socketpair()
    identity = WorkerIdentity(
        host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
    )
    initial = UbuntuWorkerTransport(
        connection=first_gateway,
        authenticator=ControlledUbuntuLocalAuthenticator(
            _local_peer(), connection=first_gateway
        ),
        expected_peer=_peer_expectation(),
        registered_identity=identity,
    )
    first_worker.close()
    deadline = monotonic() + 2
    while not initial.is_closed and monotonic() < deadline:
        sleep(0.01)
    assert initial.is_closed

    second_gateway, second_worker = socket.socketpair()
    process_scope = ControlledUbuntuProcessScope(
        result=WorkerExecutionResult.completed(stdout="after-reconnect")
    )
    service = _worker(process_scope=process_scope, channel=second_worker)
    server = Thread(
        target=serve_ubuntu_worker_connection,
        args=(second_worker, service),
        daemon=True,
    )
    server.start()
    replacement = UbuntuWorkerTransport(
        connection=second_gateway,
        authenticator=ControlledUbuntuLocalAuthenticator(
            _local_peer(), connection=second_gateway
        ),
        expected_peer=_peer_expectation(),
        registered_identity=identity,
    )
    reconnects: list[object] = []

    def reconnect() -> UbuntuWorkerTransport:
        reconnects.append(object())
        return replacement

    transport = ReconnectingUnixSocketUbuntuWorkerTransport(
        connect=reconnect,
        initial=initial,
    )
    gateway = WorkerGateway(
        workers={"ubuntu": transport},
        registered_identities={"ubuntu": identity},
    )
    try:
        result = gateway.dispatch(_proposal("action-ubuntu-reconnected"))
    finally:
        transport.close()
        server.join(timeout=2)

    assert result.stdout == "after-reconnect"
    assert len(reconnects) == 1
    assert not server.is_alive()


def test_disconnect_retires_a_connection_owned_reservation() -> None:
    gateway_connection, worker_connection = socket.socketpair()
    process_scope = ControlledUbuntuProcessScope()
    service = _worker(process_scope=process_scope, channel=worker_connection)
    server = Thread(
        target=serve_ubuntu_worker_connection,
        args=(worker_connection, service),
        daemon=True,
    )
    server.start()
    transport = UbuntuWorkerTransport(
        connection=gateway_connection,
        authenticator=ControlledUbuntuLocalAuthenticator(
            _local_peer(), connection=gateway_connection
        ),
        expected_peer=_peer_expectation(),
        registered_identity=WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    transport.register_execution(
        action_id="action-abandoned-register",
        timeout_seconds=10,
        retention_seconds=900,
    )

    transport.close()
    server.join(timeout=2)

    assert not server.is_alive()
    assert process_scope.cancellations == [("action-abandoned-register", 10)]


def test_gateway_cancellation_crosses_the_local_channel_and_stops_scope() -> None:
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

    gateway_connection, worker_connection = socket.socketpair()
    process_scope = ControlledUbuntuProcessScope(
        execution_hook=execute, cancellation_hook=cancel
    )
    service = _worker(process_scope=process_scope, channel=worker_connection)
    server = Thread(
        target=serve_ubuntu_worker_connection,
        args=(worker_connection, service),
        daemon=True,
    )
    server.start()
    identity = WorkerIdentity(
        host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
    )
    transport = UbuntuWorkerTransport(
        connection=gateway_connection,
        authenticator=ControlledUbuntuLocalAuthenticator(
            _local_peer(), connection=gateway_connection
        ),
        expected_peer=_peer_expectation(),
        registered_identity=identity,
    )
    gateway = WorkerGateway(
        workers={"ubuntu": transport}, registered_identities={"ubuntu": identity}
    )
    handle = cast(
        _CancellableHandle,
        gateway.prepare(_proposal("action-ubuntu-ipc-cancel")),
    )
    failures: list[BaseException] = []
    run = Thread(target=lambda: _capture_failure(handle.run, failures))
    run.start()
    assert started.wait(timeout=10)

    cancellation = handle.cancel()
    run.join(timeout=10)

    assert cancellation.status is ActionCancellationStatus.STOPPED
    assert not run.is_alive()
    assert isinstance(failures[0], WorkerExecutionError)
    assert failures[0].result.process_tree_stopped is True
    transport.close()
    server.join(timeout=10)
    assert not server.is_alive()


def _capture_failure(
    operation: Callable[[], object], failures: list[BaseException]
) -> None:
    try:
        operation()
    except BaseException as exc:  # noqa: BLE001 - test observes worker boundary
        failures.append(exc)


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
    assert "--property=RestrictNamespaces=yes" in command
    assert "--property=RuntimeMaxSec=120s" in command
    inaccessible = next(
        argument
        for argument in command
        if argument.startswith("--property=InaccessiblePaths=")
    )
    runtime_uid = os.getuid() if hasattr(os, "getuid") else 0
    assert f"/run/user/{runtime_uid}/systemd/private" in inaccessible
    assert f"/run/user/{runtime_uid}/bus" in inaccessible
    assert "%t" not in inaccessible
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


@pytest.mark.parametrize(
    ("return_code", "state", "wrapper_completed", "expected"),
    [
        (1, "", False, False),
        (3, "inactive\n", False, True),
        (3, "failed\n", False, True),
        (4, "inactive\n", False, False),
        (4, "inactive\n", True, True),
        (4, "unknown\n", False, False),
        (4, "unknown\n", True, True),
    ],
)
def test_systemd_scope_requires_a_known_stopped_unit_state(
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
    state: str,
    wrapper_completed: bool,
    expected: bool,
) -> None:
    scope = SystemdUbuntuProcessScope()

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], return_code, stdout=state)

    monkeypatch.setattr(ubuntu_worker_module.subprocess, "run", run)
    observed = Event()
    running = ubuntu_worker_module._RunningSystemdScope(
        unit_name="jarvis-action-test.service",
        process=cast("subprocess.Popen[bytes]", object()),
        cancel_requested=Event(),
        termination_lock=RLock(),
        unit_observed=observed,
    )

    stopped = scope._unit_is_stopped(
        running,
        timeout_seconds=1,
        wrapper_completed=wrapper_completed,
    )

    assert stopped is expected


def test_systemd_scope_waits_for_a_deactivating_unit_to_be_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = SystemdUbuntuProcessScope()
    checks = iter(
        (
            subprocess.CompletedProcess([], 3, stdout="deactivating\n"),
            subprocess.CompletedProcess([], 4, stdout="inactive\n"),
        )
    )

    monkeypatch.setattr(
        ubuntu_worker_module.subprocess,
        "run",
        lambda *_args, **_kwargs: next(checks),
    )
    running = ubuntu_worker_module._RunningSystemdScope(
        unit_name="jarvis-action-test.service",
        process=cast("subprocess.Popen[bytes]", object()),
        cancel_requested=Event(),
        termination_lock=RLock(),
        unit_observed=Event(),
    )

    assert scope._unit_is_stopped(
        running,
        timeout_seconds=1,
        wrapper_completed=True,
    )
    assert running.unit_observed.is_set()


def test_systemd_scope_accepts_a_collected_unit_after_wait_wrapper_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedWrapper:
        def __init__(self) -> None:
            self.stdout = BytesIO()
            self.stderr = BytesIO()

        @staticmethod
        def poll() -> int:
            return 0

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 4, stdout="unknown\n")

    scope = SystemdUbuntuProcessScope()
    monkeypatch.setattr(ubuntu_worker_module.subprocess, "run", run)
    running = ubuntu_worker_module._RunningSystemdScope(
        unit_name="jarvis-action-collected.service",
        process=cast("subprocess.Popen[bytes]", ExitedWrapper()),
        cancel_requested=Event(),
        termination_lock=RLock(),
        unit_observed=Event(),
    )
    invocation = _invocation(
        "action-ubuntu-collected",
        WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )

    result = scope._observe(running, invocation, lambda _event: None)

    assert result.status is WorkerExecutionStatus.COMPLETED
    assert result.process_tree_stopped is True
    assert not running.unit_observed.is_set()


def test_systemd_scope_runs_structured_compounds_inside_the_same_unit() -> None:
    scope = SystemdUbuntuProcessScope()
    invocation = replace(
        _invocation(
            "action-ubuntu-compound",
            WorkerIdentity(
                host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
            ),
        ),
        action=TerminalAction(
            host="ubuntu",
            executable="/usr/bin/printf",
            arguments=("hello",),
            cwd="/workspace",
            components=(
                TerminalComponent("/usr/bin/printf", ("hello",)),
                TerminalComponent("/usr/bin/tr", ("a-z", "A-Z"), "|"),
                TerminalComponent("/usr/bin/printf", ("done",), "&&"),
            ),
        ),
    )

    command = scope.command_for(invocation)
    separator = command.index("--")
    action_command = command[separator + 1 :]

    assert action_command[:3] == (
        sys.executable,
        "-m",
        "jarvis_control_plane.ubuntu_worker_runner",
    )
    assert all(
        cast(TerminalComponent, component).executable not in command
        for component in invocation.action.components
    )


def test_compound_runner_preserves_control_flow_without_a_shell(
    capfd: pytest.CaptureFixture[str],
) -> None:
    plan = (
        (sys.executable, ("-c", "raise SystemExit(1)"), ""),
        (sys.executable, ("-c", "print('wrong')"), "&&"),
        (sys.executable, ("-c", "print('recovered')"), "||"),
    )

    status = ubuntu_worker_runner._run_plan(plan)
    captured = capfd.readouterr()

    assert status == 0
    assert captured.out.splitlines() == ["recovered"]
    assert '"started":[0,2]' in captured.err
    assert '"completed":[0,2]' in captured.err


def test_compound_runner_connects_a_structured_pipeline(
    capfd: pytest.CaptureFixture[str],
) -> None:
    plan = (
        (sys.executable, ("-c", "import sys; sys.stdout.write('hello')"), ""),
        (
            sys.executable,
            (
                "-c",
                "import sys; sys.stdout.write(sys.stdin.read().upper())",
            ),
            "|",
        ),
    )

    status = ubuntu_worker_runner._run_plan(plan)
    captured = capfd.readouterr()

    assert status == 0
    assert captured.out == "HELLO"
    assert '"started":[0,1]' in captured.err
    assert '"completed":[0,1]' in captured.err


def test_systemd_scope_deadline_applies_after_wrapper_exit_with_open_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InheritedPipe:
        def __init__(self) -> None:
            self.closed = Event()

        def read(self, _size: int) -> bytes:
            assert self.closed.wait(timeout=5)
            return b""

        read1 = read

        def close(self) -> None:
            self.closed.set()

    class ExitedWrapper:
        def __init__(self) -> None:
            self.stdout = InheritedPipe()
            self.stderr = InheritedPipe()

        @staticmethod
        def poll() -> int:
            return 0

    scope = SystemdUbuntuProcessScope()
    wrapper = ExitedWrapper()
    running_type = ubuntu_worker_module._RunningSystemdScope
    running = running_type(
        unit_name="jarvis-action-test.service",
        process=cast("subprocess.Popen[bytes]", wrapper),
        cancel_requested=Event(),
        termination_lock=RLock(),
        unit_observed=Event(),
    )
    monkeypatch.setattr(scope, "_stop_scope", lambda *_args: True)
    invocation = replace(
        _invocation(
            "action-ubuntu-inherited-pipe",
            WorkerIdentity(
                host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
            ),
        ),
        deadline_seconds=1,
        cancellation_grace_seconds=1,
    )

    started = monotonic()
    result = scope._observe(running, invocation, lambda _event: None)

    assert monotonic() - started < 3
    assert result.status is WorkerExecutionStatus.TIMED_OUT
    assert result.process_tree_stopped is True


def test_systemd_scope_releases_a_reservation_after_pre_dispatch_rejection() -> None:
    scope = SystemdUbuntuProcessScope()
    invocation = _invocation(
        "action-ubuntu-preflight",
        WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    invocation = replace(
        invocation,
        action=TerminalAction(
            host="ubuntu",
            executable="/usr/bin/printf",
            arguments=("hello",),
            cwd="/workspace",
            components=(
                TerminalComponent(
                    executable="/usr/bin/printf",
                    arguments=("hello",),
                    redirections=("/workspace/out.txt",),
                ),
            ),
        ),
    )
    scope.reserve(action_id=invocation.action_id)

    with pytest.raises(ActionDispatcherError):
        scope.execute(invocation, lambda _event: None)

    scope.reserve(action_id=invocation.action_id)


def test_systemd_scope_retires_a_pre_start_cancellation_tombstone() -> None:
    scope = SystemdUbuntuProcessScope()
    action_id = "action-ubuntu-retire"
    scope.reserve(action_id=action_id)

    result = scope.cancel(action_id=action_id, timeout_seconds=10)
    scope.retire(action_id=action_id)
    scope.reserve(action_id=action_id)

    assert result.status is ActionCancellationStatus.NOT_STARTED


def test_systemd_scope_honors_cancellation_that_arrives_during_process_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_start = Event()
    release_start = Event()

    class StartedProcess:
        def __init__(self) -> None:
            self.stdout = BytesIO()
            self.stderr = BytesIO()

        @staticmethod
        def poll() -> None:
            return None

    process = StartedProcess()

    def start(*_args: object, **_kwargs: object) -> StartedProcess:
        entered_start.set()
        assert release_start.wait(timeout=5)
        return process

    scope = SystemdUbuntuProcessScope()
    monkeypatch.setattr(ubuntu_worker_module.sys, "platform", "linux")
    monkeypatch.setattr(ubuntu_worker_module.subprocess, "Popen", start)
    monkeypatch.setattr(scope, "_stop_scope", lambda *_args: True)
    monkeypatch.setattr(
        scope,
        "_observe",
        lambda *_args: pytest.fail("cancelled startup must not enter observation"),
    )
    invocation = _invocation(
        "action-ubuntu-cancel-during-start",
        WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    scope.reserve(action_id=invocation.action_id)
    results: list[WorkerExecutionResult] = []
    execution = Thread(
        target=lambda: results.append(scope.execute(invocation, lambda _event: None))
    )
    execution.start()
    assert entered_start.wait(timeout=2)
    cancellations: list[ActionCancellationResult] = []
    cancellation = Thread(
        target=lambda: cancellations.append(
            scope.cancel(action_id=invocation.action_id, timeout_seconds=5)
        )
    )
    cancellation.start()
    starting = scope._starting[invocation.action_id]
    assert starting.cancel_requested.wait(timeout=2)

    release_start.set()
    execution.join(timeout=2)
    cancellation.join(timeout=2)

    assert results[0].status is WorkerExecutionStatus.CANCELLED
    assert results[0].process_tree_stopped is True
    assert cancellations[0].status is ActionCancellationStatus.STOPPED
    assert process.stdout.closed
    assert process.stderr.closed


def test_ubuntu_worker_rejects_retention_above_configured_maximum() -> None:
    worker = _worker()

    with pytest.raises(ValueError, match="configured maximum"):
        worker.register_execution(
            action_id="action-ubuntu-excess-retention",
            timeout_seconds=10,
            retention_seconds=901,
        )


def test_duplicate_registration_does_not_leak_a_process_reservation() -> None:
    process_scope = ControlledUbuntuProcessScope()
    worker = _worker(process_scope=process_scope)
    worker.register_execution(
        action_id="action-ubuntu-duplicate",
        timeout_seconds=10,
        retention_seconds=900,
    )
    worker.finalize_execution(
        action_id="action-ubuntu-duplicate",
        timeout_seconds=10,
        retention_seconds=900,
    )

    with pytest.raises(ActionDispatcherError, match="already registered"):
        worker.register_execution(
            action_id="action-ubuntu-duplicate",
            timeout_seconds=10,
            retention_seconds=900,
        )

    process_scope.reserve(action_id="action-ubuntu-duplicate")


def test_reserved_action_expires_and_retires_its_process_scope() -> None:
    now = 0.0
    process_scope = ControlledUbuntuProcessScope()
    worker = UbuntuWorkerService(
        worker_id="ubuntu-01",
        expected_peer=_peer_expectation(),
        authenticator=ControlledUbuntuLocalAuthenticator(_local_peer()),
        readiness=lambda: UbuntuWorkerReadiness.READY,
        process_scope=process_scope,
        clock=lambda: now,
    )
    worker.register_execution(
        action_id="action-ubuntu-expiring",
        timeout_seconds=10,
        retention_seconds=10,
    )
    now = 11

    worker.register_execution(
        action_id="action-ubuntu-after-expiry",
        timeout_seconds=10,
        retention_seconds=10,
    )

    process_scope.reserve(action_id="action-ubuntu-expiring")


def test_running_action_disables_reservation_expiry_until_it_finishes() -> None:
    now = 0.0
    started = Event()
    release = Event()

    def execute(_invocation: object) -> WorkerExecutionResult:
        started.set()
        assert release.wait(timeout=10)
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.CANCELLED,
            process_tree_stopped=True,
        )

    def cancel(_action_id: str, _timeout_seconds: int) -> ActionCancellationResult:
        release.set()
        return ActionCancellationResult(ActionCancellationStatus.STOPPED)

    process_scope = ControlledUbuntuProcessScope(
        execution_hook=execute,
        cancellation_hook=cancel,
    )
    worker = UbuntuWorkerService(
        worker_id="ubuntu-01",
        expected_peer=_peer_expectation(),
        authenticator=ControlledUbuntuLocalAuthenticator(_local_peer()),
        readiness=lambda: UbuntuWorkerReadiness.READY,
        process_scope=process_scope,
        clock=lambda: now,
    )
    identity = worker.authenticate(selected_host="ubuntu", timeout_seconds=10)
    invocation = _invocation("action-ubuntu-running-expiry", identity)
    worker.register_execution(
        action_id=invocation.action_id,
        timeout_seconds=10,
        retention_seconds=1,
    )
    results: list[WorkerExecutionResult] = []
    thread = Thread(
        target=lambda: results.append(worker.execute(invocation, lambda _event: None))
    )
    thread.start()
    assert started.wait(timeout=10)
    now = 2
    worker.finalize_execution(
        action_id=invocation.action_id,
        timeout_seconds=10,
        retention_seconds=1,
    )
    now = 4

    cancellation = worker.cancel(
        action_id=invocation.action_id,
        timeout_seconds=10,
        retention_seconds=1,
    )
    thread.join(timeout=10)

    assert cancellation.status is ActionCancellationStatus.STOPPED
    assert not thread.is_alive()
    assert results[0].status is WorkerExecutionStatus.CANCELLED
    assert process_scope.cancellations == [(invocation.action_id, 10)]


def test_observation_failure_stops_and_releases_the_process_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        def __init__(self) -> None:
            self.stdout = BytesIO()
            self.stderr = BytesIO()

        @staticmethod
        def poll() -> int:
            return 0

    process = ExitedProcess()
    stopped: list[object] = []
    scope = SystemdUbuntuProcessScope()
    monkeypatch.setattr(ubuntu_worker_module.sys, "platform", "linux")
    monkeypatch.setattr(
        ubuntu_worker_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        scope,
        "_stop_scope",
        lambda running, _timeout: stopped.append(running) is None or True,
    )
    invocation = _invocation(
        "action-ubuntu-progress-failure",
        WorkerIdentity(
            host="ubuntu", worker_id="ubuntu-01", connection_id="local-boot-01"
        ),
    )
    scope.reserve(action_id=invocation.action_id)

    with pytest.raises(RuntimeError, match="progress send failed"):
        scope.execute(
            invocation,
            lambda _event: (_ for _ in ()).throw(RuntimeError("progress send failed")),
        )

    assert len(stopped) == 1
    scope.reserve(action_id="action-ubuntu-after-progress-failure")


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
    handle = cast(
        _CancellableHandle,
        gateway.prepare(_proposal("action-ubuntu-cancel")),
    )
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


def test_cancellation_wins_the_reserved_scope_before_process_start() -> None:
    entered_execute = Event()
    release_execute = Event()
    inner = ControlledUbuntuProcessScope()

    class PausingProcessScope:
        def reserve(self, *, action_id: str) -> None:
            inner.reserve(action_id=action_id)

        def retire(self, *, action_id: str) -> None:
            inner.retire(action_id=action_id)

        def execute(self, invocation, progress):
            entered_execute.set()
            assert release_execute.wait(timeout=10)
            return inner.execute(invocation, progress)

        def cancel(self, *, action_id: str, timeout_seconds: int):
            return inner.cancel(action_id=action_id, timeout_seconds=timeout_seconds)

    worker = UbuntuWorkerService(
        worker_id="ubuntu-01",
        expected_peer=_peer_expectation(),
        authenticator=ControlledUbuntuLocalAuthenticator(_local_peer()),
        readiness=lambda: UbuntuWorkerReadiness.READY,
        process_scope=PausingProcessScope(),
    )
    identity = worker.authenticate(selected_host="ubuntu", timeout_seconds=10)
    invocation = _invocation("action-ubuntu-cancel-before-start", identity)
    worker.register_execution(
        action_id=invocation.action_id,
        timeout_seconds=10,
        retention_seconds=900,
    )
    results: list[WorkerExecutionResult] = []
    thread = Thread(
        target=lambda: results.append(worker.execute(invocation, lambda _event: None))
    )
    thread.start()
    assert entered_execute.wait(timeout=10)

    cancellation = worker.cancel(
        action_id=invocation.action_id,
        timeout_seconds=10,
        retention_seconds=900,
    )
    release_execute.set()
    thread.join(timeout=10)

    assert cancellation.status is ActionCancellationStatus.NOT_STARTED
    assert results[0].status is WorkerExecutionStatus.CANCELLED
    assert inner.invocations == []


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
    worker = UbuntuWorkerService(
        worker_id="ubuntu-01",
        expected_peer=_peer_expectation(),
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


def test_ubuntu_worker_rejects_limits_above_its_configured_contract() -> None:
    process_scope = ControlledUbuntuProcessScope()
    worker = _worker(process_scope=process_scope)
    identity = worker.authenticate(selected_host="ubuntu", timeout_seconds=10)
    invocation = replace(
        _invocation("action-ubuntu-limit-bypass", identity),
        deadline_seconds=121,
    )
    worker.register_execution(
        action_id=invocation.action_id,
        timeout_seconds=10,
        retention_seconds=900,
    )

    with pytest.raises(ActionDispatcherError, match="limits are invalid"):
        worker.execute(invocation, lambda _event: None)

    assert process_scope.invocations == []


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
