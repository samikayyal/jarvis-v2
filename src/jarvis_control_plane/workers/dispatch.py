"""Cancellable gateway dispatch handle implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from threading import Event, RLock, Thread
from time import monotonic
from typing import cast

from ..models import FrozenActionProposal
from ..ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
)
from ..terminal_policy import TerminalAction, TerminalComponent
from .contracts import (
    WorkerExecutionError,
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerIdentity,
    WorkerInvocation,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerTransport,
)
from .gateway_validation import _bounded_result, _truncate_output


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
