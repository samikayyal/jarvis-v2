"""Deterministic controlled transport for gateway contract tests."""

from __future__ import annotations

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
from ..terminal_policy import TerminalAction
from .contracts import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    WorkerProgressSink,
)


class _WorkerTransportActionState(str, Enum):
    """Transport-owned state for one registered action identifier."""

    RESERVED = "reserved"
    RUNNING = "running"
    CANCELLATION_TOMBSTONE = "cancellation_tombstone"
    TERMINAL = "terminal"
    FINALIZED = "finalized"


@dataclass(slots=True)
class _WorkerTransportActionRecord:
    state: _WorkerTransportActionState
    retention_seconds: int
    expires_at: float | None = None
    finalize_requested: bool = False


@dataclass(frozen=True, slots=True)
class _RetainedCancellation:
    result: ActionCancellationResult
    expires_at: float


class ControlledWorkerTransport:
    """Deterministic test transport for the closed worker-gateway contract."""

    def __init__(
        self,
        *,
        identities: dict[str, WorkerIdentity],
        result: WorkerExecutionResult | None = None,
        execution_hook: Callable[[WorkerInvocation], WorkerExecutionResult]
        | None = None,
        progress_hook: Callable[[WorkerProgressSink], None] | None = None,
        on_cancel: Callable[[str], ActionCancellationResult | None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.identities = dict(identities)
        self.result = result or WorkerExecutionResult.completed()
        self.execution_hook = execution_hook
        self.progress_hook = progress_hook
        self.on_cancel = on_cancel
        self.invocations: list[WorkerInvocation] = []
        self.executions: list[TerminalAction] = []
        self.cancelled: list[str] = []
        self.registrations: list[tuple[str, int, int]] = []
        self.finalizations: list[str] = []
        self._clock = clock or monotonic
        self._action_state_lock = RLock()
        self._action_states: dict[str, _WorkerTransportActionRecord] = {}
        self._cancel_results: dict[str, _RetainedCancellation] = {}

    def register_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        """Reserve an action ID before the gateway exposes its handle."""

        with self._action_state_lock:
            self._prune_expired_locked()
            if action_id in self._action_states:
                raise ActionDispatcherError(
                    f"worker action {action_id} is already registered",
                    may_have_dispatched=True,
                )
            self._validate_retention(retention_seconds)
            self.registrations.append((action_id, timeout_seconds, retention_seconds))
            self._action_states[action_id] = _WorkerTransportActionRecord(
                state=_WorkerTransportActionState.RESERVED,
                retention_seconds=retention_seconds,
            )

    def authenticate(
        self, *, selected_host: str, timeout_seconds: int
    ) -> WorkerIdentity:
        identity = self.identities.get(selected_host)
        if identity is None:
            raise ActionDispatcherError(
                f"selected execution host {selected_host} is unavailable"
            )
        return identity

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        with self._action_state_lock:
            self._prune_expired_locked()
            record = self._action_states.get(invocation.action_id)
            state = record.state if record is not None else None
            if state is _WorkerTransportActionState.CANCELLATION_TOMBSTONE:
                return WorkerExecutionResult(
                    status=WorkerExecutionStatus.CANCELLED,
                    process_tree_stopped=True,
                )
            if state is not _WorkerTransportActionState.RESERVED:
                raise ActionDispatcherError(
                    f"worker action {invocation.action_id} was not executable",
                    may_have_dispatched=state
                    in {
                        _WorkerTransportActionState.RUNNING,
                        _WorkerTransportActionState.TERMINAL,
                    },
                )
            assert record is not None
            record.state = _WorkerTransportActionState.RUNNING
            self.invocations.append(invocation)
            self.executions.append(invocation.action)
        try:
            if self.progress_hook is not None:
                self.progress_hook(progress)
            if self.execution_hook is not None:
                return self.execution_hook(invocation)
            return self.result
        finally:
            with self._action_state_lock:
                record = self._action_states.get(invocation.action_id)
                if record is not None:
                    record.state = (
                        _WorkerTransportActionState.FINALIZED
                        if record.finalize_requested
                        else _WorkerTransportActionState.TERMINAL
                    )
                    record.expires_at = self._clock() + record.retention_seconds

    def cancel(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> ActionCancellationResult:
        with self._action_state_lock:
            self._prune_expired_locked()
            cached = self._cancel_results.get(action_id)
            if cached is not None:
                return cached.result
            self._validate_retention(retention_seconds)
            record = self._action_states.get(action_id)
            state = record.state if record is not None else None
            if state in {
                None,
                _WorkerTransportActionState.RESERVED,
                _WorkerTransportActionState.CANCELLATION_TOMBSTONE,
            }:
                if record is None:
                    record = _WorkerTransportActionRecord(
                        state=_WorkerTransportActionState.CANCELLATION_TOMBSTONE,
                        retention_seconds=retention_seconds,
                    )
                    self._action_states[action_id] = record
                else:
                    record.state = _WorkerTransportActionState.CANCELLATION_TOMBSTONE
                record.expires_at = self._clock() + record.retention_seconds
                result = ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
                self._retain_cancellation_locked(action_id, result, record)
                self.cancelled.append(action_id)
                return result
            if state is _WorkerTransportActionState.FINALIZED:
                result = ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
                assert record is not None
                self._retain_cancellation_locked(action_id, result, record)
                self.cancelled.append(action_id)
                return result
            if state is _WorkerTransportActionState.TERMINAL:
                result = ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
                assert record is not None
                self._retain_cancellation_locked(action_id, result, record)
                self.cancelled.append(action_id)
                return result
            self.cancelled.append(action_id)
        if self.on_cancel is not None:
            result = self.on_cancel(action_id)
            if result is not None:
                if not isinstance(result, ActionCancellationResult):
                    raise TypeError(
                        "controlled cancellation must return a typed result"
                    )
            else:
                result = ActionCancellationResult(ActionCancellationStatus.STOPPED)
        else:
            result = ActionCancellationResult(ActionCancellationStatus.STOPPED)
        with self._action_state_lock:
            record = self._action_states.get(action_id)
            if record is not None:
                self._retain_cancellation_locked(action_id, result, record)
        return result

    def finalize_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        """Retire terminal state while fencing delayed registration/execution."""

        del timeout_seconds  # The controlled transport has no remote hop.
        with self._action_state_lock:
            self._prune_expired_locked()
            self._validate_retention(retention_seconds)
            record = self._action_states.get(action_id)
            if record is None:
                record = _WorkerTransportActionRecord(
                    state=_WorkerTransportActionState.FINALIZED,
                    retention_seconds=retention_seconds,
                )
                self._action_states[action_id] = record
            elif record.state is _WorkerTransportActionState.RUNNING:
                if not record.finalize_requested:
                    record.finalize_requested = True
                    self.finalizations.append(action_id)
                record.expires_at = self._clock() + record.retention_seconds
                return
            else:
                if record.state is _WorkerTransportActionState.FINALIZED:
                    return
                record.state = _WorkerTransportActionState.FINALIZED
            record.expires_at = self._clock() + record.retention_seconds
            self._cancel_results.pop(action_id, None)
            self.finalizations.append(action_id)

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        expired_cancellations = tuple(
            action_id
            for action_id, retained in self._cancel_results.items()
            if retained.expires_at <= now
        )
        for action_id in expired_cancellations:
            self._cancel_results.pop(action_id, None)
        expired = tuple(
            action_id
            for action_id, record in self._action_states.items()
            if record.expires_at is not None and record.expires_at <= now
        )
        for action_id in expired:
            self._action_states.pop(action_id, None)
            self._cancel_results.pop(action_id, None)

    @staticmethod
    def _validate_retention(retention_seconds: int) -> None:
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or retention_seconds < 1
        ):
            raise ValueError("worker action-state retention must be positive")

    def _retain_cancellation_locked(
        self,
        action_id: str,
        result: ActionCancellationResult,
        record: _WorkerTransportActionRecord,
    ) -> None:
        expires_at = record.expires_at
        if expires_at is None:
            expires_at = self._clock() + record.retention_seconds
        self._cancel_results[action_id] = _RetainedCancellation(
            result=result,
            expires_at=expires_at,
        )
