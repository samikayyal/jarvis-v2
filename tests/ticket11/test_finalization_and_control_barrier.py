from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread
from time import monotonic

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ControlledOrchestrationAdapter,
    ControlledWorkerTransport,
    FrozenActionProposal,
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
    WorkerIdentity,
    WorkerInvocation,
    WorkerProgressSink,
)
from jarvis_control_plane.sessions import DispatchStatus, ReadinessState

from .helpers import (
    NOW,
    OPERATOR,
    SECRET,
    TRANSPORT_SESSION,
    _event,
    _worker_identity,
)


def test_worker_transport_finalization_retires_state_and_rejects_stale_execution() -> (
    None
):
    now = [100.0]
    identity = _worker_identity(connection_id="retirement")
    limits = WorkerExecutionLimits(
        registration_timeout_seconds=1,
        action_state_retention_seconds=2,
    )
    worker = ControlledWorkerTransport(
        identities={"ubuntu": identity},
        clock=lambda: now[0],
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": identity},
        limits=limits,
    )

    def make_proposal(action_id: str) -> FrozenActionProposal:
        return FrozenActionProposal.create(
            action_id=action_id,
            request_id=f"request-{action_id}",
            kind="terminal",
            preview="Run the exact terminal action.",
            payload={
                "host": "ubuntu",
                "executable": "/usr/bin/git",
                "arguments": ["status"],
                "cwd": "/workspace",
            },
        )

    cancelled_proposal = make_proposal("action-worker-retire-cancelled")
    cancelled_handle = gateway.prepare(cancelled_proposal)
    cancellation = gateway.cancel(action_id=cancelled_proposal.action_id)
    assert cancellation.status is ActionCancellationStatus.NOT_STARTED
    gateway.finalize(action_id=cancelled_proposal.action_id)

    completed_proposal = make_proposal("action-worker-retire-completed")
    completed_handle = gateway.prepare(completed_proposal)
    assert completed_handle.run().status is WorkerExecutionStatus.COMPLETED
    gateway.finalize(action_id=completed_proposal.action_id)

    assert len(worker._action_states) == 2  # type: ignore[attr-defined]
    assert worker.finalizations == [
        cancelled_proposal.action_id,
        completed_proposal.action_id,
    ]

    def stale(handle: object, action_id: str) -> WorkerInvocation:
        return WorkerInvocation(
            action_id=action_id,
            action=handle.terminal,  # type: ignore[attr-defined]
            interactive=False,
            deadline_seconds=limits.deadline_seconds,
            stdout_limit_bytes=limits.stdout_limit_bytes,
            stderr_limit_bytes=limits.stderr_limit_bytes,
            cancellation_grace_seconds=limits.cancellation_grace_seconds,
            progress_event_limit=limits.progress_event_limit,
            milestone_limit_bytes=limits.milestone_limit_bytes,
            worker_identity=identity,
        )

    for handle, action_id in (
        (cancelled_handle, cancelled_proposal.action_id),
        (completed_handle, completed_proposal.action_id),
    ):
        with pytest.raises(ActionDispatcherError, match="not executable"):
            worker.execute(stale(handle, action_id), lambda _event: None)

    now[0] += 2.1
    with pytest.raises(ActionDispatcherError, match="not executable"):
        worker.execute(
            stale(cancelled_handle, cancelled_proposal.action_id),
            lambda _event: None,
        )
    assert worker._action_states == {}  # type: ignore[attr-defined]
    assert worker._cancel_results == {}  # type: ignore[attr-defined]


