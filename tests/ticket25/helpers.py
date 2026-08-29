from __future__ import annotations

import socket
import subprocess
from collections.abc import Callable
from io import BytesIO
from typing import Protocol

import pytest

import jarvis_control_plane.ubuntu_worker as ubuntu_worker_module
from jarvis_control_plane import (
    ActionCancellationResult,
    ControlledUbuntuLocalAuthenticator,
    ControlledUbuntuProcessScope,
    FrozenActionProposal,
    SystemdUbuntuProcessScope,
    UbuntuLocalPeerExpectation,
    UbuntuLocalPeerIdentity,
    UbuntuWorkerReadiness,
    UbuntuWorkerService,
    WorkerExecutionResult,
    WorkerIdentity,
    WorkerInvocation,
    WorkerProgressEvent,
)


class _CancellableHandle(Protocol):  # noqa: PYI046 - imported by lifecycle tests
    def run(self) -> object | None: ...

    def cancel(self) -> ActionCancellationResult: ...


class _ControlledUbuntuProcessScopeAdapter:
    """Script systemd observations while exercising the public scope API."""

    def __init__(
        self,
        *checks: subprocess.CompletedProcess[str],
    ) -> None:
        self._checks = list(checks)
        self._fallback = checks[-1] if checks else _unit_check(3, "inactive\n")
        self.unit_checks: list[tuple[str, float]] = []
        self.signals: list[tuple[str, str, float]] = []

    def check_unit(
        self, unit_name: str, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        self.unit_checks.append((unit_name, timeout_seconds))
        if self._checks:
            return self._checks.pop(0)
        return self._fallback

    def signal_unit(
        self, unit_name: str, signal: str, *, timeout_seconds: float
    ) -> None:
        self.signals.append((unit_name, signal, timeout_seconds))


def _unit_check(return_code: int, state: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], return_code, stdout=state)


class _ExitedProcess:
    def __init__(
        self,
        *,
        return_code: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.return_code = return_code
        self.stdout = BytesIO(stdout)
        self.stderr = BytesIO(stderr)

    def poll(self) -> int:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.return_code


def _execute_systemd_scope(
    monkeypatch: pytest.MonkeyPatch,
    scope: SystemdUbuntuProcessScope,
    invocation: WorkerInvocation,
    process: object,
    progress: Callable[[WorkerProgressEvent], None] | None = None,
) -> WorkerExecutionResult:
    monkeypatch.setattr(ubuntu_worker_module.sys, "platform", "linux")
    monkeypatch.setattr(
        ubuntu_worker_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    scope.reserve(action_id=invocation.action_id)
    return scope.execute(invocation, progress or (lambda _event: None))


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


def _capture_failure(
    operation: Callable[[], object], failures: list[BaseException]
) -> None:
    try:
        operation()
    except BaseException as exc:  # noqa: BLE001 - test observes worker boundary
        failures.append(exc)


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
