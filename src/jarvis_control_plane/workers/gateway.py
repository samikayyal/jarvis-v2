"""Shared worker gateway policy and host selection."""

from __future__ import annotations

from threading import RLock

from ..models import FrozenActionProposal
from ..ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ActionDispatchHandle,
    WorkerReadiness,
)
from .contracts import (
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerIdentity,
    WorkerTransport,
)
from .dispatch import _WorkerDispatchHandle
from .gateway_validation import _terminal_action


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
