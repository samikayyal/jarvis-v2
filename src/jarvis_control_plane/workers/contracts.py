"""Shared typed contracts for the closed worker gateway seam.

The contract module contains only values and protocols shared by the gateway
and its host-specific transport adapters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..ports import ActionCancellationResult, ActionDispatcherError
from ..terminal_policy import TerminalAction


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """One authenticated registered execution-worker connection identity.

    ``connection_id`` is the worker boot or authenticated-session binding. It
    is required because host and worker identifiers alone do not detect a
    disconnect followed by a different live connection.
    """

    host: str
    worker_id: str
    connection_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("worker identity host must be non-blank")
        if not isinstance(self.worker_id, str) or not self.worker_id.strip():
            raise ValueError("worker identity identifier must be non-blank")
        if not isinstance(self.connection_id, str) or not self.connection_id.strip():
            raise ValueError("worker connection identifier must be non-blank")
        object.__setattr__(self, "host", self.host.strip())
        object.__setattr__(self, "worker_id", self.worker_id.strip())
        object.__setattr__(self, "connection_id", self.connection_id.strip())


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
    registration_timeout_seconds: int = 10
    action_state_retention_seconds: int = 15 * 60
    progress_event_limit: int = 128
    milestone_limit_bytes: int = 4 * 1024

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (
                self.deadline_seconds,
                self.stdout_limit_bytes,
                self.stderr_limit_bytes,
                self.cancellation_grace_seconds,
                self.authentication_timeout_seconds,
                self.registration_timeout_seconds,
                self.action_state_retention_seconds,
                self.progress_event_limit,
                self.milestone_limit_bytes,
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
        if not 1 <= self.registration_timeout_seconds <= 30:
            raise ValueError(
                "worker registration timeout must be between one and 30 seconds"
            )
        if not self.registration_timeout_seconds <= self.action_state_retention_seconds:
            raise ValueError(
                "worker action-state retention must cover registration timeout"
            )
        if self.action_state_retention_seconds > 30 * 24 * 60 * 60:
            raise ValueError("worker action-state retention cannot exceed 30 days")
        if self.stdout_limit_bytes != 1024 * 1024:
            raise ValueError("worker stdout limit is fixed at one MiB")
        if self.stderr_limit_bytes != 1024 * 1024:
            raise ValueError("worker stderr limit is fixed at one MiB")
        if not 1 <= self.cancellation_grace_seconds <= 30:
            raise ValueError(
                "worker cancellation grace must be between one and 30 seconds"
            )
        if not 1 <= self.progress_event_limit <= 512:
            raise ValueError("worker progress event limit must be between one and 512")
        if not 1 <= self.milestone_limit_bytes <= 16 * 1024:
            raise ValueError(
                "worker milestone limit must be between one byte and 16 KiB"
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
    progress_event_limit: int
    milestone_limit_bytes: int
    worker_identity: WorkerIdentity


class WorkerProgressKind(str, Enum):
    """Typed progress kinds emitted before the terminal execution result."""

    READY = "ready"
    MILESTONE = "milestone"
    OUTPUT = "output"


class WorkerOutputStream(str, Enum):
    """The only output streams a worker may tag in a progress event."""

    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class WorkerProgressEvent:
    """One ordered, bounded progress event; readiness is gateway-owned."""

    sequence: int
    kind: WorkerProgressKind | str
    text: str = ""
    stream: WorkerOutputStream | str | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("worker progress sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("worker progress sequence must be positive")
        kind = WorkerProgressKind(self.kind)
        object.__setattr__(self, "kind", kind)
        if not isinstance(self.text, str):
            raise TypeError("worker progress text must be text")
        if not isinstance(self.truncated, bool):
            raise TypeError("worker progress truncation marker must be boolean")
        if kind is WorkerProgressKind.READY:
            if self.sequence != 1:
                raise ValueError("worker readiness must be the first progress event")
            if self.text:
                raise ValueError("worker readiness cannot contain a payload")
            if self.truncated:
                raise ValueError("worker readiness cannot be truncated")
        if self.stream is not None:
            object.__setattr__(self, "stream", WorkerOutputStream(self.stream))
        if kind is WorkerProgressKind.OUTPUT and self.stream is None:
            raise ValueError("worker output progress must name stdout or stderr")
        if kind is not WorkerProgressKind.OUTPUT and self.stream is not None:
            raise ValueError("only output progress may name a stream")

    @classmethod
    def ready(cls, sequence: int = 1) -> WorkerProgressEvent:
        return cls(sequence=sequence, kind=WorkerProgressKind.READY)


WorkerProgressSink = Callable[[WorkerProgressEvent], None]


@dataclass(frozen=True, slots=True)
class WorkerExecutionResult:
    """Bounded, explicit progress and outcome returned by a worker transport."""

    status: WorkerExecutionStatus
    started_components: tuple[int, ...] = ()
    completed_components: tuple[int, ...] = ()
    process_tree_stopped: bool = False
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    progress_events: tuple[WorkerProgressEvent, ...] = ()

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
        if not isinstance(self.stdout_truncated, bool) or not isinstance(
            self.stderr_truncated, bool
        ):
            raise TypeError("worker output truncation facts must be boolean")
        progress_events = tuple(self.progress_events)
        if any(not isinstance(event, WorkerProgressEvent) for event in progress_events):
            raise TypeError("worker progress must contain WorkerProgressEvent values")
        object.__setattr__(self, "progress_events", progress_events)

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
    """Authenticated, readiness-checking transport for one registered worker.

    ``register_execution`` creates a transport-owned pending record for the
    action identifier and must enforce the supplied deadline internally. The
    action-state retention bound is part of the reservation contract: terminal
    states and cancellation tombstones may be retained for that period, but
    not indefinitely. ``execute`` and ``cancel`` must atomically compete for
    the record: cancellation that wins creates a tombstone, returns
    ``NOT_STARTED``, and prevents any later execute for the same identifier
    from starting. ``finalize_execution`` is the broker's explicit retirement
    handshake after durable terminal reconciliation. It must make a late
    registration or execution for the finalized identifier non-executable and
    may retain only a bounded retirement marker. Action identifiers are never
    reused by the broker after finalization. ``authenticate`` must perform the
    transport's readiness probe and enforce its supplied deadline internally.
    ``cancel`` has the same deadline obligation; the gateway never abandons an
    unbounded transport call in a helper thread. All methods must return an
    identity, execution result, or cancellation result only when the transport
    has enough evidence to do so.
    """

    def register_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None: ...

    def authenticate(
        self, *, selected_host: str, timeout_seconds: int
    ) -> WorkerIdentity: ...

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult: ...

    def cancel(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> ActionCancellationResult: ...

    def finalize_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None: ...


def _result_message(result: WorkerExecutionResult) -> str:
    started = ",".join(str(index + 1) for index in result.started_components) or "none"
    completed = (
        ",".join(str(index + 1) for index in result.completed_components) or "none"
    )
    return (
        f"worker reported {result.status.value}; components started: {started}; "
        f"components completed: {completed}"
    )
