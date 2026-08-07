"""Closed, host-bound worker gateway contract for terminal actions.

This module deliberately stops at the gateway boundary. Native Ubuntu and
outbound Windows transports are separate adapters; the gateway enforces the
shared authorization, identity, and bounded-execution contract they must obey.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from threading import Event, RLock, Thread
from time import monotonic
from typing import Protocol

from .models import FrozenActionProposal
from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
)
from .terminal_policy import TerminalAction, terminal_action_from_proposal


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """One authenticated registered execution-worker identity."""

    host: str
    worker_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("worker identity host must be non-blank")
        if not isinstance(self.worker_id, str) or not self.worker_id.strip():
            raise ValueError("worker identity identifier must be non-blank")
        object.__setattr__(self, "host", self.host.strip())
        object.__setattr__(self, "worker_id", self.worker_id.strip())


class WorkerExecutionStatus(str, Enum):
    """A worker's one terminal report for a started execution scope."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WorkerExecutionLimits:
    """Fixed V1 execution limits passed to every worker transport."""

    deadline_seconds: int = 120
    stdout_limit_bytes: int = 1024 * 1024
    stderr_limit_bytes: int = 1024 * 1024
    cancellation_grace_seconds: int = 10
    authentication_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (
                self.deadline_seconds,
                self.stdout_limit_bytes,
                self.stderr_limit_bytes,
                self.cancellation_grace_seconds,
                self.authentication_timeout_seconds,
            )
        ):
            raise TypeError("worker execution limits must be integers")
        if not 1 <= self.deadline_seconds <= 10 * 60:
            raise ValueError(
                "worker deadline must be between one second and ten minutes"
            )
        if not 1 <= self.authentication_timeout_seconds <= 30:
            raise ValueError(
                "worker authentication timeout must be between one and 30 seconds"
            )
        if self.stdout_limit_bytes != 1024 * 1024:
            raise ValueError("worker stdout limit is fixed at one MiB")
        if self.stderr_limit_bytes != 1024 * 1024:
            raise ValueError("worker stderr limit is fixed at one MiB")
        if not 1 <= self.cancellation_grace_seconds <= 30:
            raise ValueError(
                "worker cancellation grace must be between one and 30 seconds"
            )


@dataclass(frozen=True, slots=True)
class WorkerInvocation:
    """The complete non-interactive execution envelope sent to one worker."""

    action_id: str
    action: TerminalAction
    interactive: bool
    deadline_seconds: int
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    cancellation_grace_seconds: int


@dataclass(frozen=True, slots=True)
class WorkerExecutionResult:
    """Bounded, explicit progress and outcome returned by a worker transport."""

    status: WorkerExecutionStatus
    started_components: tuple[int, ...] = ()
    completed_components: tuple[int, ...] = ()
    process_tree_stopped: bool = False
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        status = WorkerExecutionStatus(self.status)
        for name in ("started_components", "completed_components"):
            value = tuple(getattr(self, name))
            if any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in value
            ):
                raise ValueError(f"{name} must contain non-negative component indexes")
            if tuple(sorted(set(value))) != value:
                raise ValueError(f"{name} must be ordered and unique")
            object.__setattr__(self, name, value)
        if not set(self.completed_components) <= set(self.started_components):
            raise ValueError("only started components may be completed")
        if not isinstance(self.process_tree_stopped, bool):
            raise TypeError("worker process-tree stop confirmation must be boolean")
        if (
            status is not WorkerExecutionStatus.UNKNOWN
            and not self.process_tree_stopped
        ):
            status = WorkerExecutionStatus.UNKNOWN
        object.__setattr__(self, "status", status)
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("worker output must be text")

    @classmethod
    def completed(cls, *, stdout: str = "", stderr: str = "") -> WorkerExecutionResult:
        return cls(
            status=WorkerExecutionStatus.COMPLETED,
            process_tree_stopped=True,
            stdout=stdout,
            stderr=stderr,
        )


class WorkerExecutionError(ActionDispatcherError):
    """A worker returned a terminal non-success result with bounded evidence."""

    def __init__(self, result: WorkerExecutionResult) -> None:
        super().__init__(
            _result_message(result),
            may_have_dispatched=result.status is WorkerExecutionStatus.UNKNOWN,
        )
        self.result = result


