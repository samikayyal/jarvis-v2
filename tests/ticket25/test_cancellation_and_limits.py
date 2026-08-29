from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread
from typing import cast

import pytest

from jarvis_control_plane import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ControlledUbuntuLocalAuthenticator,
    ControlledUbuntuProcessScope,
    UbuntuWorkerReadiness,
    UbuntuWorkerService,
    WorkerExecutionError,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
    WorkerIdentity,
)

from .helpers import (
    _CancellableHandle,
    _invocation,
    _local_peer,
    _peer_expectation,
    _proposal,
    _worker,
)


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
