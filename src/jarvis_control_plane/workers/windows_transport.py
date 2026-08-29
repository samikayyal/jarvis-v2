"""Identity-bound outbound transport for the Windows worker."""

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
from .contracts import (
    WorkerExecutionResult,
    WorkerIdentity,
    WorkerInvocation,
    WorkerProgressSink,
)
from .windows_job import WindowsWorkerSession
from .windows_mtls import WindowsWorkerRegistration, WindowsWorkerSessionEvidence


class _ActionState(str, Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    CANCELLATION_TOMBSTONE = "cancellation_tombstone"
    TERMINAL = "terminal"
    FINALIZED = "finalized"


@dataclass(slots=True)
class _ActionRecord:
    state: _ActionState
    session_epoch: int
    retention_seconds: int
    expires_at: float | None = None


class OutboundWindowsWorkerTransport:
    """WorkerTransport backed by one outbound, identity-bound Windows session."""

    def __init__(
        self,
        *,
        registration: WindowsWorkerRegistration,
        readiness_expiry_seconds: int = 30,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if (
            isinstance(readiness_expiry_seconds, bool)
            or not isinstance(readiness_expiry_seconds, int)
            or not 1 <= readiness_expiry_seconds <= 45
        ):
            raise ValueError(
                "Windows worker readiness expiry must be between one and 45 seconds"
            )
        self.registration = registration
        if readiness_expiry_seconds < 2 * registration.heartbeat_interval_seconds:
            raise ValueError(
                "Windows worker readiness expiry must cover two heartbeat intervals"
            )
        self.readiness_expiry_seconds = readiness_expiry_seconds
        self._clock = clock or monotonic
        self._lock = RLock()
        self._session: WindowsWorkerSession | None = None
        self._session_epoch = 0
        self._last_heartbeat: float | None = None
        self._actions: dict[str, _ActionRecord] = {}

    def attach(self, session: WindowsWorkerSession) -> None:
        """Accept one already-mTLS-authenticated outbound worker session."""

        evidence = getattr(session, "evidence", None)
        if not isinstance(evidence, WindowsWorkerSessionEvidence):
            raise ActionDispatcherError("Windows worker session evidence is invalid")
        if not self._evidence_is_authenticated(evidence):
            raise ActionDispatcherError(
                "Windows worker session evidence is not mTLS authenticated"
            )
        expected = self.registration
        if (
            evidence.worker_identity != expected.identity
            or evidence.certificate_identity != expected.certificate_identity
            or evidence.application_identity != expected.application_identity
            or evidence.heartbeat_interval_seconds
            != expected.heartbeat_interval_seconds
        ):
            raise ActionDispatcherError("Windows worker session identity mismatch")
        with self._lock:
            if self._session is not None:
                raise ActionDispatcherError(
                    "registered Windows worker already has an outbound session"
                )
            self._session_epoch += 1
            self._session = session
            self._last_heartbeat = self._clock()

    @staticmethod
    def _evidence_is_authenticated(evidence: WindowsWorkerSessionEvidence) -> bool:
        return evidence.authenticated

    def heartbeat(self, session: WindowsWorkerSession) -> None:
        """Refresh readiness only for the exact attached session object."""

        with self._lock:
            if session is not self._session:
                raise ActionDispatcherError("Windows worker heartbeat session mismatch")
            self._last_heartbeat = self._clock()

    def disconnect(self, session: WindowsWorkerSession) -> None:
        """Make the worker unavailable without transferring any reserved work."""

        with self._lock:
            if session is not self._session:
                raise ActionDispatcherError(
                    "Windows worker disconnect session mismatch"
                )
            self._session = None
            self._last_heartbeat = None
            self._session_epoch += 1

    def register_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        del timeout_seconds  # Session readiness is an in-memory accepted-session fact.
        self._validate_action_arguments(action_id, retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            self._require_ready_locked()
            if action_id in self._actions:
                raise ActionDispatcherError(
                    f"Windows worker action {action_id} is already registered",
                    may_have_dispatched=True,
                )
            if any(
                record.state in {_ActionState.RESERVED, _ActionState.RUNNING}
                for record in self._actions.values()
            ):
                raise ActionDispatcherError(
                    "registered Windows worker already has an action in progress"
                )
            self._actions[action_id] = _ActionRecord(
                state=_ActionState.RESERVED,
                session_epoch=self._session_epoch,
                retention_seconds=retention_seconds,
            )

    def authenticate(
        self, *, selected_host: str, timeout_seconds: int
    ) -> WorkerIdentity:
        del timeout_seconds
        if selected_host != self.registration.identity.host:
            raise ActionDispatcherError(
                f"selected execution host {selected_host} is unavailable"
            )
        with self._lock:
            self._require_ready_locked()
            return self.registration.identity

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        if not isinstance(invocation, WorkerInvocation):
            raise TypeError("Windows worker invocation is invalid")
        if invocation.interactive:
            raise ActionDispatcherError(
                "Windows worker execution must be non-interactive"
            )
        if invocation.worker_identity != self.registration.identity:
            raise ActionDispatcherError("Windows worker invocation identity mismatch")
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(invocation.action_id)
            if record is None or record.state is not _ActionState.RESERVED:
                if (
                    record is not None
                    and record.state is _ActionState.CANCELLATION_TOMBSTONE
                ):
                    return WorkerExecutionResult(
                        status="cancelled", process_tree_stopped=True
                    )
                raise ActionDispatcherError(
                    f"Windows worker action {invocation.action_id} was not executable",
                    may_have_dispatched=record is not None
                    and record.state in {_ActionState.RUNNING, _ActionState.TERMINAL},
                )
            session = self._require_ready_locked()
            if record.session_epoch != self._session_epoch:
                record.state = _ActionState.FINALIZED
                record.expires_at = self._clock() + record.retention_seconds
                raise ActionDispatcherError(
                    "Windows worker action reserved session disconnected"
                )
            epoch = self._session_epoch
            record.state = _ActionState.RUNNING
        try:
            result = session.execute(invocation, progress)
            if not isinstance(result, WorkerExecutionResult):
                raise TypeError("Windows worker returned an invalid terminal result")
        except BaseException as exc:
            with self._lock:
                self._mark_terminal_locked(invocation.action_id)
            if isinstance(exc, ActionDispatcherError) and exc.may_have_dispatched:
                raise
            raise ActionDispatcherError(
                "Windows worker execution outcome is unknown",
                may_have_dispatched=True,
            ) from exc
        with self._lock:
            connected_session = self._session
            connected_epoch = self._session_epoch
            self._mark_terminal_locked(invocation.action_id)
        if connected_session is not session or connected_epoch != epoch:
            raise ActionDispatcherError(
                "Windows worker disconnected after execution started",
                may_have_dispatched=True,
            )
        return result

    def cancel(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> ActionCancellationResult:
        self._validate_action_arguments(action_id, retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(action_id)
            if record is None:
                self._actions[action_id] = _ActionRecord(
                    state=_ActionState.CANCELLATION_TOMBSTONE,
                    session_epoch=self._session_epoch,
                    retention_seconds=retention_seconds,
                    expires_at=self._clock() + retention_seconds,
                )
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            if record.state in {
                _ActionState.RESERVED,
                _ActionState.CANCELLATION_TOMBSTONE,
            }:
                record.state = _ActionState.CANCELLATION_TOMBSTONE
                record.expires_at = self._clock() + record.retention_seconds
                return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
            if record.state is not _ActionState.RUNNING:
                return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
            session = self._session
            epoch_matches = record.session_epoch == self._session_epoch
        if session is None or not epoch_matches:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        try:
            stopped = session.terminate_job_object(
                action_id=action_id, timeout_seconds=timeout_seconds
            )
        except BaseException:  # noqa: BLE001 - no process-tree proof means unknown
            stopped = False
        return ActionCancellationResult(
            ActionCancellationStatus.STOPPED
            if stopped is True
            else ActionCancellationStatus.UNKNOWN
        )

    def finalize_execution(
        self,
        *,
        action_id: str,
        timeout_seconds: int,
        retention_seconds: int,
    ) -> None:
        self._validate_action_arguments(action_id, retention_seconds)
        with self._lock:
            self._prune_expired_locked()
            record = self._actions.get(action_id)
            if record is None:
                record = _ActionRecord(
                    state=_ActionState.FINALIZED,
                    session_epoch=self._session_epoch,
                    retention_seconds=retention_seconds,
                )
                self._actions[action_id] = record
            else:
                record.state = _ActionState.FINALIZED
            record.expires_at = self._clock() + record.retention_seconds
            session = (
                self._session if record.session_epoch == self._session_epoch else None
            )
        if session is not None:
            try:
                session.finalize(action_id=action_id, timeout_seconds=timeout_seconds)
            except BaseException:  # noqa: BLE001 - bounded retention is the fallback
                return

    def _require_ready_locked(self) -> WindowsWorkerSession:
        session = self._session
        heartbeat = self._last_heartbeat
        if session is None or heartbeat is None:
            raise ActionDispatcherError("registered Windows worker is unavailable")
        if self._clock() - heartbeat > self.readiness_expiry_seconds:
            raise ActionDispatcherError("registered Windows worker heartbeat expired")
        return session

    def _mark_terminal_locked(self, action_id: str) -> None:
        record = self._actions.get(action_id)
        if record is None:
            return
        record.state = _ActionState.TERMINAL
        record.expires_at = self._clock() + record.retention_seconds

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        for action_id in tuple(self._actions):
            expires_at = self._actions[action_id].expires_at
            if expires_at is not None and expires_at <= now:
                self._actions.pop(action_id, None)

    @staticmethod
    def _validate_action_arguments(action_id: str, retention_seconds: int) -> None:
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("Windows worker action identifier must be non-blank")
        if (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or retention_seconds < 1
        ):
            raise ValueError("Windows worker action-state retention must be positive")


class ControlledOutboundWindowsWorkerTransport(OutboundWindowsWorkerTransport):
    """Test-only transport that accepts explicitly controlled identity evidence."""

    @staticmethod
    def _evidence_is_authenticated(evidence: WindowsWorkerSessionEvidence) -> bool:
        del evidence
        return True


class ControlledWindowsWorkerSession:
    """Deterministic Job Object session used only at the public contract seam."""

    def __init__(
        self,
        *,
        evidence: WindowsWorkerSessionEvidence,
        result: WorkerExecutionResult | None = None,
        execution_hook: Callable[[WorkerInvocation], WorkerExecutionResult]
        | None = None,
        process_tree_stopped: bool = True,
    ) -> None:
        self._evidence = evidence
        self.result = result or WorkerExecutionResult.completed()
        self.execution_hook = execution_hook
        self.process_tree_stopped = process_tree_stopped
        self.invocations: list[WorkerInvocation] = []
        self.job_object_terminations: list[str] = []
        self.finalizations: list[str] = []

    @property
    def evidence(self) -> WindowsWorkerSessionEvidence:
        return self._evidence

    def execute(
        self, invocation: WorkerInvocation, progress: WorkerProgressSink
    ) -> WorkerExecutionResult:
        del progress
        self.invocations.append(invocation)
        if self.execution_hook is not None:
            return self.execution_hook(invocation)
        return self.result

    def terminate_job_object(self, *, action_id: str, timeout_seconds: int) -> bool:
        del timeout_seconds
        self.job_object_terminations.append(action_id)
        return self.process_tree_stopped

    def finalize(self, *, action_id: str, timeout_seconds: int) -> None:
        del timeout_seconds
        self.finalizations.append(action_id)


__all__ = [
    "ControlledOutboundWindowsWorkerTransport",
    "ControlledWindowsWorkerSession",
    "OutboundWindowsWorkerTransport",
]
