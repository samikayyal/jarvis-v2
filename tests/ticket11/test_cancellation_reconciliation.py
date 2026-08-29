from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread
from time import monotonic, sleep

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    ControlledOrchestrationAdapter,
    ControlledWorkerTransport,
    FrozenActionProposal,
    TraceWriteError,
    WorkerExecutionError,
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
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


@pytest.mark.parametrize(
    ("worker_cancellation", "expected_status"),
    [
        (None, DispatchStatus.CANCELLED),
        (ActionCancellationStatus.UNKNOWN, DispatchStatus.UNKNOWN),
    ],
    ids=("stopped", "unknown"),
)
def test_cancel_reconciles_the_running_selected_worker(
    worker_cancellation: ActionCancellationStatus | None,
    expected_status: DispatchStatus,
) -> None:
    execution_started = Event()
    release_execution = Event()

    def block_until_cancel(_invocation: object) -> WorkerExecutionResult:
        execution_started.set()
        assert release_execution.wait(timeout=5)
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.CANCELLED,
            started_components=(0,),
            completed_components=(),
            process_tree_stopped=True,
        )

    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        execution_hook=block_until_cancel,
        on_cancel=lambda _action_id: (
            release_execution.set()
            or (
                ActionCancellationResult(worker_cancellation)
                if worker_cancellation is not None
                else None
            )
        ),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
    )
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket11-cancel",
        action_dispatcher=gateway,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-worker-cancel",
                request_id=request.state.request_id,
                kind="terminal",
                preview="Run the exact terminal action.",
                payload={
                    "host": "ubuntu",
                    "executable": "/usr/bin/git",
                    "arguments": ["status"],
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
    holder: list[object] = []
    dispatch = Thread(
        target=lambda: holder.append(
            components.receiver.receive(_event("show repo status", "cancel-work"))
        )
    )
    dispatch.start()
    try:
        assert execution_started.wait(timeout=5)

        cancelled = components.receiver.receive(_event("/cancel", "cancel-command"))

        assert cancelled.disposition == "cancelled"
        assert worker.cancelled == ["action-worker-cancel"]
        dispatch.join(timeout=5)
        assert not dispatch.is_alive()
        current = components.broker.working_sessions.load()
        assert current is not None
        assert current.action_outbox[-1].status is expected_status
        assert holder[0].disposition == (
            "action_dispatch_unknown"
            if expected_status is DispatchStatus.UNKNOWN
            else "late_result_ignored"
        )
    finally:
        release_execution.set()
        dispatch.join(timeout=5)


def test_new_reconciles_the_worker_cancellation_acknowledgement() -> None:
    execution_started = Event()
    release_execution = Event()

    def block_until_cancel(_invocation: object) -> WorkerExecutionResult:
        execution_started.set()
        assert release_execution.wait(timeout=5)
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.CANCELLED,
            started_components=(0,),
            process_tree_stopped=True,
        )

    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        execution_hook=block_until_cancel,
        on_cancel=lambda _action_id: release_execution.set() or None,
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
    )
    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket11-new-cancel",
        action_dispatcher=gateway,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-worker-new-cancel",
                request_id=request.state.request_id,
                kind="terminal",
                preview="Run the exact terminal action.",
                payload={
                    "host": "ubuntu",
                    "executable": "/usr/bin/git",
                    "arguments": ["status"],
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
    holder: list[object] = []
    dispatch = Thread(
        target=lambda: holder.append(
            components.receiver.receive(_event("show repo status", "new-work"))
        )
    )
    dispatch.start()
    try:
        assert execution_started.wait(timeout=5)
        replaced = components.receiver.receive(_event("/new", "new-command"))

        assert replaced.disposition == "new_session"
        assert replaced.reply is not None
        assert "Previous work was stopped" in replaced.reply.body
        current = components.broker.working_sessions.load()
        assert current is not None
        assert current.session_id == "S-002"
        assert current.action_outbox[-1].status is DispatchStatus.CANCELLED

        dispatch.join(timeout=5)
        assert not dispatch.is_alive()
        assert holder[0].disposition == "late_result_ignored"
    finally:
        release_execution.set()
        dispatch.join(timeout=5)


def test_post_start_transport_disconnect_is_an_unknown_worker_outcome() -> None:
    def disconnect(_invocation: object) -> WorkerExecutionResult:
        raise ActionDispatcherError("worker transport disconnected")

    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        execution_hook=disconnect,
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-disconnect",
        request_id="request-worker-disconnect",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        },
    )

    with pytest.raises(WorkerExecutionError) as exc:
        gateway.dispatch(proposal)

    assert exc.value.may_have_dispatched is True
    assert exc.value.result.status is WorkerExecutionStatus.UNKNOWN
    assert exc.value.result.started_components == ()


