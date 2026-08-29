from __future__ import annotations

from dataclasses import replace

import pytest
from test_support import build_receiver_components

from jarvis_control_plane import (
    ActionDispatcherError,
    ControlledOrchestrationAdapter,
    ControlledWorkerTransport,
    FrozenActionProposal,
    WorkerExecutionResult,
    WorkerGateway,
    WorkerIdentity,
    WorkerOutputStream,
    WorkerProgressEvent,
    WorkerProgressKind,
    WorkerProgressSink,
)
from jarvis_control_plane.ports import WorkerReadiness
from jarvis_control_plane.sessions import DispatchStatus, ReadinessState

from .helpers import (
    NOW,
    OPERATOR,
    SECRET,
    TRANSPORT_SESSION,
    _event,
    _worker_identity,
)


def test_gateway_authenticates_each_worker_before_publishing_readiness() -> None:
    ubuntu = ControlledWorkerTransport(identities={"ubuntu": _worker_identity()})
    windows_expected = _worker_identity(
        host="windows", worker_id="windows-01", connection_id="windows-boot-01"
    )
    windows = ControlledWorkerTransport(
        identities={
            "windows": replace(windows_expected, connection_id="unexpected-boot")
        }
    )
    gateway = WorkerGateway(
        workers={"ubuntu": ubuntu, "windows": windows},
        registered_identities={
            "ubuntu": _worker_identity(),
            "windows": windows_expected,
        },
    )

    assert gateway.current() == WorkerReadiness(ubuntu="ready", windows="unavailable")


def test_broker_persists_current_worker_readiness_before_handling_messages() -> None:
    class FixedWorkerReadiness:
        @staticmethod
        def current() -> WorkerReadiness:
            return WorkerReadiness(ubuntu="ready", windows="unavailable")

    components = build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket11-readiness",
        worker_readiness_provider=FixedWorkerReadiness(),
    )

    components.receiver.receive(_event("/status", "readiness"))

    current = components.broker.working_sessions.load()
    assert current is not None
    assert current.readiness.ubuntu == "ready"
    assert current.readiness.windows == "unavailable"


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
