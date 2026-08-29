"""Ubuntu process-scope protocols, controlled adapter, and composition."""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Protocol

from ..ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
)
from .contracts import (
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerInvocation,
    WorkerProgressEvent,
    WorkerProgressSink,
)
from .ubuntu_process_execution import _UbuntuProcessExecutionMixin
from .ubuntu_process_lifecycle import _UbuntuProcessLifecycleMixin


class UbuntuProcessScope(Protocol):
    """Least-privileged process-scope boundary owned by the native worker."""

    def reserve(self, *, action_id: str) -> None: ...

    def retire(self, *, action_id: str) -> None: ...

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult: ...

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult: ...


class ControlledUbuntuProcessScope:
    """Controlled process scope with the same typed boundary as Ubuntu."""

    def __init__(
        self,
        *,
        result: WorkerExecutionResult | None = None,
        progress_events: tuple[WorkerProgressEvent, ...] = (),
        execution_hook: Callable[[WorkerInvocation], WorkerExecutionResult]
        | None = None,
        cancellation_hook: Callable[[str, int], ActionCancellationResult] | None = None,
    ) -> None:
        self.result = result or WorkerExecutionResult.completed()
        self.progress_events = tuple(progress_events)
        self.execution_hook = execution_hook
        self.cancellation_hook = cancellation_hook
        self.invocations: list[WorkerInvocation] = []
        self.cancellations: list[tuple[str, int]] = []
        self._lock = RLock()
        self._reserved: set[str] = set()
        self._running: set[str] = set()
        self._cancelled: set[str] = set()

    def reserve(self, *, action_id: str) -> None:
        with self._lock:
            if action_id in self._reserved | self._running | self._cancelled:
                raise ActionDispatcherError(
                    f"Ubuntu process scope {action_id} is already reserved"
                )
            self._reserved.add(action_id)

    def retire(self, *, action_id: str) -> None:
        with self._lock:
            self._reserved.discard(action_id)
            self._cancelled.discard(action_id)

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        with self._lock:
            if invocation.action_id in self._cancelled:
                return WorkerExecutionResult(
                    status=WorkerExecutionStatus.CANCELLED,
                    process_tree_stopped=True,
                )
            if invocation.action_id not in self._reserved:
                raise ActionDispatcherError("Ubuntu process scope was not reserved")
            self._reserved.remove(invocation.action_id)
            self._running.add(invocation.action_id)
            self.invocations.append(invocation)
        try:
            for event in self.progress_events:
                progress(event)
            if self.execution_hook is not None:
                return self.execution_hook(invocation)
            return self.result
        finally:
            with self._lock:
                self._running.discard(invocation.action_id)

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult:
        self.cancellations.append((action_id, timeout_seconds))
        with self._lock:
            if action_id in self._reserved:
                self._reserved.remove(action_id)
                self._cancelled.add(action_id)
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            running = action_id in self._running
        if not running:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        if self.cancellation_hook is not None:
            return self.cancellation_hook(action_id, timeout_seconds)
        return ActionCancellationResult(ActionCancellationStatus.STOPPED)


class SystemdUbuntuProcessScope(
    _UbuntuProcessExecutionMixin, _UbuntuProcessLifecycleMixin
):
    """Run one exact command in a bounded, cancellable systemd scope."""
