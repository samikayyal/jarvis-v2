from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from threading import Event, Thread

import pytest

import jarvis_control_plane.ubuntu_worker as ubuntu_worker_module
from jarvis_control_plane import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ControlledUbuntuLocalAuthenticator,
    ControlledUbuntuProcessScope,
    SystemdUbuntuProcessScope,
    UbuntuWorkerReadiness,
    UbuntuWorkerService,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
)
from jarvis_control_plane.terminal_policy import TerminalAction, TerminalComponent

from .helpers import (
    _ControlledUbuntuProcessScopeAdapter,
    _invocation,
    _local_peer,
    _peer_expectation,
    _unit_check,
    _worker,
)


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

    scope = SystemdUbuntuProcessScope(
        systemd_adapter=_ControlledUbuntuProcessScopeAdapter(
            _unit_check(3, "inactive\n")
        )
    )
    monkeypatch.setattr(ubuntu_worker_module.sys, "platform", "linux")
    monkeypatch.setattr(ubuntu_worker_module.subprocess, "Popen", start)
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
    adapter = _ControlledUbuntuProcessScopeAdapter(_unit_check(3, "inactive\n"))
    scope = SystemdUbuntuProcessScope(systemd_adapter=adapter)
    monkeypatch.setattr(ubuntu_worker_module.sys, "platform", "linux")
    monkeypatch.setattr(
        ubuntu_worker_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
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

    assert adapter.unit_checks
    scope.reserve(action_id="action-ubuntu-after-progress-failure")