def test_approval_dispatch_releases_lock_before_worker_completion() -> None:
    class BlockingHandle:
        def __init__(self) -> None:
            self.started = Event()
            self.release = Event()
            self.cancelled = Event()

        def run(self) -> None:
            self.started.set()
            assert self.release.wait(timeout=5)

        def cancel(self, *, action_id: str) -> ActionCancellationResult:
            self.cancelled.set()
            self.release.set()
            return ActionCancellationResult(ActionCancellationStatus.STOPPED)

    class BlockingDispatcher:
        def __init__(self) -> None:
            self.handle = BlockingHandle()

        def prepare(self, _action: FrozenActionProposal) -> BlockingHandle:
            return self.handle

        def cancel(self, *, action_id: str) -> ActionCancellationResult:
            return self.handle.cancel(action_id=action_id)

    dispatcher = BlockingDispatcher()
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket11-approval-cancel",
        action_dispatcher=dispatcher,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-worker-approval-cancel",
                request_id=request.state.request_id,
                kind="terminal",
                preview="Run the exact terminal action.",
                payload={
                    "host": "ubuntu",
                    "executable": "/usr/bin/touch",
                    "arguments": ["/workspace/exact.txt"],
                    "cwd": "/workspace",
                },
            )
        ),
    )
    session = components.broker.working_sessions.load()
    assert session is not None
    components.broker.working_sessions.compare_and_set(
        session, replace(session, readiness=ReadinessState(ubuntu="ready"))
    )

    proposed = components.receiver.receive(
        _event("run the command", "approval-proposal")
    )
    assert proposed.disposition == "pending_action"

    approval_result: list[object] = []
    approval = Thread(
        target=lambda: approval_result.append(
            components.receiver.receive(_event("1", "approval-confirm"))
        )
    )
    approval.start()
    try:
        assert dispatcher.handle.started.wait(timeout=5)
        started_at = monotonic()
        cancelled = components.receiver.receive(_event("/cancel", "approval-cancel"))
        elapsed = monotonic() - started_at

        assert cancelled.disposition == "cancelled"
        assert elapsed < 3
        assert dispatcher.handle.cancelled.is_set()
        approval.join(timeout=5)
        assert not approval.is_alive()
        assert approval_result[0].disposition == "late_result_ignored"
    finally:
        dispatcher.handle.release.set()
        approval.join(timeout=5)

    current = components.broker.working_sessions.load()
    assert current is not None
    assert current.action_outbox[-1].status is DispatchStatus.CANCELLED


@pytest.mark.parametrize("control_text", ["/cancel", "/new"])
def test_blocked_worker_registration_does_not_hold_the_control_barrier(
    control_text: str,
) -> None:
    class BlockingRegistrationTransport:
        def __init__(self) -> None:
            self.identity = _worker_identity(connection_id="blocked-registration")
            self.registration_started = Event()
            self.release_registration = Event()
            self.cancelled = Event()
            self.finalized = Event()
            self.executed: list[str] = []
            self.registration_arguments: tuple[int, int] | None = None

        def register_execution(
            self,
            *,
            action_id: str,
            timeout_seconds: int,
            retention_seconds: int,
        ) -> None:
            del action_id
            self.registration_arguments = (timeout_seconds, retention_seconds)
            self.registration_started.set()
            if not self.release_registration.wait(timeout=5):
                raise AssertionError("test registration was not released")

        def authenticate(
            self, *, selected_host: str, timeout_seconds: int
        ) -> WorkerIdentity:
            del timeout_seconds
            if selected_host != self.identity.host:
                raise ActionDispatcherError("unexpected selected host")
            return self.identity

        def execute(
            self, invocation: WorkerInvocation, progress: WorkerProgressSink
        ) -> WorkerExecutionResult:
            del progress
            self.executed.append(invocation.action_id)
            return WorkerExecutionResult.completed()

        def cancel(
            self,
            *,
            action_id: str,
            timeout_seconds: int,
            retention_seconds: int,
        ) -> ActionCancellationResult:
            del action_id, timeout_seconds, retention_seconds
            self.cancelled.set()
            return ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)

        def finalize_execution(
            self,
            *,
            action_id: str,
            timeout_seconds: int,
            retention_seconds: int,
        ) -> None:
            del action_id, timeout_seconds, retention_seconds
            self.finalized.set()

    worker = BlockingRegistrationTransport()
    limits = WorkerExecutionLimits(
        registration_timeout_seconds=1,
        action_state_retention_seconds=1,
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": worker.identity},
        limits=limits,
    )
    action_id = f"action-worker-blocked-registration-{control_text[1:]}"
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix=f"ticket11-blocked-registration-{control_text[1:]}",
        action_dispatcher=gateway,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id=action_id,
                request_id=request.state.request_id,
                kind="terminal",
                preview="Run the exact terminal action.",
                payload={
                    "host": "ubuntu",
                    "executable": "/usr/bin/touch",
                    "arguments": ["/workspace/exact.txt"],
                    "cwd": "/workspace",
                },
            )
        ),
    )
    session = components.broker.working_sessions.load()
    assert session is not None
    components.broker.working_sessions.compare_and_set(
        session, replace(session, readiness=ReadinessState(ubuntu="ready"))
    )

    proposed = components.receiver.receive(
        _event(
            "run the exact command", f"blocked-registration-{control_text[1:]}-proposal"
        )
    )
    assert proposed.disposition == "pending_action"

    approval_result: list[object] = []
    approval = Thread(
        target=lambda: approval_result.append(
            components.receiver.receive(
                _event("1", f"blocked-registration-{control_text[1:]}-approval")
            )
        )
    )
    approval.start()
    control_result: list[object] = []
    control = Thread(
        target=lambda: control_result.append(
            components.receiver.receive(
                _event(
                    control_text,
                    f"blocked-registration-{control_text[1:]}-control",
                )
            )
        )
    )
    try:
        assert worker.registration_started.wait(timeout=5)
        started_at = monotonic()
        control.start()
        control.join(timeout=1.5)
        elapsed = monotonic() - started_at

        assert not control.is_alive()
        assert len(control_result) == 1
        assert control_result[0].disposition == (
            "cancelled" if control_text == "/cancel" else "new_session"
        )
        assert elapsed < 1.5
        assert worker.cancelled.is_set()
        assert worker.registration_arguments == (1, 1)
    finally:
        worker.release_registration.set()
        control.join(timeout=5)
        approval.join(timeout=5)

    assert not control.is_alive()
    assert not approval.is_alive()
    assert worker.executed == []
    assert worker.finalized.is_set()