class WorkerTransport(Protocol):
    """Authenticated transport owned by exactly one registered worker."""

    def authenticate(self, *, selected_host: str) -> WorkerIdentity: ...

    def execute(self, invocation: WorkerInvocation) -> WorkerExecutionResult: ...

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult: ...


def _call_with_timeout(
    operation: Callable[[], object], *, timeout_seconds: int
) -> tuple[object | None, BaseException | None, bool]:
    """Run one transport call with an explicit daemon-thread bound."""

    finished = Event()
    result: object | None = None
    failure: BaseException | None = None

    def invoke() -> None:
        nonlocal failure, result
        try:
            result = operation()
        except BaseException as exc:  # noqa: BLE001 - transport boundary
            failure = exc
        finally:
            finished.set()

    Thread(target=invoke, daemon=True).start()
    if not finished.wait(timeout=timeout_seconds):
        return None, None, True
    return result, failure, False


class _WorkerDispatchHandle:
    """One registered worker execution that can be cancelled before it starts."""

    def __init__(
        self,
        *,
        action: FrozenActionProposal,
        terminal: TerminalAction,
        worker: WorkerTransport,
        limits: WorkerExecutionLimits,
        unregister: Callable[[str, _WorkerDispatchHandle], None],
    ) -> None:
        self.action = action
        self.terminal = terminal
        self.worker = worker
        self.limits = limits
        self._unregister = unregister
        self._lock = RLock()
        self._cancel_lock = RLock()
        self._cancel_requested = Event()
        self._wake = Event()
        self._finished = Event()
        self._run_called = False
        self._started = False
        self._cancel_result: ActionCancellationResult | None = None
        self._result: WorkerExecutionResult | None = None
        self._failure: BaseException | None = None

    def run(self) -> WorkerExecutionResult:
        with self._lock:
            if self._run_called:
                raise ActionDispatcherError(
                    "worker dispatch handle was already consumed",
                    may_have_dispatched=True,
                )
            self._run_called = True
            if self._cancel_requested.is_set():
                self._unregister(self.action.action_id, self)
                raise WorkerExecutionError(self._not_started_result())

        invocation = WorkerInvocation(
            action_id=self.action.action_id,
            action=self.terminal,
            interactive=False,
            deadline_seconds=self.limits.deadline_seconds,
            stdout_limit_bytes=self.limits.stdout_limit_bytes,
            stderr_limit_bytes=self.limits.stderr_limit_bytes,
            cancellation_grace_seconds=self.limits.cancellation_grace_seconds,
        )

        def execute() -> None:
            try:
                with self._lock:
                    # This check and the started transition are the local
                    # barrier immediately before the transport call. A
                    # cancellation that wins it cannot start worker execution.
                    if self._cancel_requested.is_set():
                        self._result = self._not_started_result()
                        return
                    self._started = True
                self._result = self.worker.execute(invocation)
            except BaseException as exc:  # noqa: BLE001 - preserve transport boundary
                self._failure = exc
            finally:
                self._finished.set()
                self._wake.set()
                self._unregister(self.action.action_id, self)

        try:
            Thread(target=execute, daemon=True).start()
        except BaseException as exc:
            self._unregister(self.action.action_id, self)
            raise ActionDispatcherError(
                "worker execution thread could not start"
            ) from exc

        wait_state = self._wait_for_completion(self.limits.deadline_seconds)
        deadline_expired = wait_state == "deadline"
        if wait_state in {"deadline", "cancelled"}:
            if wait_state == "deadline":
                self.cancel()
            if not self._finished.wait(timeout=self.limits.cancellation_grace_seconds):
                raise WorkerExecutionError(self._unknown_result())

        if self._failure is not None:
            raise WorkerExecutionError(
                WorkerExecutionResult(
                    status=WorkerExecutionStatus.UNKNOWN,
                    started_components=(0,),
                )
            ) from self._failure
        if self._result is None:
            raise ActionDispatcherError(
                "worker completed without a terminal result",
                may_have_dispatched=True,
            )
        result = _bounded_result(
            self._result,
            limits=self.limits,
            component_count=len(self.terminal.components),
        )
        if deadline_expired and result.status is not WorkerExecutionStatus.UNKNOWN:
            result = replace(result, status=WorkerExecutionStatus.TIMED_OUT)
        if result.status is WorkerExecutionStatus.COMPLETED:
            return result
        raise WorkerExecutionError(result)

    def cancel(self) -> ActionCancellationResult:
        """Request bounded worker cancellation and return its acknowledgement."""

        with self._cancel_lock:
            with self._lock:
                if self._cancel_result is not None:
                    return self._cancel_result
                if not self._started:
                    self._cancel_requested.set()
                    self._wake.set()
                    result = ActionCancellationResult(
                        ActionCancellationStatus.NOT_STARTED
                    )
                    self._cancel_result = result
                    self._unregister(self.action.action_id, self)
                    return result
                if self._finished.is_set():
                    result = ActionCancellationResult(ActionCancellationStatus.STOPPED)
                    self._cancel_result = result
                    return result
                self._cancel_requested.set()
                self._wake.set()

            value, failure, timed_out = _call_with_timeout(
                lambda: self.worker.cancel(
                    action_id=self.action.action_id,
                    timeout_seconds=self.limits.cancellation_grace_seconds,
                ),
                timeout_seconds=self.limits.cancellation_grace_seconds,
            )
            if (
                timed_out
                or failure is not None
                or not isinstance(value, ActionCancellationResult)
            ):
                result = ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
            elif value.status is ActionCancellationStatus.NOT_STARTED:
                # The local barrier already marked this execution started.
                result = ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
            else:
                result = value
            with self._lock:
                self._cancel_result = result
            return result

    def _wait_for_completion(self, timeout_seconds: int) -> str:
        deadline = monotonic() + timeout_seconds
        while not self._finished.is_set():
            if self._cancel_requested.is_set():
                return "cancelled"
            remaining = deadline - monotonic()
            if remaining <= 0:
                return "deadline"
            self._wake.wait(timeout=remaining)
        return "finished"

    def _unknown_result(self) -> WorkerExecutionResult:
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.UNKNOWN,
            started_components=(0,),
        )

    @staticmethod
    def _not_started_result() -> WorkerExecutionResult:
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.CANCELLED,
            process_tree_stopped=True,
        )


