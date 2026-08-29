from __future__ import annotations

from dataclasses import replace
from threading import Event

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ActionDispatcherError,
    ControlledOrchestrationAdapter,
    ControlledWorkerTransport,
    FrozenActionProposal,
    WorkerExecutionError,
    WorkerExecutionLimits,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
)
from jarvis_control_plane.manual_admin import _open_manual_trace_boundary
from jarvis_control_plane.sessions import ReadinessState

from .helpers import (
    NOW,
    OPERATOR,
    SECRET,
    TRANSPORT_SESSION,
    _event,
    _worker_identity,
)


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
    assert result.reason == (
        "Execution host: ubuntu\n"
        "Execution status: completed\n"
        "stdout_truncated: false\n"
        "stderr_truncated: false\n"
        'stdout JSON: "bounded worker output"\n'
        'stderr JSON: ""'
    )
    assert result.reply is not None
    assert result.reply.body.startswith(result.reason)
    assert worker.finalizations == ["action-worker-trace"]
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


def test_worker_gateway_rejects_completed_progress_with_an_unfinished_component() -> (
    None
):
    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        result=WorkerExecutionResult(
            status=WorkerExecutionStatus.COMPLETED,
            started_components=(0, 1),
            completed_components=(0,),
            process_tree_stopped=True,
        ),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-impossible-completion",
        request_id="request-worker-impossible-completion",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/true",
            "arguments": [],
            "cwd": "/workspace",
            "components": [
                {"executable": "/usr/bin/true", "arguments": []},
                {
                    "operator_before": "&&",
                    "executable": "/usr/bin/true",
                    "arguments": [],
                },
            ],
        },
    )

    with pytest.raises(WorkerExecutionError) as exc:
        gateway.dispatch(proposal)

    assert exc.value.result.status is WorkerExecutionStatus.UNKNOWN


def test_worker_gateway_accepts_completed_short_circuit_progress() -> None:
    worker = ControlledWorkerTransport(
        identities={"ubuntu": _worker_identity()},
        result=WorkerExecutionResult(
            status=WorkerExecutionStatus.COMPLETED,
            started_components=(0,),
            completed_components=(0,),
            process_tree_stopped=True,
        ),
    )
    gateway = WorkerGateway(
        workers={"ubuntu": worker},
        registered_identities={"ubuntu": _worker_identity()},
    )
    proposal = FrozenActionProposal.create(
        action_id="action-worker-short-circuit",
        request_id="request-worker-short-circuit",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/true",
            "arguments": [],
            "cwd": "/workspace",
            "components": [
                {"executable": "/usr/bin/true", "arguments": []},
                {
                    "operator_before": "||",
                    "executable": "/usr/bin/printf",
                    "arguments": ["not-run"],
                },
            ],
        },
    )

    result = gateway.dispatch(proposal)

    assert result.status is WorkerExecutionStatus.COMPLETED
    assert result.started_components == (0,)
    assert result.completed_components == (0,)


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