def test_cancel_marks_an_inflight_connector_action_unknown_without_a_stop_proof() -> (
    None
):
    class BlockingConnectorHandle:
        def __init__(self) -> None:
            self.started = Event()
            self.release = Event()
            self.cancelled = Event()

        def run(self) -> None:
            self.started.set()
            assert self.release.wait(timeout=5)

        def cancel(self, *, action_id: str) -> ActionCancellationResult:
            self.cancelled.set()
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)

    class BlockingConnectorDispatcher:
        def __init__(self) -> None:
            self.handle = BlockingConnectorHandle()

        def prepare(self, _action: FrozenActionProposal) -> BlockingConnectorHandle:
            return self.handle

        def cancel(self, *, action_id: str) -> ActionCancellationResult:
            return self.handle.cancel(action_id=action_id)

    dispatcher = BlockingConnectorDispatcher()
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket11-connector-cancel",
        action_dispatcher=dispatcher,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-calendar-cancel",
                request_id=request.state.request_id,
                kind="calendar_update",
                preview="Update the calendar event.",
                payload={
                    "event_id": "event-calendar-cancel",
                    "start": "2026-08-07T13:00:00Z",
                },
            )
        ),
    )

    proposed = components.receiver.receive(
        _event("update the calendar", "connector-proposal")
    )
    assert proposed.disposition == "pending_action"

    approval_result: list[object] = []
    approval = Thread(
        target=lambda: approval_result.append(
            components.receiver.receive(_event("1", "connector-approval"))
        )
    )
    approval.start()
    try:
        assert dispatcher.handle.started.wait(timeout=5)
        cancelled = components.receiver.receive(_event("/cancel", "connector-cancel"))

        assert cancelled.disposition == "cancelled"
        assert dispatcher.handle.cancelled.is_set()
        current = components.broker.working_sessions.load()
        assert current is not None
        assert current.action_outbox[-1].status is DispatchStatus.UNKNOWN

        dispatcher.handle.release.set()
        approval.join(timeout=5)
        assert not approval.is_alive()
        assert approval_result[0].disposition == "action_dispatch_unknown"
    finally:
        dispatcher.handle.release.set()
        approval.join(timeout=5)
