from __future__ import annotations

from threading import Event, Thread

import pytest

from jarvis_control_plane import (
    ActionCancellationStatus,
    ControlledWorkerTransport,
    FrozenActionProposal,
    WorkerExecutionError,
    WorkerExecutionResult,
    WorkerExecutionStatus,
    WorkerGateway,
)

from .helpers import (
    _worker_identity,
)


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
