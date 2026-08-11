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
from typing import Protocol, cast

from .models import FrozenActionProposal
from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
    WorkerReadiness,
)
from .terminal_policy import (
    TerminalAction,
    TerminalComponent,
    terminal_action_from_proposal,
)


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


class _WorkerDispatchHandle:
    """One registered worker execution that can be cancelled before it starts."""

    def __init__(
        self,
        *,
        action: FrozenActionProposal,
        terminal: TerminalAction,
        worker: WorkerTransport,
        expected_identity: WorkerIdentity,
        limits: WorkerExecutionLimits,
        unregister: Callable[[str, _WorkerDispatchHandle], None],
        forget: Callable[[str, _WorkerDispatchHandle], None],
    ) -> None:
        self.action = action
        self.terminal = terminal
        self.worker = worker
        self.expected_identity = expected_identity
        self.limits = limits
        self._unregister = unregister
        self._forget = forget
        self._lock = RLock()
        self._cancel_lock = RLock()
        self._cancel_requested = Event()
        self._wake = Event()
        self._finished = Event()
        self._run_called = False
        self._execute_submitted = False
        self._cancel_result: ActionCancellationResult | None = None
        self._finalize_requested = False
        self._transport_finalized = False
        self._result: WorkerExecutionResult | None = None
        self._failure: BaseException | None = None
        self._progress_lock = RLock()
        self._progress_events: list[WorkerProgressEvent] = []
        self._progress_stdout_bytes = 0
        self._progress_stderr_bytes = 0
        self._progress_milestone_bytes = 0

    def run(self) -> WorkerExecutionResult:
        with self._lock:
            if self._run_called:
                raise ActionDispatcherError(
                    "worker dispatch handle was already consumed",
                    may_have_dispatched=True,
                )
            if self._finalize_requested or self._transport_finalized:
                raise ActionDispatcherError(
                    "worker dispatch handle was finalized before execution"
                )
            self._run_called = True
            if self._cancel_requested.is_set():
                self._unregister(self.action.action_id, self)
                raise WorkerExecutionError(self._not_started_result())

        def execute() -> None:
            try:
                actual = self._authenticate_before_start()
                with self._lock:
                    if self._cancel_requested.is_set():
                        self._result = self._not_started_result()
                        return
                invocation = WorkerInvocation(
                    action_id=self.action.action_id,
                    action=self.terminal,
                    interactive=False,
                    deadline_seconds=self.limits.deadline_seconds,
                    stdout_limit_bytes=self.limits.stdout_limit_bytes,
                    stderr_limit_bytes=self.limits.stderr_limit_bytes,
                    cancellation_grace_seconds=self.limits.cancellation_grace_seconds,
                    progress_event_limit=self.limits.progress_event_limit,
                    milestone_limit_bytes=self.limits.milestone_limit_bytes,
                    worker_identity=actual,
                )
                with self._lock:
                    if self._cancel_requested.is_set():
                        self._result = self._not_started_result()
                        return
                    # This records only that the transport call was submitted;
                    # the worker transport owns the pending/running/tombstone
                    # boundary and is the sole authority for component progress.
                    self._execute_submitted = True
                self._record_gateway_ready()
                self._result = self.worker.execute(invocation, self._record_progress)
            except BaseException as exc:  # noqa: BLE001 - preserve transport boundary
                self._failure = exc
            finally:
                self._finished.set()
                self._wake.set()
                self._unregister(self.action.action_id, self)
                self._finalize_if_requested()

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
            with self._lock:
                execute_submitted = self._execute_submitted
            if not execute_submitted and isinstance(
                self._failure, ActionDispatcherError
            ):
                raise self._failure
            if not execute_submitted:
                raise ActionDispatcherError(
                    "worker authentication failed before execution"
                ) from self._failure
            raise WorkerExecutionError(self._unknown_result()) from self._failure
        if self._result is None:
            raise ActionDispatcherError(
                "worker completed without a terminal result",
                may_have_dispatched=True,
            )
        try:
            result = _bounded_result(
                self._result,
                limits=self.limits,
                components=tuple(
                    cast(TerminalComponent, component)
                    for component in self.terminal.components
                ),
            )
        except ActionDispatcherError as exc:
            raise WorkerExecutionError(self._unknown_result()) from exc
        result = replace(result, progress_events=self._progress_events_snapshot())
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
                if self._finished.is_set():
                    # The result may already be a successful external side
                    # effect even though the control plane has not persisted it.
                    result = ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
                    self._cancel_result = result
                    return result
                run_called = self._run_called
                self._cancel_requested.set()
                self._wake.set()

            try:
                value = self.worker.cancel(
                    action_id=self.action.action_id,
                    timeout_seconds=self.limits.cancellation_grace_seconds,
                    retention_seconds=self.limits.action_state_retention_seconds,
                )
            except (ActionDispatcherError, TimeoutError, TypeError, ValueError):
                value = None
            if not isinstance(value, ActionCancellationResult):
                result = ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
            else:
                result = value
            with self._lock:
                self._cancel_result = result
            if not run_called:
                self._unregister(self.action.action_id, self)
            return result

    def finalize(self) -> None:
        """Retire the transport state after durable broker reconciliation."""

        with self._lock:
            if self._transport_finalized:
                return
            self._finalize_requested = True
            if self._execute_submitted and not self._finished.is_set():
                return
        self._finalize_transport()

    def _finalize_if_requested(self) -> None:
        with self._lock:
            if not self._finalize_requested or self._transport_finalized:
                return
        self._finalize_transport()

    def _finalize_transport(self) -> None:
        try:
            self.worker.finalize_execution(
                action_id=self.action.action_id,
                timeout_seconds=self.limits.registration_timeout_seconds,
                retention_seconds=self.limits.action_state_retention_seconds,
            )
        except BaseException:  # noqa: BLE001 - cleanup cannot change the outcome
            # The transport owns the bounded fallback retention policy. A
            # cleanup failure must not change the already durable action result.
            return
        with self._lock:
            self._transport_finalized = True
        self._forget(self.action.action_id, self)

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
            progress_events=self._progress_events_snapshot(),
        )

    def _authenticate_before_start(self) -> WorkerIdentity:
        """Verify the exact registered connection at the execution barrier."""

        try:
            actual = self.worker.authenticate(
                selected_host=self.terminal.host,
                timeout_seconds=self.limits.authentication_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ActionDispatcherError(
                f"selected execution host {self.terminal.host} authentication timed out"
            ) from exc
        except ActionDispatcherError:
            raise
        except BaseException as exc:
            raise ActionDispatcherError(
                f"selected execution host {self.terminal.host} authentication failed"
            ) from exc
        if not isinstance(actual, WorkerIdentity):
            raise ActionDispatcherError(
                f"selected execution host {self.terminal.host} returned an invalid identity"
            )
        if actual != self.expected_identity:
            raise ActionDispatcherError(
                f"selected execution host {self.terminal.host} did not authenticate as its registered worker connection"
            )
        return actual

    def _record_gateway_ready(self) -> None:
        """Record the one payload-free readiness event owned by the gateway."""

        self._append_progress(WorkerProgressEvent.ready(), gateway_owned=True)

    def _record_progress(self, event: WorkerProgressEvent) -> None:
        """Validate worker ordering and enforce bounded, tagged progress material."""

        self._append_progress(event, gateway_owned=False)

    def _append_progress(
        self, event: WorkerProgressEvent, *, gateway_owned: bool
    ) -> None:
        """Append one event after applying its source-specific invariants."""

        if not isinstance(event, WorkerProgressEvent):
            raise ActionDispatcherError("worker returned an invalid progress event")
        with self._progress_lock:
            expected_sequence = len(self._progress_events) + 1
            if event.sequence != expected_sequence:
                raise ActionDispatcherError(
                    "worker progress sequence was not contiguous"
                )
            if len(self._progress_events) >= self.limits.progress_event_limit:
                raise ActionDispatcherError("worker progress event limit exceeded")
            if event.kind is WorkerProgressKind.READY and not gateway_owned:
                raise ActionDispatcherError(
                    "worker cannot publish readiness; the gateway owns the first event"
                )
            if event.kind is WorkerProgressKind.MILESTONE:
                encoded = len(event.text.encode())
                if (
                    self._progress_milestone_bytes + encoded
                    > self.limits.milestone_limit_bytes
                ):
                    raise ActionDispatcherError(
                        "worker milestone output limit exceeded"
                    )
                self._progress_milestone_bytes += encoded
            elif event.kind is WorkerProgressKind.OUTPUT:
                if event.stream is WorkerOutputStream.STDOUT:
                    limit = self.limits.stdout_limit_bytes
                    available = limit - self._progress_stdout_bytes
                else:
                    limit = self.limits.stderr_limit_bytes
                    available = limit - self._progress_stderr_bytes
                text = event.text
                truncated = event.truncated
                if len(text.encode()) > max(available, 0):
                    text = _truncate_output(text, max(available, 0))
                    truncated = True
                encoded = len(text.encode())
                if event.stream is WorkerOutputStream.STDOUT:
                    self._progress_stdout_bytes += encoded
                else:
                    self._progress_stderr_bytes += encoded
                event = replace(event, text=text, truncated=truncated)
            self._progress_events.append(event)

    def _progress_events_snapshot(self) -> tuple[WorkerProgressEvent, ...]:
        with self._progress_lock:
            return tuple(self._progress_events)

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
        self._handles: dict[str, _WorkerDispatchHandle] = {}
        self._lock = RLock()

    def current(self) -> WorkerReadiness:
        """Authenticate both worker seams and publish only safe readiness labels."""

        levels: dict[str, str] = {}
        for host in ("ubuntu", "windows"):
            worker = self._workers.get(host)
            expected = self._registered_identities.get(host)
            if worker is None or expected is None:
                levels[host] = "unavailable"
                continue
            try:
                identity = worker.authenticate(
                    selected_host=host,
                    timeout_seconds=self._limits.authentication_timeout_seconds,
                )
            except (
                ActionDispatcherError,
                OSError,
                TimeoutError,
                TypeError,
                ValueError,
            ):
                levels[host] = "unavailable"
            else:
                levels[host] = "ready" if identity == expected else "unavailable"
        return WorkerReadiness(
            ubuntu=levels["ubuntu"],
            windows=levels["windows"],
        )

    def prepare(self, action: FrozenActionProposal) -> ActionDispatchHandle:
        terminal = _terminal_action(action)
        worker = self._workers.get(terminal.host)
        expected = self._registered_identities.get(terminal.host)
        if worker is None or expected is None:
            raise ActionDispatcherError(
                f"selected execution host {terminal.host} has no registered worker"
            )
        handle = _WorkerDispatchHandle(
            action=action,
            terminal=terminal,
            worker=worker,
            expected_identity=expected,
            limits=self._limits,
            unregister=self._unregister,
            forget=self._forget_handle,
        )
        with self._lock:
            if action.action_id in self._handles:
                raise ActionDispatcherError(
                    f"worker action {action.action_id} is already prepared",
                    may_have_dispatched=True,
                )
            # Publish the local cancellation handle before entering the
            # transport. A bounded or stalled registration must remain
            # cancellable and cannot hide behind this lock.
            self._handles[action.action_id] = handle
            self._running[action.action_id] = handle
        try:
            worker.register_execution(
                action_id=action.action_id,
                timeout_seconds=self._limits.registration_timeout_seconds,
                retention_seconds=self._limits.action_state_retention_seconds,
            )
        except TimeoutError as exc:
            handle.finalize()
            raise ActionDispatcherError(
                f"worker action {action.action_id} registration timed out"
            ) from exc
        except ActionDispatcherError:
            handle.finalize()
            raise
        except (TypeError, ValueError) as exc:
            handle.finalize()
            raise ActionDispatcherError(
                f"worker action {action.action_id} registration failed"
            ) from exc
        except BaseException as exc:
            handle.finalize()
            raise ActionDispatcherError(
                f"worker action {action.action_id} registration failed"
            ) from exc
        return handle

    def dispatch(self, action: FrozenActionProposal) -> WorkerExecutionResult:
        """Compatibility helper for direct gateway callers."""

        return self.prepare(action).run()  # type: ignore[no-any-return]

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        """Cancel a prepared or running action through its typed lifecycle."""

        with self._lock:
            running = self._running.get(action_id)
        if running is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return running.cancel()

    def finalize(self, *, action_id: str) -> None:
        """Retire transport state after the broker durably closes an action."""

        with self._lock:
            handle = self._handles.get(action_id)
        if handle is not None:
            handle.finalize()

    def _unregister(self, action_id: str, handle: _WorkerDispatchHandle) -> None:
        with self._lock:
            if self._running.get(action_id) is handle:
                del self._running[action_id]

    def _forget_handle(self, action_id: str, handle: _WorkerDispatchHandle) -> None:
        with self._lock:
            if self._handles.get(action_id) is handle:
                del self._handles[action_id]


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
    components: tuple[TerminalComponent, ...],
) -> WorkerExecutionResult:
    if not isinstance(result, WorkerExecutionResult):
        raise ActionDispatcherError("worker returned an invalid execution result")
    component_count = len(components)
    if any(index >= component_count for index in result.started_components):
        raise ActionDispatcherError("worker reported an unknown command component")
    if result.status is WorkerExecutionStatus.COMPLETED and result.started_components:
        started = set(result.started_components)
        if result.completed_components != result.started_components:
            raise ActionDispatcherError("worker reported incomplete completed progress")
        if 0 not in started or any(
            component.operator_before == ";" and index not in started
            for index, component in enumerate(components)
        ):
            raise ActionDispatcherError("worker reported impossible compound progress")
        if any(
            component.operator_before == "|"
            and ((index in started) != (index - 1 in started))
            for index, component in enumerate(components)
        ):
            raise ActionDispatcherError("worker reported a partial pipeline")
    stdout_overflow = len(result.stdout.encode()) > limits.stdout_limit_bytes
    stderr_overflow = len(result.stderr.encode()) > limits.stderr_limit_bytes
    return replace(
        result,
        stdout=_truncate_output(result.stdout, limits.stdout_limit_bytes),
        stderr=_truncate_output(result.stderr, limits.stderr_limit_bytes),
        stdout_truncated=result.stdout_truncated or stdout_overflow,
        stderr_truncated=result.stderr_truncated or stderr_overflow,
    )


def _truncate_output(value: str, limit: int) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    suffix = "\n[truncated]"
    if limit <= 0:
        return ""
    if limit <= len(suffix.encode()):
        return encoded[:limit].decode(errors="ignore")
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
