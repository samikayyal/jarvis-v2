from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
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
    InboundMessage,
    SignedInboundEvent,
    TraceWriteError,
    WorkerExecutionError,
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
    WorkerIdentity,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerProgressSink,
)
from jarvis_control_plane.manual_admin import _open_manual_trace_boundary
from jarvis_control_plane.sessions import DispatchStatus, ReadinessState

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"
SECRET = b"ticket11-test-secret"


def _worker_identity(
    *,
    host: str = "ubuntu",
    worker_id: str = "ubuntu-01",
    connection_id: str = "boot-01",
) -> WorkerIdentity:
    return WorkerIdentity(host=host, worker_id=worker_id, connection_id=connection_id)


def _event(text: str, suffix: str) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id=f"event-worker-{suffix}",
            message_id=f"message-worker-{suffix}",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        SECRET,
    )


def test_worker_gateway_rejects_mismatched_authenticated_identity_without_failover() -> (
    None
):
    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity(host="windows", worker_id="windows-01")}
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
        id_prefix="ticket11-identity",
        action_dispatcher=gateway,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-worker-001",
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

    result = components.receiver.receive(_event("show repo status", "identity"))

    assert result.disposition == "action_dispatch_failed"
    assert worker.executions == []
    assert worker.cancelled == []
    current = components.broker.working_sessions.load()
    assert current is not None
    assert current.action_outbox[-1].status is DispatchStatus.FAILED


def test_worker_gateway_rechecks_the_authenticated_connection_at_execution_barrier() -> (
    None
):
    registered = WorkerIdentity(
        host="ubuntu", worker_id="ubuntu-01", connection_id="boot-a"
    )
    worker = ControlledWorkerTransport(identities={"ubuntu": registered})
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": registered},
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-rebind",
        request_id="request-worker-rebind",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        },
    )

    handle = gateway.prepare(proposal)
    worker.identities["ubuntu"] = WorkerIdentity(
        host="ubuntu", worker_id="ubuntu-01", connection_id="boot-b"
    )

    with pytest.raises(ActionDispatcherError, match="registered worker connection"):
        handle.run()

    assert worker.invocations == []
    assert worker.executions == []


def test_worker_gateway_exposes_ordered_bounded_progress_events() -> None:
    identity = WorkerIdentity(
        host="ubuntu", worker_id="ubuntu-01", connection_id="boot-progress"
    )

    def publish(progress: WorkerProgressSink) -> None:
        progress(
            WorkerProgressEvent(
                sequence=2,
                kind=WorkerProgressKind.MILESTONE,
                text="prepared",
            )
        )
        progress(
            WorkerProgressEvent(
                sequence=3,
                kind=WorkerProgressKind.OUTPUT,
                text="git status\n",
                stream=WorkerOutputStream.STDOUT,
            )
        )

    worker = ControlledWorkerTransport(
        identities={"ubuntu": identity},
        progress_hook=publish,
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker}, registered_identities={"ubuntu": identity}
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-progress",
        request_id="request-worker-progress",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        },
    )

    result = gateway.dispatch(proposal)

    assert worker.invocations[0].worker_identity == identity
    assert [event.sequence for event in result.progress_events] == [1, 2, 3]
    assert [event.kind for event in result.progress_events] == [
        WorkerProgressKind.READY,
        WorkerProgressKind.MILESTONE,
        WorkerProgressKind.OUTPUT,
    ]
    assert result.progress_events[-1].stream is WorkerOutputStream.STDOUT


def test_worker_progress_readiness_is_payload_free_and_gateway_owned() -> None:
    assert WorkerProgressEvent.ready().text == ""
    with pytest.raises(ValueError, match="cannot contain a payload"):
        WorkerProgressEvent(
            sequence=1,
            kind=WorkerProgressKind.READY,
            text="x" * 10_000,
        )
    with pytest.raises(ValueError, match="must be the first"):
        WorkerProgressEvent(sequence=2, kind=WorkerProgressKind.READY)


def test_worker_gateway_forwards_only_bounded_non_interactive_execution() -> None:
    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        result=WorkerExecutionResult.completed(stdout="x" * (1024 * 1024 + 10)),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-bounded",
        request_id="request-worker-bounded",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        },
    )

    result = gateway.dispatch(proposal)

    invocation = worker.invocations[0]
    assert invocation.interactive is False
    assert invocation.deadline_seconds == 120
    assert len(result.stdout.encode()) <= 1024 * 1024
    assert result.stdout.endswith("[truncated]")


def test_worker_dispatch_result_is_retained_in_the_manual_trace_boundary() -> None:
    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        result=WorkerExecutionResult.completed(stdout="bounded worker output"),
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
        id_prefix="ticket11-trace",
        action_dispatcher=gateway,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id="action-worker-trace",
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

    result = components.receiver.receive(_event("show repo status", "trace"))

    assert result.disposition == "action_dispatched"
    assert components.trace_store is not None
    manual = _open_manual_trace_boundary(components.trace_store)
    try:
        traces = manual.list_traces(operation_type="worker")
        assert len(traces) == 1
        assert "bounded worker output" in str(traces[0].result)
    finally:
        manual.close()