class WorkerGateway:
    """Dispatch frozen terminal actions only to their selected worker identity."""

    def __init__(
        self,
        *,
        workers: dict[str, WorkerTransport],
        registered_identities: dict[str, WorkerIdentity],
        limits: WorkerExecutionLimits | None = None,
    ) -> None:
        if set(workers) != set(registered_identities):
            raise ValueError(
                "workers and registered identities must name the same hosts"
            )
        if not workers:
            raise ValueError("worker gateway requires at least one registered worker")
        for host, identity in registered_identities.items():
            if host != identity.host:
                raise ValueError("registered worker identity must match its host key")
        self._workers = dict(workers)
        self._registered_identities = dict(registered_identities)
        self._limits = limits or WorkerExecutionLimits()
        self._running: dict[str, _WorkerDispatchHandle] = {}
        self._lock = RLock()

    def prepare(self, action: FrozenActionProposal) -> ActionDispatchHandle:
        terminal = _terminal_action(action)
        worker = self._workers.get(terminal.host)
        expected = self._registered_identities.get(terminal.host)
        if worker is None or expected is None:
            raise ActionDispatcherError(
                f"selected execution host {terminal.host} has no registered worker"
            )
        actual, failure, timed_out = _call_with_timeout(
            lambda: worker.authenticate(selected_host=terminal.host),
            timeout_seconds=self._limits.authentication_timeout_seconds,
        )
        if timed_out:
            raise ActionDispatcherError(
                f"selected execution host {terminal.host} authentication timed out"
            )
        if failure is not None:
            if isinstance(failure, ActionDispatcherError):
                raise failure
            raise ActionDispatcherError(
                f"selected execution host {terminal.host} authentication failed"
            ) from failure
        if not isinstance(actual, WorkerIdentity):
            raise ActionDispatcherError(
                f"selected execution host {terminal.host} returned an invalid identity"
            )
        if actual != expected:
            raise ActionDispatcherError(
                f"selected execution host {terminal.host} did not authenticate as its registered worker"
            )
        handle = _WorkerDispatchHandle(
            action=action,
            terminal=terminal,
            worker=worker,
            limits=self._limits,
            unregister=self._unregister,
        )
        with self._lock:
            if action.action_id in self._running:
                raise ActionDispatcherError(
                    f"worker action {action.action_id} is already prepared",
                    may_have_dispatched=True,
                )
            self._running[action.action_id] = handle
        return handle

    def dispatch(self, action: FrozenActionProposal) -> WorkerExecutionResult:
        """Compatibility helper for direct gateway callers."""

        return self.prepare(action).run()  # type: ignore[no-any-return]

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        """Cancel a prepared or running action through its typed lifecycle."""

        with self._lock:
            running = self._running.get(action_id)
        if running is None:
            return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
        return running.cancel()

    def _unregister(self, action_id: str, handle: _WorkerDispatchHandle) -> None:
        with self._lock:
            if self._running.get(action_id) is handle:
                del self._running[action_id]


