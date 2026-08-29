from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from threading import Event, Thread

import pytest

from jarvis_control_plane import (
    ActionDispatcherError,
    ControlledUbuntuLocalAuthenticator,
    ControlledUbuntuProcessScope,
    UbuntuWorkerReadiness,
    UbuntuWorkerService,
    UbuntuWorkerTransport,
    UnixSocketUbuntuLocalAuthenticator,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
    WorkerIdentity,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    serve_ubuntu_worker_connection,
)

from .helpers import (
    _capture_failure,
    _local_peer,
    _peer_expectation,
    _proposal,
    _worker,
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
        result=WorkerExecutionResult(
            status=WorkerExecutionStatus.COMPLETED,
            process_tree_stopped=True,
            stdout="over-local-channel\n[output truncated]",
            stderr="warning\n[output truncated]",
            stdout_truncated=True,
            stderr_truncated=True,
        ),
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

        assert result.stdout == "over-local-channel\n[output truncated]"
        assert result.stderr == "warning\n[output truncated]"
        assert result.stdout_truncated is True
        assert result.stderr_truncated is True
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
