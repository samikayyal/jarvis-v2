from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Thread

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ActionDispatcherError,
    ControlledOrchestrationAdapter,
    ControlledWorkerTransport,
    FrozenActionProposal,
    InboundMessage,
    SignedInboundEvent,
    WorkerExecutionError,
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
    WorkerIdentity,
)
from jarvis_control_plane.manual_admin import _open_manual_trace_boundary
from jarvis_control_plane.sessions import DispatchStatus, ReadinessState

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
OPERATOR = "operator.test"
TRANSPORT_SESSION = "session.test"
SECRET = b"ticket11-test-secret"


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
        identities={"ubuntu": WorkerIdentity(host="windows", worker_id="windows-01")}
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={
            "ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")
        },
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


def test_worker_gateway_forwards_only_bounded_non_interactive_execution() -> None:
    worker = ControlledWorkerTransport(
        identities={"ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")},
        result=WorkerExecutionResult.completed(stdout="x" * (1024 * 1024 + 10)),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={
            "ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")
        },
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
        identities={"ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")},
        result=WorkerExecutionResult.completed(stdout="bounded worker output"),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={
            "ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")
        },
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
        identities={"ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")},
        result=WorkerExecutionResult(
            status=WorkerExecutionStatus.FAILED,
            started_components=(0, 1),
            completed_components=(0,),
            stderr="second command exited 1",
        ),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={
            "ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")
        },
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
        )

    worker = ControlledWorkerTransport(
        identities={"ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")},
        execution_hook=wait_for_cancellation,
        on_cancel=lambda _action_id: release_execution.set(),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={
            "ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")
        },
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


def test_cancel_targets_the_running_selected_worker_and_ends_dispatch() -> None:
    execution_started = Event()
    release_execution = Event()

    def block_until_cancel(_invocation: object) -> WorkerExecutionResult:
        execution_started.set()
        assert release_execution.wait(timeout=5)
        return WorkerExecutionResult(
            status=WorkerExecutionStatus.CANCELLED,
            started_components=(0,),
            completed_components=(),
        )

    worker = ControlledWorkerTransport(
        identities={"ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")},
        execution_hook=block_until_cancel,
        on_cancel=lambda _action_id: release_execution.set(),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={
            "ubuntu": WorkerIdentity(host="ubuntu", worker_id="ubuntu-01")
        },
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
    finally:
        release_execution.set()
        dispatch.join(timeout=5)
