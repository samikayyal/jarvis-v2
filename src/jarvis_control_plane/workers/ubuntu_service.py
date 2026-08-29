"""Authenticated Ubuntu worker service core."""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from time import monotonic

from ..ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
)
from .contracts import (
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    WorkerProgressSink,
)
from .ubuntu_authentication import (
    UbuntuLocalAuthenticator,
    UbuntuLocalPeerExpectation,
    UbuntuWorkerReadiness,
)
from .ubuntu_process_execution import _truncate_utf8
from .ubuntu_process_scope import UbuntuProcessScope


class _ActionState(str, Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    CANCELLED = "cancelled"
    TERMINAL = "terminal"
    FINALIZED = "finalized"


@dataclass(slots=True)
class _ActionRecord:
    state: _ActionState
    retention_seconds: int
    expires_at: float | None = None
    finalize_requested: bool = False


class UbuntuWorkerService:
    """Worker-side service core reached only through the local wire adapter."""

    def __init__(
        self,
        *,
        worker_id: str,
        expected_peer: UbuntuLocalPeerExpectation,
        authenticator: UbuntuLocalAuthenticator,
        readiness: Callable[[], UbuntuWorkerReadiness],
        process_scope: UbuntuProcessScope,
        limits: WorkerExecutionLimits | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("Ubuntu worker identifier must be non-blank")
        self._worker_id = worker_id.strip()
        self._expected_peer = expected_peer
        self._authenticator = authenticator
        self._readiness = readiness
        self._process_scope = process_scope
        self._limits = limits or WorkerExecutionLimits()
        self._clock = clock
        self._lock = RLock()
        self._actions: dict[str, _ActionRecord] = {}
        self._active_action_id: str | None = None
        self._authenticated_identity: WorkerIdentity | None = None

    def binds(self, connection: socket.socket) -> bool:
        """Return whether OS identity checks use this exact accepted channel."""

        return self._authenticator.binds(connection)

    def register_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        del timeout_seconds  # There is no remote registration hop.
        self._validate_retention(retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            if action_id in self._actions:
                raise ActionDispatcherError(
                    f"Ubuntu worker action {action_id} is already registered",
                    may_have_dispatched=True,
                )
            self._process_scope.reserve(action_id=action_id)
            try:
                self._actions[action_id] = _ActionRecord(
                    state=_ActionState.RESERVED,
                    retention_seconds=retention_seconds,
                    expires_at=self._clock() + retention_seconds,
                )
            except BaseException:
                self._process_scope.retire(action_id=action_id)
                raise

    def authenticate(
        self, *, selected_host: str, timeout_seconds: int
    ) -> WorkerIdentity:
        if selected_host != "ubuntu":
            raise ActionDispatcherError("native Ubuntu worker is bound only to ubuntu")
        readiness = UbuntuWorkerReadiness(self._readiness())
        if readiness is not UbuntuWorkerReadiness.READY:
            raise ActionDispatcherError(f"native Ubuntu worker is {readiness.value}")
        identity = self._authenticate_local_identity(timeout_seconds=timeout_seconds)
        with self._lock:
            self._authenticated_identity = identity
        return identity

    def _authenticate_local_identity(self, *, timeout_seconds: int) -> WorkerIdentity:
        peer = self._authenticator.authenticate(timeout_seconds=timeout_seconds)
        if not self._expected_peer.matches(peer):
            raise ActionDispatcherError(
                "native Ubuntu worker local peer identity failed"
            )
        return WorkerIdentity(
            host="ubuntu",
            worker_id=self._worker_id,
            connection_id=peer.connection_id,
        )

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(invocation.action_id)
            state = record.state if record is not None else None
            if state is _ActionState.CANCELLED:
                return WorkerExecutionResult(
                    status=WorkerExecutionStatus.CANCELLED,
                    process_tree_stopped=True,
                )
            if state is not _ActionState.RESERVED:
                raise ActionDispatcherError(
                    f"Ubuntu worker action {invocation.action_id} is not executable",
                    may_have_dispatched=state
                    in {_ActionState.RUNNING, _ActionState.TERMINAL},
                )
            if self._active_action_id is not None:
                raise ActionDispatcherError("native Ubuntu worker is busy")
            self._validate_invocation_locked(invocation)
            actual_identity = self._authenticate_local_identity(
                timeout_seconds=min(invocation.deadline_seconds, 10)
            )
            if actual_identity != invocation.worker_identity:
                raise ActionDispatcherError(
                    "native Ubuntu worker connection identity changed"
                )
            assert record is not None
            record.state = _ActionState.RUNNING
            record.expires_at = None
            self._active_action_id = invocation.action_id
        try:
            result = self._process_scope.execute(invocation, progress)
            return _bound_result(result, invocation)
        finally:
            with self._lock:
                record = self._actions.get(invocation.action_id)
                if record is not None:
                    record.state = (
                        _ActionState.FINALIZED
                        if record.finalize_requested
                        else _ActionState.TERMINAL
                    )
                    record.expires_at = self._clock() + record.retention_seconds
                if self._active_action_id == invocation.action_id:
                    self._active_action_id = None

    def cancel(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> ActionCancellationResult:
        self._validate_retention(retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(action_id)
            state = record.state if record is not None else None
            if state in {None, _ActionState.RESERVED, _ActionState.CANCELLED}:
                if record is None:
                    record = _ActionRecord(
                        state=_ActionState.CANCELLED,
                        retention_seconds=retention_seconds,
                    )
                    self._actions[action_id] = record
                else:
                    record.state = _ActionState.CANCELLED
                record.expires_at = self._clock() + record.retention_seconds
                scope_result = self._process_scope.cancel(
                    action_id=action_id, timeout_seconds=timeout_seconds
                )
                if scope_result.status not in {
                    ActionCancellationStatus.NOT_STARTED,
                    ActionCancellationStatus.UNKNOWN,
                }:
                    raise ActionDispatcherError(
                        "Ubuntu process scope returned invalid pre-start cancellation"
                    )
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            if state in {_ActionState.TERMINAL, _ActionState.FINALIZED}:
                return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        result = self._process_scope.cancel(
            action_id=action_id, timeout_seconds=timeout_seconds
        )
        if not isinstance(result, ActionCancellationResult):
            raise TypeError("Ubuntu process scope returned an invalid cancellation")
        return result

    def finalize_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        del timeout_seconds
        self._validate_retention(retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(action_id)
            if record is None:
                record = _ActionRecord(
                    state=_ActionState.FINALIZED,
                    retention_seconds=retention_seconds,
                )
                self._actions[action_id] = record
            elif record.state is _ActionState.RUNNING:
                record.finalize_requested = True
            else:
                record.state = _ActionState.FINALIZED
                record.expires_at = self._clock() + record.retention_seconds
            self._process_scope.retire(action_id=action_id)

    def _validate_invocation_locked(self, invocation: WorkerInvocation) -> None:
        if invocation.action.host != "ubuntu":
            raise ActionDispatcherError("native Ubuntu worker rejected another host")
        if invocation.interactive:
            raise ActionDispatcherError("native Ubuntu worker is non-interactive")
        bounded_values = (
            (invocation.deadline_seconds, self._limits.deadline_seconds),
            (invocation.stdout_limit_bytes, self._limits.stdout_limit_bytes),
            (invocation.stderr_limit_bytes, self._limits.stderr_limit_bytes),
            (
                invocation.cancellation_grace_seconds,
                self._limits.cancellation_grace_seconds,
            ),
            (invocation.progress_event_limit, self._limits.progress_event_limit),
            (invocation.milestone_limit_bytes, self._limits.milestone_limit_bytes),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > maximum
            for value, maximum in bounded_values
        ):
            raise ActionDispatcherError("native Ubuntu worker limits are invalid")
        if self._readiness() is not UbuntuWorkerReadiness.READY:
            raise ActionDispatcherError("native Ubuntu worker is not ready")
        if invocation.worker_identity != self._authenticated_identity:
            raise ActionDispatcherError(
                "native Ubuntu worker connection identity changed"
            )

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        for action_id, record in tuple(self._actions.items()):
            if record.expires_at is not None and record.expires_at <= now:
                del self._actions[action_id]
                self._process_scope.retire(action_id=action_id)

    def _validate_retention(self, retention_seconds: int) -> None:
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or retention_seconds < 1
            or retention_seconds > self._limits.action_state_retention_seconds
        ):
            raise ValueError(
                "Ubuntu worker retention must be between one second and the "
                "configured maximum"
            )


def _bound_result(
    result: WorkerExecutionResult, invocation: WorkerInvocation
) -> WorkerExecutionResult:
    if not isinstance(result, WorkerExecutionResult):
        raise TypeError("Ubuntu process scope returned an invalid result")
    stdout_overflow = len(result.stdout.encode()) > invocation.stdout_limit_bytes
    stderr_overflow = len(result.stderr.encode()) > invocation.stderr_limit_bytes
    stdout = _truncate_utf8(result.stdout, invocation.stdout_limit_bytes)
    stderr = _truncate_utf8(result.stderr, invocation.stderr_limit_bytes)
    return WorkerExecutionResult(
        status=result.status,
        started_components=result.started_components,
        completed_components=result.completed_components,
        process_tree_stopped=result.process_tree_stopped,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=result.stdout_truncated or stdout_overflow,
        stderr_truncated=result.stderr_truncated or stderr_overflow,
    )
