from __future__ import annotations

import socket
from threading import Event, Thread
from time import monotonic, sleep
from typing import cast

from jarvis_control_plane import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ControlledUbuntuLocalAuthenticator,
    ControlledUbuntuProcessScope,
    UbuntuWorkerTransport,
    WorkerExecutionError,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
    WorkerIdentity,
    serve_ubuntu_worker_connection,
)
from jarvis_control_plane.ubuntu_worker_ipc import (
    ReconnectingUnixSocketUbuntuWorkerTransport,
)

from .helpers import (
    _CancellableHandle,
    _capture_failure,
    _local_peer,
    _peer_expectation,
    _proposal,
    _worker,
)


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