def test_deadline_cancellation_preserves_an_explicit_unknown_worker_result() -> None:
    release_execution = Event()

    def unknown_after_cancel(_invocation: object) -> WorkerExecutionResult:
        assert release_execution.wait(timeout=5)
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.UNKNOWN,
            started_components=(0,),
        )

    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        execution_hook=unknown_after_cancel,
        on_cancel=lambda _action_id: release_execution.set(),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
        limits=WorkerExecutionLimits(deadline_seconds=1),
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-deadline-unknown",
        request_id="request-worker-deadline-unknown",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        },
    )

    with pytest.raises(WorkerExecutionError) as exc:
        gateway.dispatch(proposal)

    assert exc.value.may_have_dispatched is True
    assert exc.value.result.status is WorkerExecutionStatus.UNKNOWN


def test_late_worker_completion_cleans_its_unconfirmed_running_entry() -> None:
    release_execution = Event()

    def complete_late(_invocation: object) -> WorkerExecutionResult:
        assert release_execution.wait(timeout=5)
        return WorkerExecutionResult.completed()

    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        execution_hook=complete_late,
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
        limits=WorkerExecutionLimits(deadline_seconds=1, cancellation_grace_seconds=1),
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-late-cleanup",
        request_id="request-worker-late-cleanup",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        },
    )

    with pytest.raises(WorkerExecutionError) as exc:
        gateway.dispatch(proposal)
    assert exc.value.result.status is WorkerExecutionStatus.UNKNOWN
    assert proposal.action_id in gateway._running  # type: ignore[attr-defined]

    release_execution.set()
    deadline = monotonic() + 2
    while proposal.action_id in gateway._running and monotonic() < deadline:  # type: ignore[attr-defined]
        sleep(0.01)

    assert proposal.action_id not in gateway._running  # type: ignore[attr-defined]


def test_post_dispatch_trace_failure_returns_the_same_unknown_outcome_it_persists() -> (
    None
):
    class TraceFailingDispatcher:
        def prepare(self, _action: FrozenActionProposal) -> TraceFailingDispatcher:
            return self

        def run(self) -> None:
            try:
                raise TraceWriteError(
                    "trace persistence failed", operation_started=True
                )
            except TraceWriteError as trace_error:
                raise ActionDispatcherError(
                    "worker result could not be recorded"
                ) from trace_error

        def cancel(self, *, action_id: str) -> ActionCancellationResult:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)

    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket11-trace-failure",
        action_dispatcher=TraceFailingDispatcher(),
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-worker-trace-failure",
                request_id=request.state.request_id,
                kind="terminal",
                preview="Run the exact terminal action.",
                payload={
                    "host": "ubuntu",
                    "executable": "/usr/bin/git",
                    "arguments": ["status"],
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

    result = components.receiver.receive(_event("show repo status", "trace-failure"))

    assert result.disposition == "action_dispatch_unknown"
    current = components.broker.working_sessions.load()
    assert current is not None
    assert current.action_outbox[-1].status is DispatchStatus.UNKNOWN
