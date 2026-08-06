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
from typing import Protocol

from .models import FrozenActionProposal
from .ports import ActionDispatcherError
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

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (
                self.deadline_seconds,
                self.stdout_limit_bytes,
                self.stderr_limit_bytes,
                self.cancellation_grace_seconds,
            )
        ):
            raise TypeError("worker execution limits must be integers")
        if not 1 <= self.deadline_seconds <= 10 * 60:
            raise ValueError(
                "worker deadline must be between one second and ten minutes"
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
        object.__setattr__(self, "status", WorkerExecutionStatus(self.status))
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

    def cancel(self, *, action_id: str) -> None: ...


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
        self._running: dict[str, tuple[object, WorkerTransport]] = {}
        self._lock = RLock()

    def dispatch(self, action: FrozenActionProposal) -> WorkerExecutionResult:
        terminal = _terminal_action(action)
        worker = self._workers.get(terminal.host)
        expected = self._registered_identities.get(terminal.host)
        if worker is None or expected is None:
            raise ActionDispatcherError(
                f"selected execution host {terminal.host} has no registered worker"
            )
        actual = worker.authenticate(selected_host=terminal.host)
        if actual != expected:
            raise ActionDispatcherError(
                f"selected execution host {terminal.host} did not authenticate as its registered worker"
            )
        invocation = WorkerInvocation(
            action_id=action.action_id,
            action=terminal,
            interactive=False,
            deadline_seconds=self._limits.deadline_seconds,
            stdout_limit_bytes=self._limits.stdout_limit_bytes,
            stderr_limit_bytes=self._limits.stderr_limit_bytes,
            cancellation_grace_seconds=self._limits.cancellation_grace_seconds,
        )
        finished = Event()
        result: WorkerExecutionResult | None = None
        failure: BaseException | None = None
        execution = object()
        with self._lock:
            self._running[action.action_id] = (execution, worker)

        def execute() -> None:
            nonlocal failure, result
            try:
                result = worker.execute(invocation)
            except BaseException as exc:  # noqa: BLE001 - preserve transport boundary
                failure = exc
            finally:
                finished.set()
                with self._lock:
                    if self._running.get(action.action_id) == (execution, worker):
                        del self._running[action.action_id]

        thread = Thread(target=execute, daemon=True)
        try:
            thread.start()
        except BaseException as exc:
            with self._lock:
                if self._running.get(action.action_id) == (execution, worker):
                    del self._running[action.action_id]
            raise ActionDispatcherError(
                "worker execution thread could not start"
            ) from exc
        deadline_expired = not finished.wait(timeout=self._limits.deadline_seconds)
        if deadline_expired:
            worker.cancel(action_id=action.action_id)
            if not finished.wait(timeout=self._limits.cancellation_grace_seconds):
                unknown = WorkerExecutionResult(
                    status=WorkerExecutionStatus.UNKNOWN,
                    started_components=(0,),
                )
                raise WorkerExecutionError(unknown)
        if failure is not None:
            raise WorkerExecutionError(
                WorkerExecutionResult(status=WorkerExecutionStatus.UNKNOWN)
            ) from failure
        if result is None:
            raise ActionDispatcherError(
                "worker completed without a terminal result",
                may_have_dispatched=True,
            )
        result = _bounded_result(
            result,
            limits=self._limits,
            component_count=len(terminal.components),
        )
        if deadline_expired and result.status is not WorkerExecutionStatus.UNKNOWN:
            result = replace(
                result,
                status=(
                    WorkerExecutionStatus.TIMED_OUT
                    if result.process_tree_stopped
                    else WorkerExecutionStatus.UNKNOWN
                ),
            )
        if result.status is WorkerExecutionStatus.COMPLETED:
            return result
        raise WorkerExecutionError(result)

    def cancel(self, *, action_id: str) -> None:
        """Forward cancellation only to a worker currently executing that action."""

        with self._lock:
            running = self._running.get(action_id)
        if running is not None:
            _, worker = running
            worker.cancel(action_id=action_id)


class ControlledWorkerTransport:
    """Deterministic test transport for the closed worker-gateway contract."""

    def __init__(
        self,
        *,
        identities: dict[str, WorkerIdentity],
        result: WorkerExecutionResult | None = None,
        execution_hook: Callable[[WorkerInvocation], WorkerExecutionResult]
        | None = None,
        on_cancel: Callable[[str], None] | None = None,
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

    def cancel(self, *, action_id: str) -> None:
        self.cancelled.append(action_id)
        if self.on_cancel is not None:
            self.on_cancel(action_id)


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