class ControlledWorkerTransport:
    """Deterministic test transport for the closed worker-gateway contract."""

    def __init__(
        self,
        *,
        identities: dict[str, WorkerIdentity],
        result: WorkerExecutionResult | None = None,
        execution_hook: Callable[[WorkerInvocation], WorkerExecutionResult]
        | None = None,
        on_cancel: Callable[[str], ActionCancellationResult | None] | None = None,
    ) -> None:
        self.identities = dict(identities)
        self.result = result or WorkerExecutionResult.completed()
        self.execution_hook = execution_hook
        self.on_cancel = on_cancel
        self.invocations: list[WorkerInvocation] = []
        self.executions: list[TerminalAction] = []
        self.cancelled: list[str] = []

    def authenticate(self, *, selected_host: str) -> WorkerIdentity:
        identity = self.identities.get(selected_host)
        if identity is None:
            raise ActionDispatcherError(
                f"selected execution host {selected_host} is unavailable"
            )
        return identity

    def execute(self, invocation: WorkerInvocation) -> WorkerExecutionResult:
        self.invocations.append(invocation)
        self.executions.append(invocation.action)
        if self.execution_hook is not None:
            return self.execution_hook(invocation)
        return self.result

    def cancel(
        self, *, action_id: str, timeout_seconds: int
    ) -> ActionCancellationResult:
        self.cancelled.append(action_id)
        if self.on_cancel is not None:
            result = self.on_cancel(action_id)
            if result is not None:
                if not isinstance(result, ActionCancellationResult):
                    raise TypeError(
                        "controlled cancellation must return a typed result"
                    )
                return result
        return ActionCancellationResult(ActionCancellationStatus.STOPPED)


def _terminal_action(action: FrozenActionProposal) -> TerminalAction:
    if action.kind != "terminal":
        raise ActionDispatcherError("worker gateway accepts terminal actions only")
    try:
        return terminal_action_from_proposal(action)
    except (TypeError, ValueError) as exc:
        raise ActionDispatcherError("terminal action payload is invalid") from exc


def _bounded_result(
    result: WorkerExecutionResult,
    *,
    limits: WorkerExecutionLimits,
    component_count: int,
) -> WorkerExecutionResult:
    if not isinstance(result, WorkerExecutionResult):
        raise ActionDispatcherError("worker returned an invalid execution result")
    all_components = tuple(range(component_count))
    if result.status is WorkerExecutionStatus.COMPLETED and (
        result.started_components not in {(), all_components}
        or result.completed_components not in {(), all_components}
    ):
        raise ActionDispatcherError("worker reported incomplete progress as completed")
    if any(index >= component_count for index in result.started_components):
        raise ActionDispatcherError("worker reported an unknown command component")
    return replace(
        result,
        stdout=_truncate_output(result.stdout, limits.stdout_limit_bytes),
        stderr=_truncate_output(result.stderr, limits.stderr_limit_bytes),
    )


def _truncate_output(value: str, limit: int) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    suffix = "\n[truncated]"
    prefix = encoded[: limit - len(suffix.encode())].decode(errors="ignore")
    return f"{prefix}{suffix}"


def _result_message(result: WorkerExecutionResult) -> str:
    started = ",".join(str(index + 1) for index in result.started_components) or "none"
    completed = (
        ",".join(str(index + 1) for index in result.completed_components) or "none"
    )
    return (
        f"worker reported {result.status.value}; components started: {started}; "
        f"components completed: {completed}"
    )