def test_worker_gateway_retains_known_partial_compound_failure_without_retry() -> None:
    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        result=WorkerExecutionResult(
            status=WorkerExecutionStatus.FAILED,
            started_components=(0, 1),
            completed_components=(0,),
            process_tree_stopped=True,
            stderr="second command exited 1",
        ),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-partial",
        request_id="request-worker-partial",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
            "components": [
                {"executable": "/usr/bin/git", "arguments": ["status"]},
                {
                    "operator_before": "&&",
                    "executable": "/usr/bin/git",
                    "arguments": ["diff"],
                },
            ],
        },
    )

    with pytest.raises(
        ActionDispatcherError, match="started: 1,2; components completed: 1"
    ) as exc:
        gateway.dispatch(proposal)

    assert isinstance(exc.value, WorkerExecutionError)
    assert exc.value.may_have_dispatched is False
    assert len(worker.invocations) == 1
    assert exc.value.result.status is WorkerExecutionStatus.FAILED


def test_worker_gateway_enforces_deadline_then_cancels_the_process_scope() -> None:
    release_execution = Event()

    def wait_for_cancellation(_invocation: object) -> WorkerExecutionResult:
        assert release_execution.wait(timeout=5)
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.CANCELLED,
            started_components=(0,),
            process_tree_stopped=True,
        )

    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        execution_hook=wait_for_cancellation,
        on_cancel=lambda _action_id: release_execution.set(),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
        limits=WorkerExecutionLimits(deadline_seconds=1),
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-timeout",
        request_id="request-worker-timeout",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        },
    )

    with pytest.raises(WorkerExecutionError, match="timed_out") as exc:
        gateway.dispatch(proposal)

    assert worker.cancelled == [proposal.action_id]
    assert exc.value.result.status is WorkerExecutionStatus.TIMED_OUT


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


@pytest.mark.parametrize(
    "status",
    [
        WorkerExecutionStatus.COMPLETED,
        WorkerExecutionStatus.FAILED,
        WorkerExecutionStatus.TIMED_OUT,
        WorkerExecutionStatus.CANCELLED,
    ],
)
def test_definite_worker_status_without_process_tree_proof_becomes_unknown(
    status: WorkerExecutionStatus,
) -> None:
    result = WorkerExecutionResult(
        status=status,
        started_components=(0,),
        completed_components=(0,) if status is WorkerExecutionStatus.COMPLETED else (),
        process_tree_stopped=False,
    )

    assert result.status is WorkerExecutionStatus.UNKNOWN


def test_worker_gateway_publishes_handle_before_worker_start_and_cancellation_wins() -> (
    None
):
    worker = ControlledWorkerTransport(identities={"ubuntu": _worker_identity()})
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-before-start-cancel",
        request_id="request-worker-before-start-cancel",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        },
    )

    handle = gateway.prepare(proposal)
    cancellation = gateway.cancel(action_id=proposal.action_id)

    assert cancellation.status is ActionCancellationStatus.NOT_STARTED
    assert proposal.action_id not in gateway._running  # type: ignore[attr-defined]
    with pytest.raises(WorkerExecutionError) as exc:
        handle.run()
    assert exc.value.result.status is WorkerExecutionStatus.CANCELLED
    assert worker.invocations == []


def test_transport_tombstone_wins_after_gateway_execute_submission() -> None:
    worker = ControlledWorkerTransport(identities={"ubuntu": _worker_identity()})
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-transport-tombstone",
        request_id="request-worker-transport-tombstone",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/git",
            "arguments": ["status"],
            "cwd": "/workspace",
        },
    )
    handle = gateway.prepare(proposal)
    ready_to_submit = Event()
    release_submission = Event()

    def pause_before_transport_call() -> None:
        ready_to_submit.set()
        assert release_submission.wait(timeout=5)

    # This seam pauses after the gateway's local submission marker and before
    # the transport call, making the cancellation/execute race deterministic.
    handle._record_gateway_ready = pause_before_transport_call  # type: ignore[attr-defined]
    errors: list[BaseException] = []

    def run_handle() -> None:
        try:
            handle.run()
        except BaseException as exc:  # noqa: BLE001 - capture thread outcome
            errors.append(exc)

    execution = Thread(
        target=run_handle,
    )
    execution.start()
    try:
        assert ready_to_submit.wait(timeout=5)
        cancellation = gateway.cancel(action_id=proposal.action_id)
        assert cancellation.status is ActionCancellationStatus.NOT_STARTED
        release_submission.set()
        execution.join(timeout=5)
        assert not execution.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], WorkerExecutionError)
        assert errors[0].result.started_components == ()  # type: ignore[union-attr]
        assert worker.invocations == []
    finally:
        release_submission.set()
        execution.join(timeout=5)


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
